"""Unit tests for the SSRF guard. No network required — all tests use
predictable hostnames or pre-resolved IPs via monkeypatching."""

from __future__ import annotations

import socket

import pytest

from sila.core.security.ssrf_guard import (
    validate_external_url,
    validate_external_url_sync,
)


@pytest.fixture
def fake_resolve(monkeypatch: pytest.MonkeyPatch):
    """Replace socket.getaddrinfo with a dict-driven fake."""
    table: dict[str, list[str]] = {}

    def fake(host: str, *args: object, **kwargs: object) -> list[tuple]:
        if host not in table:
            raise socket.gaierror(f"no such host: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in table[host]]

    monkeypatch.setattr("socket.getaddrinfo", fake)
    return table


# ---- Scheme validation ------------------------------------------------------


def test_rejects_non_http_scheme(fake_resolve):
    with pytest.raises(ValueError, match="scheme"):
        validate_external_url_sync("file:///etc/passwd")
    with pytest.raises(ValueError, match="scheme"):
        validate_external_url_sync("gopher://example.com")


def test_rejects_empty_url(fake_resolve):
    with pytest.raises(ValueError):
        validate_external_url_sync("")
    with pytest.raises(ValueError):
        validate_external_url_sync(None)  # type: ignore[arg-type]


# ---- Private / loopback / link-local rejection ------------------------------


def test_rejects_loopback(fake_resolve):
    fake_resolve["evil.example"] = ["127.0.0.1"]
    with pytest.raises(ValueError, match="loopback"):
        validate_external_url_sync("https://evil.example/api")


def test_rejects_ipv6_loopback(fake_resolve):
    fake_resolve["evil.example"] = ["::1"]
    with pytest.raises(ValueError, match="loopback"):
        validate_external_url_sync("https://evil.example/api")


def test_rejects_private_10(fake_resolve):
    fake_resolve["internal.example"] = ["10.0.5.7"]
    with pytest.raises(ValueError, match="private"):
        validate_external_url_sync("https://internal.example/api")


def test_rejects_private_172(fake_resolve):
    fake_resolve["internal.example"] = ["172.20.1.1"]
    with pytest.raises(ValueError, match="private"):
        validate_external_url_sync("https://internal.example/api")


def test_rejects_private_192_168(fake_resolve):
    fake_resolve["router.example"] = ["192.168.1.1"]
    with pytest.raises(ValueError, match="private"):
        validate_external_url_sync("http://router.example")


def test_rejects_link_local(fake_resolve):
    fake_resolve["x.example"] = ["169.254.42.42"]
    with pytest.raises(ValueError):
        validate_external_url_sync("https://x.example/")


# ---- Cloud metadata --------------------------------------------------------


def test_rejects_aws_metadata(fake_resolve):
    fake_resolve["meta.example"] = ["169.254.169.254"]
    with pytest.raises(ValueError, match="cloud-metadata"):
        validate_external_url_sync("http://meta.example/latest/")


def test_rejects_alibaba_metadata(fake_resolve):
    fake_resolve["a.example"] = ["100.100.100.200"]
    with pytest.raises(ValueError, match="cloud-metadata"):
        validate_external_url_sync("http://a.example/")


def test_rejects_direct_ip_literal_metadata(fake_resolve):
    fake_resolve["169.254.169.254"] = ["169.254.169.254"]
    with pytest.raises(ValueError):
        validate_external_url_sync("http://169.254.169.254/")


# ---- Public addresses pass --------------------------------------------------


def test_accepts_public_ipv4(fake_resolve):
    fake_resolve["api.example.com"] = ["93.184.216.34"]
    out = validate_external_url_sync("https://api.example.com/v1/tools")
    assert out == "https://api.example.com/v1/tools"


def test_normalizes_path(fake_resolve):
    fake_resolve["api.example.com"] = ["93.184.216.34"]
    out = validate_external_url_sync("https://api.example.com")
    assert out.endswith("/")


# ---- Hostname allow-list bypass --------------------------------------------


def test_allowlist_bypasses_ip_check(fake_resolve):
    # sila-mcp-* sidecars resolve to private Docker-network IPs by design
    fake_resolve["sila-mcp-fetch"] = ["172.19.0.5"]
    # Without allowlist: rejected
    with pytest.raises(ValueError, match="private"):
        validate_external_url_sync("http://sila-mcp-fetch:3333/")
    # With allowlist: accepted
    out = validate_external_url_sync(
        "http://sila-mcp-fetch:3333/", allow_hostnames=frozenset({"sila-mcp-fetch"})
    )
    assert out == "http://sila-mcp-fetch:3333/"


def test_allowlist_does_not_bypass_other_hostnames(fake_resolve):
    fake_resolve["evil.example"] = ["127.0.0.1"]
    with pytest.raises(ValueError):
        validate_external_url_sync(
            "http://evil.example/", allow_hostnames=frozenset({"sila-mcp-fetch"})
        )


# ---- DNS failure ------------------------------------------------------------


def test_unresolvable_hostname_rejected(fake_resolve):
    with pytest.raises(ValueError, match="resolve"):
        validate_external_url_sync("https://does-not-exist.invalid/")


# ---- Async variant ----------------------------------------------------------


@pytest.mark.asyncio
async def test_async_variant_accepts_public(fake_resolve):
    fake_resolve["api.example.com"] = ["93.184.216.34"]
    out = await validate_external_url("https://api.example.com/")
    assert out == "https://api.example.com/"


@pytest.mark.asyncio
async def test_async_variant_rejects_private(fake_resolve):
    fake_resolve["evil.example"] = ["10.0.0.1"]
    with pytest.raises(ValueError):
        await validate_external_url("https://evil.example/")
