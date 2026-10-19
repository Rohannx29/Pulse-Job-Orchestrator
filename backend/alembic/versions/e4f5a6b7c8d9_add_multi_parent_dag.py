"""add job_dependencies table for multi-parent DAG workflows

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-20 01:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_dependencies',
        sa.Column('parent_job_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('child_job_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['parent_job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['child_job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('parent_job_id', 'child_job_id'),
    )
    op.create_index(op.f('ix_job_dependencies_parent_job_id'), 'job_dependencies', ['parent_job_id'], unique=False)
    op.create_index(op.f('ix_job_dependencies_child_job_id'), 'job_dependencies', ['child_job_id'], unique=False)

    # Seamless data migration: Backfill existing single-parent dependencies into join table
    op.execute(
        """
        INSERT INTO job_dependencies (parent_job_id, child_job_id, created_at)
        SELECT depends_on_job_id, id, created_at
        FROM jobs
        WHERE depends_on_job_id IS NOT NULL
        ON CONFLICT DO NOTHING;
        """
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_job_dependencies_child_job_id'), table_name='job_dependencies')
    op.drop_index(op.f('ix_job_dependencies_parent_job_id'), table_name='job_dependencies')
    op.drop_table('job_dependencies')
