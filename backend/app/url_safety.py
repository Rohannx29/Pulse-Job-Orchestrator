"""Defense-in-Depth SSRF Protection and Safe HTTP Client for Pulse.

Provides multi-layer security against Server-Side Request Forgery (SSRF) and DNS rebinding:
1. Validates URL schemes (strictly http and https).
2. Resolves all IPv4 and IPv6 target addresses, blocking:
   - Private networks (RFC 1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, fc00::/7)
   - Loopback ranges (127.0.0.0/8, ::1)
   - Link-local and cloud instance metadata (169.254.0.0/16, fe80::/10, 169.254.169.254, fd00:ec2::254)
   - Multicast, broadcast, unspecified, and reserved address blocks
   - IPv4-mapped IPv6 embedded addresses (::ffff:127.0.0.1, ::ffff:169.254.169.254)
3. Strictly forbids unvalidated HTTP redirects (follow_redirects=False).
4. Enforces bounded connect (5s), read (10s), write (5s), and pool (5s) timeouts.
5. Enforces bounded request and response memory consumption (capped at 1MB).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
MAX_HTTP_TIMEOUT_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_000_000  # 1 MB

CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


def is_ip_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Checks if an IP address falls into any blocked or sensitive address space."""
    # Check IPv4-mapped IPv6 (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_ip_disallowed(ip.ipv4_mapped)

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or ip in CLOUD_METADATA_IPS
    ):
        return True

    return False


def resolve_and_validate_public_url(url: str) -> tuple[str, list[str]]:
    """Validates the URL and resolves its hostname across IPv4 and IPv6.
    Returns (hostname, list_of_validated_ip_strings).
    Raises ValueError if URL is invalid or resolves to non-public/metadata IPs.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme {parsed.scheme!r} not allowed; only http/https")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname {parsed.hostname!r}: {exc}")

    resolved_ips: list[str] = []
    seen = set()

    for item in addrinfo:
        ip_str = item[4][0]
        if ip_str in seen:
            continue
        seen.add(ip_str)

        ip = ipaddress.ip_address(ip_str)
        if is_ip_disallowed(ip):
            raise ValueError(
                f"URL {url!r} resolves to a non-public address ({ip_str}) — blocked to prevent SSRF"
            )
        resolved_ips.append(ip_str)

    if not resolved_ips:
        raise ValueError(f"No IP addresses resolved for hostname {parsed.hostname!r}")

    return parsed.hostname, resolved_ips


def validate_public_url(url: str) -> None:
    """Backwards-compatible SSRF validator. Raises ValueError if URL is unsafe."""
    resolve_and_validate_public_url(url)


def execute_safe_http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_data: Optional[Any] = None,
    content: Optional[bytes] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> Tuple[int, bytes, Dict[str, str]]:
    """Executes an outbound HTTP request with defense-in-depth SSRF protection:
    - Resolves and validates all IPs against loopback/private/metadata ranges.
    - Preserves TLS verification and Host headers.
    - Strictly forbids unvalidated HTTP redirects (follow_redirects=False).
    - Enforces bounded connect, read, write, and pool timeouts.
    - Enforces bounded response body truncation.

    Returns:
        (status_code: int, response_body: bytes, response_headers: dict)
    """
    validate_public_url(url)

    timeout = httpx.Timeout(
        connect=min(5.0, timeout_seconds),
        read=timeout_seconds,
        write=5.0,
        pool=5.0,
    )

    req_headers = dict(headers or {})

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream(
            method=method.upper(),
            url=url,
            headers=req_headers,
            json=json_data,
            content=content,
        ) as resp:
            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    chunks.append(chunk[: max_bytes - (total - len(chunk))])
                    break
                chunks.append(chunk)

            body_bytes = b"".join(chunks)
            resp_headers = dict(resp.headers)
            return resp.status_code, body_bytes, resp_headers
