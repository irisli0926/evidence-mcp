"""Exploit B: internal errors must not leak through the MCP interface.

These tests call the server through a real MCP client (in memory), so they
check exactly what Claude Desktop would receive.
"""

import json
import logging

import httpx
import pytest
from mcp import MCPError

from evidence_mcp import archive, storage
from evidence_mcp.errors import (
    EVIDENCE_NOT_FOUND,
    INTERNAL_ERROR,
    REMOTE_AUTH_FAILED,
    STORAGE_ERROR,
    UNSAFE_URL,
)
from helpers import FAKE_TOKEN, PDF_BYTES, PDF_URL, FakeGitHub, FakeWeb

pytestmark = pytest.mark.anyio

SENSITIVE_PATH = "/Users/example/private/evidence"
SECRET = "SUPER_SECRET_TEST_VALUE"


def visible_text(result) -> str:
    """Everything the MCP client receives for a tool call, as one string."""
    return json.dumps(result.model_dump(mode="json"))


def assert_nothing_leaked(text: str) -> None:
    assert SENSITIVE_PATH not in text  # no absolute path
    assert SECRET not in text  # no secret value
    assert "Traceback" not in text  # no stack trace
    assert "RuntimeError" not in text and "PermissionError" not in text  # no raw exception


async def test_unexpected_exception_is_not_disclosed(client, serve, settings, monkeypatch, caplog):
    serve(settings)

    def crash(*args, **kwargs):
        raise RuntimeError(f"cannot open {SENSITIVE_PATH}/index.db using key {SECRET}")

    monkeypatch.setattr(archive, "fetch", crash)
    result = await client.call_tool("archive_source", {"url": "https://example.org/report.pdf"})

    assert result.is_error
    text = visible_text(result)
    assert INTERNAL_ERROR in text
    assert_nothing_leaked(text)
    assert SECRET in caplog.text  # the detail is kept - but only in the local log


async def test_storage_error_is_sanitized(client, serve, settings, monkeypatch):
    serve(settings)
    monkeypatch.setattr(
        archive,
        "fetch",
        lambda url, settings, transport=None: archive.Download(
            PDF_BYTES, "application/pdf", url, "report.pdf"
        ),
    )

    def failing_save(*args, **kwargs):
        raise PermissionError(13, f"Permission denied: '{SENSITIVE_PATH}' token={SECRET}")

    monkeypatch.setattr(storage, "save_new_evidence", failing_save)
    result = await client.call_tool("archive_source", {"url": PDF_URL})

    assert result.is_error
    text = visible_text(result)
    assert STORAGE_ERROR in text
    assert_nothing_leaked(text)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8000/private", "file:///etc/passwd", "http://169.254.169.254/"]
)
async def test_unsafe_url_is_refused_through_mcp_without_any_request(
    client, serve, settings, monkeypatch, url
):
    serve(settings)

    def no_network(*args, **kwargs):
        raise AssertionError("a network request was attempted")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    result = await client.call_tool("archive_source", {"url": url})

    assert result.is_error
    assert UNSAFE_URL in visible_text(result)


async def test_github_auth_failure_is_a_sanitized_warning(
    client, serve, github_settings, fake_dns, caplog
):
    caplog.set_level(logging.DEBUG)
    web = FakeWeb({PDF_URL: (200, {"content-type": "application/pdf"}, PDF_BYTES)})
    serve(github_settings, web=web, github=FakeGitHub(status=401))

    result = await client.call_tool("archive_source", {"url": PDF_URL})

    assert not result.is_error  # local archival succeeded: a warning, not a failure
    assert result.structured_content["status"] == "archived_with_sync_warning"
    assert result.structured_content["local_saved"] is True
    assert result.structured_content["remote_metadata_synced"] is False
    assert result.structured_content["error_code"] == REMOTE_AUTH_FAILED
    text = visible_text(result)
    assert FAKE_TOKEN not in text and "Bearer" not in text
    assert str(github_settings.evidence_root) not in text  # no absolute local path
    assert FAKE_TOKEN not in caplog.text


async def test_missing_evidence_is_a_clean_not_found(client, serve, settings):
    serve(settings)
    with pytest.raises(MCPError) as info:
        await client.read_resource("evidence://artifact/ev_0000000000000000")
    assert EVIDENCE_NOT_FOUND in str(info.value)


async def test_resource_storage_error_is_sanitized(client, serve, settings, monkeypatch):
    serve(settings)

    def broken(*args, **kwargs):
        raise OSError(f"{SENSITIVE_PATH}/metadata.json is corrupt ({SECRET})")

    monkeypatch.setattr(storage, "load_metadata", broken)
    with pytest.raises(MCPError) as info:
        await client.read_resource("evidence://artifact/ev_0000000000000000")
    message = str(info.value)
    assert STORAGE_ERROR in message
    assert_nothing_leaked(message)
