"""Blocks the classic SSRF targets for any job handler that makes an
outbound HTTP call with a caller-supplied URL: loopback, link-local
(including cloud metadata endpoints at 169.254.169.254), RFC1918 private
ranges, and other reserved/non-public address space.

Deliberately simple, not a complete DNS-rebinding-proof implementation:
this resolves the hostname once and validates that address, but doesn't
pin the connection to the validated IP — a sufficiently adversarial DNS
server could in principle resolve differently between this check and the
actual connection a moment later. Documented as a known limitation rather
than silently assumed solved; closing it fully would mean resolving once
and handing the IP directly to httpx instead of the hostname, which is a
reasonable next step if this handler is ever exposed to untrusted callers
rather than the operator submitting their own jobs.
"""

import ipaddress
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


def validate_public_url(url: str) -> None:
    """Raises ValueError if the URL isn't safe to fetch server-side."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme {parsed.scheme!r} not allowed; only http/https")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")

    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname {parsed.hostname!r}: {exc}")

    for ip_str in resolved:
        ip = ipaddress.ip_address(ip_str)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"URL {url!r} resolves to a non-public address ({ip_str}) — blocked to prevent SSRF"
            )
