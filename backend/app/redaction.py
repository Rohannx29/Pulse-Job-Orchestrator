"""Data Sanitization & Secret Redaction Engine.

Ensures sensitive authentication tokens, signing secrets, passwords,
and credentials never leak through delivery inspection logs, audit trails,
or API responses.
"""

from __future__ import annotations

import re
from typing import Any, Optional

REDACTED_MARKER = "***REDACTED***"

SENSITIVE_KEY_NAMES = {
    "password",
    "pass",
    "secret",
    "secret_key",
    "signing_secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "private_key",
    "client_secret",
    "database_url",
}

# Regex patterns for string sanitization
PATTERNS = [
    # Authorization header: "Bearer <token>", "Token <token>"
    (re.compile(r"(?i)\b(bearer|token)\s+([a-zA-Z0-9_\-\.]+)\b"), r"\1 ***REDACTED***"),
    # Webhook secrets: "whsec_<hex>"
    (re.compile(r"\bwhsec_[a-zA-Z0-9_]{10,}\b"), r"whsec_***REDACTED***"),
    # JSON-like secret assignments: "password": "...", "token": "..."
    (
        re.compile(
            r'(?i)(["\']?(?:password|token|secret|access_token|refresh_token|api_key|client_secret)["\']?\s*[:=]\s*["\'])([^"\']+)(["\'])'
        ),
        r"\g<1>***REDACTED***\g<3>",
    ),
    # Connection strings with credentials: "scheme://user:pass@host"
    (
        re.compile(r"([a-zA-Z][a-zA-Z0-9+\-.]*://[^:@\s]+:)([^@\s]+)(@[^/\s]+)"),
        r"\g<1>***REDACTED***\g<3>",
    ),
]


def redact_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Sanitizes sensitive tokens, passwords, and secrets from arbitrary strings."""
    if not text or not isinstance(text, str):
        return text

    sanitized = text
    for regex, replacement in PATTERNS:
        sanitized = regex.sub(replacement, sanitized)
    return sanitized


def redact_sensitive_dict(data: Any) -> Any:
    """Recursively traverses dictionaries, lists, and primitives, redacting sensitive fields."""
    if isinstance(data, dict):
        sanitized_dict = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(sensitive in key_lower for sensitive in SENSITIVE_KEY_NAMES):
                sanitized_dict[key] = REDACTED_MARKER
            else:
                sanitized_dict[key] = redact_sensitive_dict(value)
        return sanitized_dict
    elif isinstance(data, list):
        return [redact_sensitive_dict(item) for item in data]
    elif isinstance(data, str):
        return redact_sensitive_text(data)
    else:
        return data
