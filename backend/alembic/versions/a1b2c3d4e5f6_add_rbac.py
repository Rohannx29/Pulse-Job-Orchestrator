"""add rbac: user role, org members, invite tokens

Revision ID: a1b2c3d4e5f6
Revises: b492bca13ef2
Create Date: 2026-10-19 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'b492bca13ef2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        user_role_enum = sa.Enum('system_admin', 'org_admin', 'member', name='userrole')
        user_role_enum.create(bind, checkfirst=True)
        org_member_role_enum = sa.Enum('admin', 'member', name='orgmemberrole')
        org_member_role_enum.create(bind, checkfirst=True)

    # 2. Add `role` column to users (default org_admin — preserves existing accounts)
    op.add_column(
        'users',
        sa.Column(
            'role',
            sa.Enum('system_admin', 'org_admin', 'member', name='userrole'),
            nullable=False,
            server_default='org_admin',
        )
    )

    # 3. Create organization_members table
    op.create_table(
        'organization_members',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('org_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('user_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column(
            'role',
            sa.Enum('admin', 'member', name='orgmemberrole'),
            nullable=False,
            server_default='admin',
        ),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id', 'user_id', name='uq_org_member'),
    )
    op.create_index('ix_organization_members_org_id', 'organization_members', ['org_id'])
    op.create_index('ix_organization_members_user_id', 'organization_members', ['user_id'])

    # 4. Backfill: create OrganizationMember rows for existing org owners
    # so the new access control table is consistent with existing data.
    if bind.dialect.name == "postgresql":
        op.execute("""
            INSERT INTO organization_members (id, org_id, user_id, role, created_at)
            SELECT gen_random_uuid(), id, owner_id, 'admin', created_at
            FROM organizations
            ON CONFLICT (org_id, user_id) DO NOTHING
        """)

        # 5. Promote the very first user to system_admin (oldest created_at).
        # This is a best-effort bootstrap: on a fresh DB there's no user yet,
        # so nothing happens. On an existing DB the original operator account
        # becomes SYSTEM_ADMIN.
        op.execute("""
            UPDATE users
            SET role = 'system_admin'
            WHERE id = (SELECT id FROM users ORDER BY created_at ASC LIMIT 1)
        """)

    # 6. Create invite_tokens table
    op.create_table(
        'invite_tokens',
        sa.Column('id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('org_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('invited_by_user_id', sa.UUID(as_uuid=False), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invited_by_user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_invite_tokens_org_id', 'invite_tokens', ['org_id'])
    op.create_index('ix_invite_tokens_email', 'invite_tokens', ['email'])
    op.create_index('ix_invite_tokens_token_hash', 'invite_tokens', ['token_hash'], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index('ix_invite_tokens_token_hash', table_name='invite_tokens')
    op.drop_index('ix_invite_tokens_email', table_name='invite_tokens')
    op.drop_index('ix_invite_tokens_org_id', table_name='invite_tokens')
    op.drop_table('invite_tokens')

    op.drop_index('ix_organization_members_user_id', table_name='organization_members')
    op.drop_index('ix_organization_members_org_id', table_name='organization_members')
    op.drop_table('organization_members')

    op.drop_column('users', 'role')

    if bind.dialect.name == "postgresql":
        sa.Enum(name='orgmemberrole').drop(bind, checkfirst=True)
        sa.Enum(name='userrole').drop(bind, checkfirst=True)
