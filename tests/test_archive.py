"""archive_source end to end against a fake web (no real network, GitHub not configured)."""

import hashlib
import json

import httpx
import pytest

from evidence_mcp import storage
from evidence_mcp.archive import archive_source
from evidence_mcp.errors import (
    DOWNLOAD_FAILED,
    FILE_TOO_LARGE,
    REMOTE_NOT_CONFIGURED,
    STORAGE_ERROR,
    UNSAFE_URL,
    UNSUPPORTED_RESPONSE,
    EvidenceError,
)
from helpers import PDF_BYTES, PDF_URL, FakeWeb


def artifact_folders(settings) -> list[str]:
    return sorted(p.name for p in (settings.evidence_root / "artifacts").glob("ev_*"))


def staging_leftovers(settings) -> list:
    staging = settings.evidence_root / "tmp"
    return list(staging.iterdir()) if staging.exists() else []


def archive(url, settings, web, note=""):
    return archive_source(url, note, settings, web_transport=web.transport)


# --- happy path -------------------------------------------------------------------


def test_archives_one_source_with_provenance(settings, fake_dns):
    web = FakeWeb({PDF_URL: (200, {"content-type": "application/pdf"}, PDF_BYTES)})
    result = archive(PDF_URL, settings, web, note="Primary source for claim 3")

    sha = hashlib.sha256(PDF_BYTES).hexdigest()
    assert result["evidence_id"] == f"ev_{sha[:16]}"
    assert result["sha256"] == sha
    assert result["filename"] == "study.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["size_bytes"] == len(PDF_BYTES)
    assert result["local_saved"] is True
    assert result["already_archived"] is False

    folder = settings.evidence_root / "artifacts" / result["evidence_id"]
    assert (folder / "study.pdf").read_bytes() == PDF_BYTES  # exact original bytes
    metadata = json.loads((folder / "metadata.json").read_text())
    assert metadata["original_url"] == PDF_URL
    assert metadata["note"] == "Primary source for claim 3"
    assert metadata["sha256"] == sha
    assert staging_leftovers(settings) == []


def test_without_github_config_the_result_is_an_honest_warning(settings, fake_dns):
    web = FakeWeb({PDF_URL: (200, {}, PDF_BYTES)})
    result = archive(PDF_URL, settings, web)
    assert result["status"] == "archived_with_sync_warning"
    assert result["remote_metadata_synced"] is False
    assert result["error_code"] == REMOTE_NOT_CONFIGURED


def test_identical_content_is_stored_only_once(settings, fake_dns):
    mirror = "https://mirror.example/copy-of-study.pdf"
    web = FakeWeb({PDF_URL: (200, {}, PDF_BYTES), mirror: (200, {}, PDF_BYTES)})
    first = archive(PDF_URL, settings, web)
    second = archive(mirror, settings, web)

    assert second["evidence_id"] == first["evidence_id"]
    assert second["already_archived"] is True
    assert second["original_url"] == PDF_URL  # the existing record is returned
    assert artifact_folders(settings) == [first["evidence_id"]]


def test_filename_comes_from_content_disposition(settings, fake_dns):
    url = "https://data.example/download?id=42"
    headers = {
        "content-type": "text/csv",
        "content-disposition": 'attachment; filename="survey results 2026.csv"',
    }
    web = FakeWeb({url: (200, headers, b"a,b\n1,2\n")})
    assert archive(url, settings, web)["filename"] == "survey_results_2026.csv"


@pytest.mark.parametrize(
    ("suggested", "content_type", "expected"),
    [
        ("report.pdf", "application/pdf", "report.pdf"),
        ("../../etc/passwd", "text/plain", "etc_passwd.txt"),  # path traversal neutralised
        ("", "text/html", "source.html"),
        ("metadata.json", "application/json", "source_metadata.json"),  # reserved name
        ("weird name (final).PDF", "application/pdf", "weird_name_final_.pdf"),
    ],
)
def test_safe_filename(suggested, content_type, expected):
    assert storage.safe_filename(suggested, content_type) == expected


# --- redirects ----------------------------------------------------------------------


def test_redirect_to_a_public_url_is_followed(settings, fake_dns):
    doi = "https://doi.example/10.1234/abc"
    web = FakeWeb(
        {
            doi: (302, {"location": PDF_URL}, b""),
            PDF_URL: (200, {"content-type": "application/pdf"}, PDF_BYTES),
        }
    )
    result = archive(doi, settings, web)
    metadata = storage.load_metadata(settings.evidence_root, result["evidence_id"])
    assert metadata["original_url"] == doi
    assert metadata["final_url"] == PDF_URL
    assert web.requests == [doi, PDF_URL]


def test_redirect_to_cloud_metadata_endpoint_is_blocked(settings, fake_dns):
    start = "https://innocent.example/article"
    web = FakeWeb({start: (302, {"location": "http://169.254.169.254/latest/meta-data/"}, b"")})
    with pytest.raises(EvidenceError) as info:
        archive(start, settings, web)
    assert info.value.code == UNSAFE_URL
    assert web.requests == [start]  # the metadata endpoint was never contacted


def test_redirect_limit_is_enforced(settings, fake_dns):
    loop = "https://loop.example/a"
    web = FakeWeb({loop: (302, {"location": loop}, b"")})
    with pytest.raises(EvidenceError) as info:
        archive(loop, settings, web)
    assert info.value.code == DOWNLOAD_FAILED
    assert len(web.requests) == settings.max_redirects + 1


# --- unsafe input and bad responses ----------------------------------------------------


def test_unsafe_url_is_refused_before_any_request(settings, fake_dns):
    web = FakeWeb()
    with pytest.raises(EvidenceError) as info:
        archive("http://127.0.0.1:8000/private", settings, web)
    assert info.value.code == UNSAFE_URL
    assert web.requests == []


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, DOWNLOAD_FAILED), (500, DOWNLOAD_FAILED), (204, UNSUPPORTED_RESPONSE)],
)
def test_non_200_responses_are_not_archived(settings, fake_dns, status, code):
    web = FakeWeb({PDF_URL: (status, {}, b"")})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)
    assert info.value.code == code
    assert artifact_folders(settings) == []


def test_empty_body_is_not_archived(settings, fake_dns):
    web = FakeWeb({PDF_URL: (200, {}, b"")})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)
    assert info.value.code == UNSUPPORTED_RESPONSE


def test_timeout_is_a_download_failure(settings, fake_dns):
    web = FakeWeb({PDF_URL: httpx.ReadTimeout("server too slow")})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)
    assert info.value.code == DOWNLOAD_FAILED


# --- file-size protection ----------------------------------------------------------------


def test_declared_content_length_over_limit_is_refused(settings, fake_dns):
    too_big = settings.max_file_size_bytes + 1
    web = FakeWeb({PDF_URL: (200, {"content-length": str(too_big)}, b"x" * too_big)})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)
    assert info.value.code == FILE_TOO_LARGE
    assert artifact_folders(settings) == []


def test_lying_content_length_is_not_trusted(settings, fake_dns):
    body = b"x" * (settings.max_file_size_bytes + 10)
    web = FakeWeb({PDF_URL: (200, {"content-length": "10"}, body)})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)
    assert info.value.code == FILE_TOO_LARGE


def test_streamed_body_over_limit_stops_downloading(settings, fake_dns):
    chunk = b"x" * (256 * 1024)
    sent = {"chunks": 0}

    def endless_body():  # no Content-Length: the size is unknown up front
        while True:
            sent["chunks"] += 1
            yield chunk

    web = FakeWeb({PDF_URL: (200, {}, endless_body)})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)

    assert info.value.code == FILE_TOO_LARGE
    # 1 MB limit / 256 KB chunks = 4 chunks; we stop on the first one past the limit.
    assert sent["chunks"] == settings.max_file_size_bytes // len(chunk) + 1
    assert artifact_folders(settings) == []
    assert staging_leftovers(settings) == []


# --- failure-safe storage ------------------------------------------------------------------


def test_interrupted_write_leaves_no_partial_evidence(settings, fake_dns, monkeypatch):
    real_write = storage._write_file

    def write_that_fails_on_metadata(path, data):
        if path.name == "metadata.json":
            raise OSError("simulated disk full")
        real_write(path, data)

    monkeypatch.setattr(storage, "_write_file", write_that_fails_on_metadata)
    web = FakeWeb({PDF_URL: (200, {}, PDF_BYTES)})
    with pytest.raises(EvidenceError) as info:
        archive(PDF_URL, settings, web)

    assert info.value.code == STORAGE_ERROR
    assert artifact_folders(settings) == []  # no folder claims to be evidence
    assert staging_leftovers(settings) == []  # and the half-written copy was removed
