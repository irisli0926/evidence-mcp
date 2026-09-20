"""The authenticated GitHub provenance index. Every GitHub response is mocked:
these tests never use a real token or a real repository."""

import base64
import json
import logging

import httpx
import pytest

from evidence_mcp import github_index, storage
from evidence_mcp.archive import archive_source
from evidence_mcp.errors import (
    REMOTE_AUTH_FAILED,
    REMOTE_RECORD_CONFLICT,
    REMOTE_SYNC_FAILED,
    EvidenceError,
)
from helpers import FAKE_TOKEN, PDF_BYTES, PDF_URL, FakeGitHub, FakeWeb

RECORD = {
    "evidence_id": "ev_0123456789abcdef",
    "original_url": "https://example.org/a.pdf",
    "sha256": "0123456789abcdef" + "0" * 48,
    "note": "test",
}


def decoded_record(request: httpx.Request) -> dict:
    body = json.loads(request.content)
    return json.loads(base64.b64decode(body["content"]))


# --- github_index.put_record ----------------------------------------------------------


def test_valid_authenticated_write_creates_one_record_file(github_settings):
    github = FakeGitHub(status=201)
    path = github_index.put_record(RECORD, github_settings, transport=github.transport)

    assert path == "records/ev_0123456789abcdef.json"
    [request] = github.requests
    assert request.method == "PUT"
    assert str(request.url) == (
        "https://api.github.com/repos/alice/private-evidence-index/contents/"
        "records/ev_0123456789abcdef.json"
    )
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["x-github-api-version"] == github_index.API_VERSION
    body = json.loads(request.content)
    assert body["branch"] == "main"
    assert "sha" not in body  # write-once: never overwrite an existing record
    assert decoded_record(request) == RECORD


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, REMOTE_AUTH_FAILED),  # bad / expired token
        (403, REMOTE_AUTH_FAILED),  # token lacks permission
        (404, REMOTE_AUTH_FAILED),  # private repo the token cannot see
        (409, REMOTE_RECORD_CONFLICT),  # record already exists
        (422, REMOTE_SYNC_FAILED),
        (500, REMOTE_SYNC_FAILED),
        (503, REMOTE_SYNC_FAILED),
    ],
)
def test_github_error_statuses_map_to_safe_codes(github_settings, caplog, status, code):
    caplog.set_level(logging.DEBUG)
    github = FakeGitHub(status=status)
    with pytest.raises(EvidenceError) as info:
        github_index.put_record(RECORD, github_settings, transport=github.transport)

    assert info.value.code == code
    assert FAKE_TOKEN not in str(info.value)
    assert FAKE_TOKEN not in caplog.text
    assert "Bearer" not in caplog.text


def test_existing_record_reported_as_422_sha_is_a_conflict(github_settings):
    github = FakeGitHub(status=422, message='Invalid request.\n\n"sha" wasn\'t supplied.')
    with pytest.raises(EvidenceError) as info:
        github_index.put_record(RECORD, github_settings, transport=github.transport)
    assert info.value.code == REMOTE_RECORD_CONFLICT


def test_network_failure_is_a_sync_failure(github_settings, caplog):
    caplog.set_level(logging.DEBUG)
    github = FakeGitHub(error=httpx.ConnectError("connection refused"))
    with pytest.raises(EvidenceError) as info:
        github_index.put_record(RECORD, github_settings, transport=github.transport)
    assert info.value.code == REMOTE_SYNC_FAILED
    assert FAKE_TOKEN not in caplog.text


def test_settings_repr_never_shows_the_token(github_settings):
    assert FAKE_TOKEN not in repr(github_settings)


# --- archive_source + GitHub: partial-failure semantics -----------------------------------


def archive(settings, github):
    web = FakeWeb({PDF_URL: (200, {"content-type": "application/pdf"}, PDF_BYTES)})
    return archive_source(
        PDF_URL, "note", settings, web_transport=web.transport, github_transport=github.transport
    )


def test_full_success_reports_local_and_remote(github_settings, fake_dns):
    github = FakeGitHub(status=201)
    result = archive(github_settings, github)

    assert result["status"] == "archived"
    assert result["local_saved"] is True
    assert result["remote_metadata_synced"] is True
    assert result["error_code"] is None

    remote = decoded_record(github.requests[0])
    assert remote["sha256"] == result["sha256"]
    assert remote["original_url"] == PDF_URL
    assert "remote_sync" not in remote
    assert PDF_BYTES not in base64.b64decode(json.loads(github.requests[0].content)["content"])

    local = storage.load_metadata(github_settings.evidence_root, result["evidence_id"])
    assert local["remote_sync"]["status"] == "synced"
    assert local["remote_sync"]["remote_path"] == f"records/{result['evidence_id']}.json"


def test_github_auth_failure_keeps_local_evidence(github_settings, fake_dns, caplog):
    caplog.set_level(logging.DEBUG)
    result = archive(github_settings, FakeGitHub(status=401))

    assert result["status"] == "archived_with_sync_warning"
    assert result["local_saved"] is True
    assert result["remote_metadata_synced"] is False
    assert result["error_code"] == REMOTE_AUTH_FAILED

    folder = github_settings.evidence_root / "artifacts" / result["evidence_id"]
    assert (folder / "study.pdf").read_bytes() == PDF_BYTES  # local evidence NOT deleted
    local = storage.load_metadata(github_settings.evidence_root, result["evidence_id"])
    assert local["remote_sync"]["status"] == "failed"
    assert local["remote_sync"]["error_code"] == REMOTE_AUTH_FAILED

    assert FAKE_TOKEN not in json.dumps(result)
    assert FAKE_TOKEN not in caplog.text


def test_archiving_again_only_resyncs_an_unsynced_record(github_settings, fake_dns):
    first = archive(github_settings, FakeGitHub(status=503))
    assert first["remote_metadata_synced"] is False

    working = FakeGitHub(status=201)
    second = archive(github_settings, working)
    assert second["already_archived"] is True
    assert second["remote_metadata_synced"] is True
    assert len(working.requests) == 1

    archive(github_settings, working)
    assert len(working.requests) == 1  # already synced: GitHub is not called again
