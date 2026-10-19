"""harden webhook secrets with encryption and rotation grace window

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-20 02:22:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Expand secret column length to accommodate KMS Fernet ciphertext tokens
    op.alter_column(
        'webhook_subscriptions',
        'secret',
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=False,
    )

    # 2. Add previous_secret and expiration for rotation grace window
    op.add_column(
        'webhook_subscriptions',
        sa.Column('previous_secret', sa.String(length=512), nullable=True),
    )
    op.add_column(
        'webhook_subscriptions',
        sa.Column('previous_secret_expires_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('webhook_subscriptions', 'previous_secret_expires_at')
    op.drop_column('webhook_subscriptions', 'previous_secret')
    op.alter_column(
        'webhook_subscriptions',
        'secret',
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
