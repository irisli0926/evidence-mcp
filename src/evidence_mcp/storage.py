"""Local evidence store: the canonical copy of everything that was archived.

    <EVIDENCE_ROOT>/
    ├── artifacts/
    │   └── ev_<id>/
    │       ├── <original file name>     the exact downloaded bytes
    │       └── metadata.json            provenance (written last)
    └── tmp/                             staging area for in-progress writes

Failure safety: a new evidence folder is completely written inside tmp/
(artifact first, metadata.json last) and then moved into artifacts/ with ONE
rename. A rename on the same filesystem is atomic, so artifacts/ only ever
contains complete folders: if metadata.json exists, the artifact exists too.
An interrupted write can only leave junk in tmp/, which readers ignore.
"""

import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = "artifacts"
STAGING_DIR = "tmp"
METADATA_FILE = "metadata.json"
RESERVED_NAMES = {METADATA_FILE, METADATA_FILE + ".tmp"}
EVIDENCE_ID_PATTERN = re.compile(r"ev_[0-9a-f]{16}")
MAX_STEM_LENGTH = 80


def evidence_id_for(sha256: str) -> str:
    """The ID is derived from the content hash: the same bytes always get the same ID."""
    return f"ev_{sha256[:16]}"


def is_valid_evidence_id(evidence_id: str) -> bool:
    # A strict pattern means an ID can never contain "/" or "..".
    return EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is not None


def evidence_dir(root: Path, evidence_id: str) -> Path:
    if not is_valid_evidence_id(evidence_id):
        raise ValueError("invalid evidence ID")
    return root / ARTIFACTS_DIR / evidence_id


def load_metadata(root: Path, evidence_id: str) -> dict[str, Any] | None:
    """Return one evidence item's metadata, or None if it does not exist."""
    if not is_valid_evidence_id(evidence_id):
        return None
    path = evidence_dir(root, evidence_id) / METADATA_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_recent(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Metadata of the most recently archived items, newest first."""
    records = []
    for path in (root / ARTIFACTS_DIR).glob(f"ev_*/{METADATA_FILE}"):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.warning("Skipping unreadable metadata in %s", path.parent.name)
    records.sort(key=lambda record: record.get("retrieved_at", ""), reverse=True)
    return records[:limit]


def save_new_evidence(
    root: Path, evidence_id: str, filename: str, content: bytes, metadata: dict[str, Any]
) -> bool:
    """Atomically create artifacts/<evidence_id>/ holding the artifact and metadata.json.

    Returns False, writing nothing, if this evidence ID is already stored.
    """
    final_dir = evidence_dir(root, evidence_id)
    if (final_dir / METADATA_FILE).exists():
        return False

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = root / STAGING_DIR
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{evidence_id}-", dir=staging_root))
    try:
        _write_file(staging / filename, content)
        _write_file(staging / METADATA_FILE, _to_json(metadata))  # metadata last
        try:
            staging.rename(final_dir)  # atomic: the folder appears complete, or not at all
        except OSError:
            if (final_dir / METADATA_FILE).exists():
                return False  # a parallel call stored the same bytes a moment earlier
            raise
        return True
    finally:
        if staging.exists():  # the rename did not happen: discard the partial copy
            shutil.rmtree(staging, ignore_errors=True)


def update_metadata(root: Path, evidence_id: str, metadata: dict[str, Any]) -> None:
    """Replace metadata.json atomically (write a temporary file, then rename it)."""
    path = evidence_dir(root, evidence_id) / METADATA_FILE
    temporary = path.with_name(METADATA_FILE + ".tmp")
    _write_file(temporary, _to_json(metadata))
    temporary.replace(path)


def safe_filename(suggested: str, content_type: str) -> str:
    """Turn an untrusted name (from a URL or a header) into a safe local file name.

    Only letters, digits, '.', '_' and '-' survive, so the result can never
    contain a path separator or '..' and cannot escape the evidence folder.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", suggested).strip("._")
    name = Path(cleaned or "source")
    suffix = name.suffix.lower() or mimetypes.guess_extension(content_type) or ".bin"
    result = f"{name.stem[:MAX_STEM_LENGTH]}{suffix[:10]}"
    if result.lower() in RESERVED_NAMES:  # never let a download overwrite our metadata
        result = f"source_{result}"
    return result


def _write_file(path: Path, data: bytes) -> None:
    with path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())  # make sure the bytes reach the disk before any rename


def _to_json(metadata: dict[str, Any]) -> bytes:
    return (json.dumps(metadata, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
