"""
SSRF protection for MCP server URLs and outbound HTTP tool destinations.

Public entry point: `validate_external_url(url, *, allow_hostnames=None) -> str`
- Validates the URL structure
- Resolves the hostname to IP address(es)
- Rejects loopback, private, link-local, multicast, and cloud-metadata ranges
- Hostnames in `allow_hostnames` bypass the IP check (for trusted Docker-network
  services like sila-mcp-* sidecars). Used by the built-in registration path.

Returns the normalized URL string (scheme + netloc + path).
Raises ValueError on any rejection — caller maps to HTTPException 400.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud metadata IPs that must never be reachable from MCP server URLs.
# Even though most are covered by the link-local check, they're listed
# explicitly so the rejection message is clear and future ranges can be
# extended without touching the netmask logic.
CLOUD_METADATA_IPS = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, OpenStack
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IPv6
    }
)


def _is_disallowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> tuple[bool, str]:
    """Return (rejected, reason). True means the IP is not allowed."""
    if str(ip) in CLOUD_METADATA_IPS:
        return True, "cloud-metadata address"
    if ip.is_loopback:
        return True, "loopback address"
    if ip.is_link_local:
        return True, "link-local address"
    if ip.is_private:
        return True, "private address"
    if ip.is_multicast:
        return True, "multicast address"
    if ip.is_unspecified:
        return True, "unspecified address (0.0.0.0 / ::)"
    if ip.is_reserved:
        return True, "reserved address"
    return False, ""


def _resolve_all(hostname: str) -> list[str]:
    """Synchronous DNS resolution. Returns all A and AAAA records.

    Raises socket.gaierror if hostname doesn't resolve.
    """
    infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return list({str(info[4][0]) for info in infos})


async def validate_external_url(url: str, *, allow_hostnames: frozenset[str] | None = None) -> str:
    """Validate URL and return a normalized form. Raises ValueError on rejection.

    `allow_hostnames` is a set of hostnames (exact match) that bypass the IP
    range check. Used internally to permit `sila-mcp-*` Docker-network
    sidecars when they are registered as built-in tools by the service.

    Complexity: O(1) — single DNS resolve + constant-time IP range checks.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL is empty or not a string")

    parts = urlsplit(url.strip())
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme must be http or https (got {parts.scheme!r})")

    hostname = parts.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    # Built-in / known-safe service hostnames bypass IP check.
    if allow_hostnames and hostname in allow_hostnames:
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))

    # Resolve. asyncio.to_thread keeps the event loop responsive on slow DNS.
    try:
        ips = await asyncio.to_thread(_resolve_all, hostname)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname {hostname!r}: {e}") from e

    if not ips:
        raise ValueError(f"Hostname {hostname!r} resolved to no addresses")

    for ip_str in ips:
        ip = ipaddress.ip_address(ip_str)
        rejected, reason = _is_disallowed_ip(ip)
        if rejected:
            raise ValueError(f"URL {url!r} resolves to a {reason} ({ip_str}); not permitted")

    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def validate_external_url_sync(url: str, *, allow_hostnames: frozenset[str] | None = None) -> str:
    """Sync variant for use in startup hooks and tests."""
    if not url or not isinstance(url, str):
        raise ValueError("URL is empty or not a string")

    parts = urlsplit(url.strip())
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme must be http or https (got {parts.scheme!r})")

    hostname = parts.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    if allow_hostnames and hostname in allow_hostnames:
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))

    try:
        ips = _resolve_all(hostname)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname {hostname!r}: {e}") from e

    if not ips:
        raise ValueError(f"Hostname {hostname!r} resolved to no addresses")

    for ip_str in ips:
        ip = ipaddress.ip_address(ip_str)
        rejected, reason = _is_disallowed_ip(ip)
        if rejected:
            raise ValueError(f"URL {url!r} resolves to a {reason} ({ip_str}); not permitted")

    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
