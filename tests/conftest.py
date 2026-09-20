import dataclasses
import functools
import ipaddress
import socket

import pytest
from mcp import Client

from evidence_mcp import archive, server
from evidence_mcp.config import Settings
from helpers import FAKE_TOKEN, PUBLIC_IP

REAL_ARCHIVE_SOURCE = archive.archive_source


@pytest.fixture
def anyio_backend():
    # Async MCP client tests run on asyncio (pattern from the SDK testing docs:
    # https://py.sdk.modelcontextprotocol.io/get-started/testing/).
    return "asyncio"


@pytest.fixture
def fake_dns(monkeypatch):
    """Replace real DNS with a small table so tests are fast and offline.

    IP literals resolve to themselves, unknown hostnames resolve to a public
    address, and tests can add entries (e.g. a hostname that points to 10.x).
    An empty list simulates a hostname that does not resolve.
    """
    table: dict[str, list[str]] = {"localhost": ["127.0.0.1", "::1"]}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ips = [str(ipaddress.ip_address(host))]
        except ValueError:
            ips = table.get(host, [PUBLIC_IP])
        if not ips:
            raise socket.gaierror("fake DNS: name does not resolve")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return table


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with GitHub sync NOT configured and a temporary evidence store."""
    return Settings(
        evidence_root=tmp_path / "evidence_store",
        max_file_size_bytes=1024 * 1024,  # 1 MB keeps the size tests quick
        http_timeout_seconds=5,
        max_redirects=3,
    )


@pytest.fixture
def github_settings(settings) -> Settings:
    """Settings with GitHub sync configured, using an obviously fake token."""
    return dataclasses.replace(
        settings,
        github_owner="alice",
        github_repo="private-evidence-index",
        github_branch="main",
        github_token=FAKE_TOKEN,
    )


@pytest.fixture
def serve(monkeypatch):
    """Point the MCP server at test settings and (optionally) fake network transports."""

    def configure(settings, web=None, github=None):
        monkeypatch.setattr(server, "get_settings", lambda: settings)
        monkeypatch.setattr(
            archive,
            "archive_source",
            functools.partial(
                REAL_ARCHIVE_SOURCE,
                web_transport=web.transport if web else None,
                github_transport=github.transport if github else None,
            ),
        )

    return configure


@pytest.fixture
async def client():
    """A real MCP client connected in memory to our server: it sees exactly what
    Claude Desktop would see (raise_exceptions=False, the default)."""
    async with Client(server.mcp) as mcp_client:
        yield mcp_client
