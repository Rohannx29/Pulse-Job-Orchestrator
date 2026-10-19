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
    with op.batch_alter_table('webhook_subscriptions') as batch_op:
        batch_op.alter_column(
            'secret',
            existing_type=sa.String(length=255),
            type_=sa.String(length=512),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column('previous_secret', sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column('previous_secret_expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('webhook_subscriptions') as batch_op:
        batch_op.drop_column('previous_secret_expires_at')
        batch_op.drop_column('previous_secret')
        batch_op.alter_column(
            'secret',
            existing_type=sa.String(length=512),
            type_=sa.String(length=255),
            existing_nullable=False,
        )
