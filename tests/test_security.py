"""Exploit A (unit level): SSRF / unsafe URLs are rejected before any request."""

import ipaddress

import pytest

from evidence_mcp import security
from evidence_mcp.errors import DOWNLOAD_FAILED, UNSAFE_URL, EvidenceError

UNSAFE_URLS = [
    # The examples from the assignment brief
    "file:///etc/passwd",
    "http://127.0.0.1:8000/admin",
    "http://127.0.0.1:8000/private",
    "http://localhost:3000",
    "http://169.254.169.254/",
    "http://192.168.1.1/",
    # Other private / special ranges
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.8/",
    "http://172.16.0.1/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",  # carrier-grade NAT (shared address space)
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4 loopback hidden inside IPv6
    "http://[fe80::1]/",
    # Other schemes
    "ftp://example.com/file.txt",
    "gopher://example.com/",
    "javascript:alert(1)",
    # Embedded credentials (including a backslash parser trick)
    "http://user:password@example.com/",
    "https://token@example.com/",
    "http://example.com\\@127.0.0.1/",
    # Malformed
    "http:///no-host",
    "not a url",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_urls_are_rejected(url, fake_dns):
    with pytest.raises(EvidenceError) as info:
        security.validate_url(url)
    assert info.value.code == UNSAFE_URL


def test_decimal_ip_trick_is_rejected():
    # 2130706433 == 127.0.0.1 written as one number. Uses the REAL resolver
    # (no network needed for a numeric host) to show resolution catches it.
    with pytest.raises(EvidenceError) as info:
        security.validate_url("http://2130706433/")
    assert info.value.code == UNSAFE_URL


def test_hostname_resolving_to_private_ip_is_rejected(fake_dns):
    fake_dns["intranet.example"] = ["10.1.2.3"]
    with pytest.raises(EvidenceError) as info:
        security.validate_url("https://intranet.example/report.pdf")
    assert info.value.code == UNSAFE_URL


def test_any_private_address_in_the_dns_answer_is_rejected(fake_dns):
    fake_dns["mixed.example"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(EvidenceError) as info:
        security.validate_url("https://mixed.example/")
    assert info.value.code == UNSAFE_URL


def test_unresolvable_hostname_is_a_download_failure(fake_dns):
    fake_dns["nowhere.example"] = []
    with pytest.raises(EvidenceError) as info:
        security.validate_url("https://nowhere.example/")
    assert info.value.code == DOWNLOAD_FAILED


def test_very_long_url_is_rejected():
    with pytest.raises(EvidenceError) as info:
        security.validate_url("https://example.com/" + "a" * 3000)
    assert info.value.code == UNSAFE_URL


@pytest.mark.parametrize(
    "url", ["https://example.org/paper.pdf", "HTTP://Example.org/data.csv", "http://93.184.216.34/x"]
)
def test_public_urls_are_allowed(url, fake_dns):
    security.validate_url(url)  # does not raise


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("::ffff:10.0.0.1", False),
        ("fd00::1", False),
    ],
)
def test_is_public_ip(ip, expected):
    assert security.is_public_ip(ipaddress.ip_address(ip)) is expected
