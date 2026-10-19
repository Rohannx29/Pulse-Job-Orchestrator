"""Automated test proving Alembic migration round-trip: upgrade -> downgrade -> upgrade.

Verifies:
1. Named foreign keys and constraints drop cleanly.
2. Reversible schema evolution without orphaned artifacts.
3. Full round trip from base to head, back to base, and back to head.
"""

from __future__ import annotations

import os
import tempfile
import pytest

from alembic import command
from alembic.config import Config


def test_alembic_migration_round_trip():
    """Executes upgrade head -> downgrade base -> upgrade head."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/alembic")
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

        # 1. Upgrade from empty to head
        command.upgrade(cfg, "head")

        # 2. Downgrade from head back to base
        command.downgrade(cfg, "base")

        # 3. Upgrade from base to head again
        command.upgrade(cfg, "head")

    finally:
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
