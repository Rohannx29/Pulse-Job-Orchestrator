"""add webhook subscriptions and deliveries tables

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-20 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'webhook_subscriptions',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('org_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('secret', sa.String(length=255), nullable=False),
        sa.Column('events', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_webhook_subscriptions_org_id'), 'webhook_subscriptions', ['org_id'], unique=False)

    op.create_table(
        'webhook_deliveries',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('subscription_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('event', sa.String(length=100), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.Column('response_body', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('duration_ms', sa.Float(), nullable=True),
        sa.Column('is_success', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('delivered_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['webhook_subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_webhook_deliveries_subscription_id'), 'webhook_deliveries', ['subscription_id'], unique=False)
    op.create_index(op.f('ix_webhook_deliveries_event'), 'webhook_deliveries', ['event'], unique=False)
    op.create_index(op.f('ix_webhook_deliveries_delivered_at'), 'webhook_deliveries', ['delivered_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_webhook_deliveries_delivered_at'), table_name='webhook_deliveries')
    op.drop_index(op.f('ix_webhook_deliveries_event'), table_name='webhook_deliveries')
    op.drop_index(op.f('ix_webhook_deliveries_subscription_id'), table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')

    op.drop_index(op.f('ix_webhook_subscriptions_org_id'), table_name='webhook_subscriptions')
    op.drop_table('webhook_subscriptions')
