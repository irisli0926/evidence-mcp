"""archive_source: turn one public URL into one durable, auditable evidence item.

The steps, in order (each is a small function below):

  1. validate the URL                         security.validate_url   (no network yet)
  2. download it, re-validating redirects     fetch()                 (public web)
  3. enforce the size limit while streaming   _read_body()
  4. hash the bytes, derive the evidence ID   hashlib + storage.evidence_id_for
  5. save artifact + metadata.json locally    storage.save_new_evidence (atomic)
  6. push the metadata to the GitHub index    github_index.put_record  (authenticated)
  7. record the sync outcome locally          storage.update_metadata

Local storage is canonical. If step 6 fails, the local evidence is kept and
the result says so honestly (status "archived_with_sync_warning").
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx

# pydantic (which the MCP SDK uses to build the tool's output schema) requires
# typing_extensions.TypedDict instead of typing.TypedDict on Python < 3.12.
from typing_extensions import TypedDict

from evidence_mcp import USER_AGENT, github_index, security, storage
from evidence_mcp.config import Settings
from evidence_mcp.errors import (
    DOWNLOAD_FAILED,
    FILE_TOO_LARGE,
    REMOTE_NOT_CONFIGURED,
    STORAGE_ERROR,
    UNSUPPORTED_RESPONSE,
    EvidenceError,
)

logger = logging.getLogger(__name__)

REDIRECT_CODES = {301, 302, 303, 307, 308}


class ArchiveResult(TypedDict):
    """The small structured result returned to the MCP client."""

    status: Literal["archived", "archived_with_sync_warning"]
    evidence_id: str
    sha256: str
    filename: str
    content_type: str
    size_bytes: int
    retrieved_at: str
    original_url: str
    already_archived: bool
    local_saved: bool
    remote_metadata_synced: bool
    error_code: str | None


@dataclass
class Download:
    content: bytes
    content_type: str
    final_url: str
    suggested_filename: str


def archive_source(
    url: str,
    note: str,
    settings: Settings,
    *,
    web_transport: httpx.BaseTransport | None = None,
    github_transport: httpx.BaseTransport | None = None,
) -> ArchiveResult:
    """Archive ONE public http(s) source. Raises EvidenceError on failure.

    The two transport arguments exist only so tests can plug in a fake web and
    a fake GitHub API. In normal use they are None and the real network is used.
    """
    url = url.strip()
    download = fetch(url, settings, transport=web_transport)  # steps 1-3

    sha256 = hashlib.sha256(download.content).hexdigest()  # step 4
    evidence_id = storage.evidence_id_for(sha256)

    existing = _load_existing(settings, evidence_id)
    if existing is not None and existing.get("sha256") != sha256:
        # Two different files sharing a 64-bit ID prefix: practically impossible,
        # but refuse rather than silently mix up two pieces of evidence.
        logger.error("Evidence ID collision for %s", evidence_id)
        raise EvidenceError(STORAGE_ERROR, "The evidence could not be saved to local storage.")

    if existing is None:
        metadata = _new_metadata(evidence_id, sha256, url, note, download)
        created = _save_locally(settings, metadata, download.content)  # step 5
        if not created:  # a parallel call stored identical bytes first
            metadata = _load_existing(settings, evidence_id) or metadata
        already_archived = not created
    else:
        metadata, already_archived = existing, True  # identical bytes: no duplicate copy

    metadata = _sync_to_github(settings, metadata, github_transport)  # steps 6-7
    return _result(metadata, already_archived)


# --- steps 1-3: safe download ---------------------------------------------------


def fetch(url: str, settings: Settings, transport: httpx.BaseTransport | None = None) -> Download:
    """Download `url`, validating every redirect hop and capping the size."""
    client = httpx.Client(
        transport=transport,
        follow_redirects=False,  # we follow redirects ourselves, so every hop is validated
        timeout=settings.http_timeout_seconds,
        trust_env=False,  # ignore proxy environment variables and ~/.netrc credentials
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    current = url
    with client:
        for _ in range(settings.max_redirects + 1):
            security.validate_url(current)  # the ORIGINAL url and every redirect target
            try:
                with client.stream("GET", current) as response:
                    if response.status_code in REDIRECT_CODES:
                        current = _redirect_target(response)
                        continue
                    return _read_body(response, settings)
            except httpx.TimeoutException:
                logger.warning("Timed out downloading %r", current)
                raise EvidenceError(DOWNLOAD_FAILED, "The request timed out.") from None
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                logger.warning("Download of %r failed: %r", current, exc)
                raise EvidenceError(DOWNLOAD_FAILED, "The source could not be downloaded.") from None
    raise EvidenceError(DOWNLOAD_FAILED, "The source redirected too many times.")


def _redirect_target(response: httpx.Response) -> str:
    location = response.headers.get("location")
    if not location:
        raise EvidenceError(UNSUPPORTED_RESPONSE, "The server sent a redirect without a target.")
    return str(response.url.join(location))  # resolves relative redirects like "/file.pdf"


def _read_body(response: httpx.Response, settings: Settings) -> Download:
    status = response.status_code
    if status >= 400:
        raise EvidenceError(DOWNLOAD_FAILED, f"The server responded with HTTP {status}.")
    if status != 200:
        raise EvidenceError(UNSUPPORTED_RESPONSE, f"An HTTP {status} response cannot be archived.")

    limit = settings.max_file_size_bytes
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        # Cheap early exit. The header can be missing or lie, so we also count below.
        raise EvidenceError(FILE_TOO_LARGE, "The source is larger than the configured size limit.")

    chunks, received = [], 0
    for chunk in response.iter_bytes():
        received += len(chunk)
        if received > limit:
            # Stop reading now; leaving the `with client.stream(...)` block closes
            # the connection, and nothing has been written to disk yet.
            raise EvidenceError(FILE_TOO_LARGE, "The source is larger than the configured size limit.")
        chunks.append(chunk)

    content = b"".join(chunks)
    if not content:
        raise EvidenceError(UNSUPPORTED_RESPONSE, "The source returned an empty body.")
    return Download(
        content=content,
        content_type=_content_type(response),
        final_url=str(response.url),
        suggested_filename=_suggested_filename(response),
    )


def _content_type(response: httpx.Response) -> str:
    value = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return value[:100] or "application/octet-stream"


def _suggested_filename(response: httpx.Response) -> str:
    """The name the source suggests: Content-Disposition first, else the URL path."""
    disposition = response.headers.get("content-disposition")
    if disposition:
        # The standard-library email parser understands filename="..." and filename*=UTF-8''...
        header = Message()
        header["content-disposition"] = disposition
        if name := header.get_filename():
            return name
    return PurePosixPath(response.url.path).name  # httpx has already URL-decoded the path


# --- steps 4-5: local evidence ----------------------------------------------------


def _new_metadata(
    evidence_id: str, sha256: str, url: str, note: str, download: Download
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "original_url": url,
        "final_url": download.final_url,
        "retrieved_at": _now(),
        "sha256": sha256,
        "filename": storage.safe_filename(download.suggested_filename, download.content_type),
        "original_filename": download.suggested_filename[:255],
        "content_type": download.content_type,
        "size_bytes": len(download.content),
        "note": note.strip(),
        "remote_sync": {"status": "pending"},
    }


def _load_existing(settings: Settings, evidence_id: str) -> dict[str, Any] | None:
    try:
        return storage.load_metadata(settings.evidence_root, evidence_id)
    except (OSError, ValueError):
        logger.exception("Could not read metadata for %s", evidence_id)
        raise EvidenceError(STORAGE_ERROR, "The local evidence store could not be read.") from None


def _save_locally(settings: Settings, metadata: dict[str, Any], content: bytes) -> bool:
    try:
        return storage.save_new_evidence(
            settings.evidence_root, metadata["evidence_id"], metadata["filename"], content, metadata
        )
    except OSError:
        logger.exception("Could not save evidence %s", metadata["evidence_id"])
        raise EvidenceError(STORAGE_ERROR, "The evidence could not be saved to local storage.") from None


# --- steps 6-7: authenticated remote provenance record ---------------------------


def _sync_to_github(
    settings: Settings, metadata: dict[str, Any], transport: httpx.BaseTransport | None
) -> dict[str, Any]:
    """Push the provenance record to GitHub, then record the outcome locally."""
    if metadata.get("remote_sync", {}).get("status") == "synced":
        return metadata  # already in the remote index: nothing to do

    if not settings.github_configured:
        outcome: dict[str, Any] = {"status": "not_configured", "error_code": REMOTE_NOT_CONFIGURED}
    else:
        record = {key: value for key, value in metadata.items() if key != "remote_sync"}
        try:
            remote_path = github_index.put_record(record, settings, transport=transport)
            outcome = {"status": "synced", "error_code": None, "remote_path": remote_path}
        except EvidenceError as exc:
            # No retry, no queue: the failure is recorded and reported honestly.
            outcome = {"status": "failed", "error_code": exc.code}
    outcome["attempted_at"] = _now()

    metadata = {**metadata, "remote_sync": outcome}
    try:
        storage.update_metadata(settings.evidence_root, metadata["evidence_id"], metadata)
    except OSError:
        # The evidence itself is safely stored; only the recorded sync status is stale.
        logger.exception("Could not record the sync outcome for %s", metadata["evidence_id"])
    return metadata


def _result(metadata: dict[str, Any], already_archived: bool) -> ArchiveResult:
    sync = metadata["remote_sync"]
    synced = sync["status"] == "synced"
    return ArchiveResult(
        status="archived" if synced else "archived_with_sync_warning",
        evidence_id=metadata["evidence_id"],
        sha256=metadata["sha256"],
        filename=metadata["filename"],
        content_type=metadata["content_type"],
        size_bytes=metadata["size_bytes"],
        retrieved_at=metadata["retrieved_at"],
        original_url=metadata["original_url"],
        already_archived=already_archived,
        local_saved=True,
        remote_metadata_synced=synced,
        error_code=None if synced else sync.get("error_code"),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
