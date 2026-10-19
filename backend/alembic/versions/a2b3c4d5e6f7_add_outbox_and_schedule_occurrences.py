"""add outbox_events and schedule_occurrences tables

Revision ID: a2b3c4d5e6f7
Revises: f5a6b7c8d9e0
Create Date: 2026-10-20 02:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'f5a6b7c8d9e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. schedule_occurrences table
    op.create_table(
        'schedule_occurrences',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('schedule_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('scheduled_fire_time', sa.DateTime(), nullable=False),
        sa.Column('dispatched_job_id', sa.UUID(as_uuid=False), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['schedule_id'], ['job_schedules.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['dispatched_job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('schedule_id', 'scheduled_fire_time', name='uq_schedule_occurrence_fire_time'),
    )
    op.create_index(op.f('ix_schedule_occurrences_schedule_id'), 'schedule_occurrences', ['schedule_id'], unique=False)
    op.create_index(op.f('ix_schedule_occurrences_scheduled_fire_time'), 'schedule_occurrences', ['scheduled_fire_time'], unique=False)

    # 2. outbox_events table
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('aggregate_type', sa.String(length=50), nullable=False),
        sa.Column('aggregate_id', sa.String(length=255), nullable=False),
        sa.Column('payload', JSONB().with_variant(sa.JSON(), 'sqlite'), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('fencing_key', sa.String(length=255), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('published_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('fencing_key', name='uq_outbox_events_fencing_key'),
    )
    op.create_index(op.f('ix_outbox_events_event_type'), 'outbox_events', ['event_type'], unique=False)
    op.create_index(op.f('ix_outbox_events_aggregate_id'), 'outbox_events', ['aggregate_id'], unique=False)
    op.create_index(op.f('ix_outbox_events_aggregate_type'), 'outbox_events', ['aggregate_type'], unique=False)
    op.create_index(op.f('ix_outbox_events_status'), 'outbox_events', ['status'], unique=False)
    op.create_index(op.f('ix_outbox_events_created_at'), 'outbox_events', ['created_at'], unique=False)
    op.create_index('ix_outbox_events_status_created', 'outbox_events', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_outbox_events_status_created', table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_created_at'), table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_status'), table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_aggregate_type'), table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_aggregate_id'), table_name='outbox_events')
    op.drop_index(op.f('ix_outbox_events_event_type'), table_name='outbox_events')
    op.drop_table('outbox_events')

    op.drop_index(op.f('ix_schedule_occurrences_scheduled_fire_time'), table_name='schedule_occurrences')
    op.drop_index(op.f('ix_schedule_occurrences_schedule_id'), table_name='schedule_occurrences')
    op.drop_table('schedule_occurrences')
