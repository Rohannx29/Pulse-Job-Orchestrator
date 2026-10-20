"""Resumable, idempotent data migration command to encrypt existing plaintext webhook secrets.

Operational semantics:
- Scans `webhook_subscriptions` for unencrypted secrets (lacking 'enc:v1:' prefix).
- Encrypts raw signing secrets in batches using authenticated AES-128 (Fernet) via KMS.
- Idempotent: re-running against an already-encrypted database performs 0 mutations.
- Resumable: if interrupted, subsequent runs continue with remaining unencrypted records.
- Observable: logs structured progress per batch and total migrated counts.

Rollout Guidance:
1. Ensure a full PostgreSQL backup is taken before execution (`pg_dump -Fc pulse > pulse_pre_kms_backup.dump`).
2. Verify WEBHOOK_ENCRYPTION_KEY is configured in the environment.
3. Run this command: `python -m app.commands.encrypt_webhook_secrets`.
4. Verify all webhook subscriptions are encrypted: `python -m app.commands.encrypt_webhook_secrets --dry-run`.
5. Rollback Guidance: In the event of KMS key corruption, restore database from backup or use previous key.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.kms import ENCRYPTION_PREFIX, encrypt_webhook_secret
from app.models import WebhookSubscription

logger = logging.getLogger("app.commands.encrypt_webhook_secrets")


def run_webhook_secret_encryption(
    db: Session,
    batch_size: int = 100,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Scans and encrypts unencrypted webhook secrets in manageable batches."""
    logger.info("Starting webhook secret encryption migration (batch_size=%d, dry_run=%s)", batch_size, dry_run)

    total_scanned = db.query(WebhookSubscription).count()
    already_encrypted = (
        db.query(WebhookSubscription)
        .filter(WebhookSubscription.secret.startswith(ENCRYPTION_PREFIX))
        .count()
    )
    unencrypted_remaining = total_scanned - already_encrypted

    logger.info(
        "Discovered %d total subscriptions (%d already encrypted, %d pending encryption)",
        total_scanned,
        already_encrypted,
        unencrypted_remaining,
    )

    if unencrypted_remaining == 0:
        logger.info("All webhook secrets are already encrypted at rest. Migration is up to date.")
        return {
            "total_scanned": total_scanned,
            "already_encrypted": already_encrypted,
            "migrated": 0,
            "status": "up_to_date",
        }

    migrated_count = 0

    while True:
        # Fetch next batch of unencrypted rows
        unencrypted_batch = (
            db.query(WebhookSubscription)
            .filter(~WebhookSubscription.secret.startswith(ENCRYPTION_PREFIX))
            .limit(batch_size)
            .all()
        )

        if not unencrypted_batch:
            break

        for sub in unencrypted_batch:
            sub.secret = encrypt_webhook_secret(sub.secret)
            if sub.previous_secret and not sub.previous_secret.startswith(ENCRYPTION_PREFIX):
                sub.previous_secret = encrypt_webhook_secret(sub.previous_secret)
            migrated_count += 1

        if not dry_run:
            db.commit()
            logger.info("Committed batch: encrypted %d subscriptions so far", migrated_count)
        else:
            db.rollback()
            logger.info("[Dry Run] Would encrypt batch of %d subscriptions", len(unencrypted_batch))
            break

    logger.info(
        "Webhook secret encryption completed: %d encrypted, %d were already encrypted.",
        migrated_count,
        already_encrypted,
    )

    return {
        "total_scanned": total_scanned,
        "already_encrypted": already_encrypted,
        "migrated": migrated_count,
        "status": "dry_run" if dry_run else "completed",
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Encrypt existing plaintext webhook secrets at rest via KMS.")
    parser.add_argument("--batch-size", type=int, default=100, help="Number of records to process per commit batch")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and report without mutating database")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        results = run_webhook_secret_encryption(db, batch_size=args.batch_size, dry_run=args.dry_run)
        print(f"Result: {results}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
