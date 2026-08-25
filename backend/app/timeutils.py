"""Replacement for the deprecated datetime.utcnow().

Returns a NAIVE datetime representing UTC — deliberately, to match every
existing DateTime column in this project (none use timezone=True), so
adopting this everywhere is a pure refactor with zero behavior or schema
change. Genuinely timezone-aware storage would mean an Alembic migration
across every timestamp column in the schema (created_at, started_at,
finished_at, expires_at, and more, on nearly every table) — not worth the
risk to fix what is currently a cosmetic deprecation warning, not an
actual bug. Python has deprecated datetime.utcnow(), not removed it.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
