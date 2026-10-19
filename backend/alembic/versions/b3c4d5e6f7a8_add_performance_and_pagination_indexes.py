"""add performance and pagination indexes

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-20 03:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Compound indexes for jobs listing & cursor pagination
    op.create_index('ix_jobs_queue_created', 'jobs', ['queue_id', 'created_at'], unique=False)
    op.create_index('ix_jobs_status_created', 'jobs', ['status', 'created_at'], unique=False)

    # 2. Compound index for tenant-scoped audit log pagination
    op.create_index('ix_audit_logs_org_created', 'audit_logs', ['org_id', 'created_at'], unique=False)

    # 3. Compound index for webhook deliveries pagination
    op.create_index('ix_webhook_deliveries_sub_delivered', 'webhook_deliveries', ['subscription_id', 'delivered_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_webhook_deliveries_sub_delivered', table_name='webhook_deliveries')
    op.drop_index('ix_audit_logs_org_created', table_name='audit_logs')
    op.drop_index('ix_jobs_status_created', table_name='jobs')
    op.drop_index('ix_jobs_queue_created', table_name='jobs')
