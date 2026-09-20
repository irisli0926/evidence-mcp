"""SSRF protection: decide whether a URL is safe for this server to fetch.

The URL given to archive_source comes from the model, so it is untrusted input.
This server runs on your machine, inside your network. Without these checks a
confused or prompt-injected model could make it fetch things such as
http://127.0.0.1:8000/admin or the cloud metadata endpoint 169.254.169.254.

Rules, all checked BEFORE any request is sent:
  1. the scheme must be http or https (no file://, ftp://, gopher://, ...);
  2. the URL must not contain a username or password;
  3. the hostname is resolved, and EVERY IP address it resolves to must be
     public (not loopback, private, link-local, reserved, multicast, ...).

archive.py runs validate_url() again on every redirect target.

Known limitation (documented in the README): we resolve DNS here, and httpx
resolves it again when it connects. A hostile DNS server could answer
differently the second time ("DNS rebinding"). Closing that gap fully would
mean pinning the checked IP address inside the HTTP connection, which is out
of scope for v1.
"""

import ipaddress
import logging
import socket

import httpx

from evidence_mcp.errors import DOWNLOAD_FAILED, UNSAFE_URL, EvidenceError

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
MAX_URL_LENGTH = 2048

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def validate_url(url: str) -> None:
    """Raise EvidenceError(UNSAFE_URL) unless `url` is a public http(s) URL."""
    if len(url) > MAX_URL_LENGTH:
        raise EvidenceError(UNSAFE_URL, "The URL is too long.")

    # Parse with httpx, the same parser that will send the request, so the host
    # we check is exactly the host we later connect to.
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise EvidenceError(UNSAFE_URL, "The URL is malformed.") from None

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise EvidenceError(UNSAFE_URL, "Only http:// and https:// URLs can be archived.")
    if parsed.userinfo:
        raise EvidenceError(UNSAFE_URL, "URLs containing a username or password are not allowed.")
    if not parsed.raw_host:
        raise EvidenceError(UNSAFE_URL, "The URL has no hostname.")

    host = parsed.raw_host.decode("ascii")  # the exact (punycode) host httpx will connect to
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    for address in resolve_host(host, port):
        if not is_public_ip(address):
            logger.warning("Blocked %r: host resolves to non-public address %s", url, address)
            raise EvidenceError(
                UNSAFE_URL, "The URL points to a local, private, or reserved network address."
            )


def resolve_host(host: str, port: int) -> list[IPAddress]:
    """Return every IP address `host` resolves to (an IP literal resolves to itself)."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise EvidenceError(DOWNLOAD_FAILED, "The hostname could not be resolved.") from None
    addresses = [ipaddress.ip_address(sockaddr[0]) for *_, sockaddr in results]
    if not addresses:  # fail closed: "no answer" must never mean "allowed"
        raise EvidenceError(DOWNLOAD_FAILED, "The hostname could not be resolved.")
    return addresses


def is_public_ip(address: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    # "::ffff:127.0.0.1" is really the IPv4 address 127.0.0.1 in IPv6 clothing.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    if (
        address.is_loopback  # 127.0.0.0/8, ::1
        or address.is_private  # 10/8, 172.16/12, 192.168/16, fc00::/7, ...
        or address.is_link_local  # 169.254/16 (cloud metadata), fe80::/10
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified  # 0.0.0.0, ::
    ):
        return False
    # Catch-all for special ranges not covered above, e.g. 100.64.0.0/10.
    return address.is_global
