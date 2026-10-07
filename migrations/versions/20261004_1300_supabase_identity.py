"""supabase identity mapping

Adds the columns that let an application account be linked to a Supabase Auth
identity (Google sign-in):

* ``users.supabase_user_id`` — the durable federated identity key. Unique, so the
  same Supabase user can never be mapped to two application accounts. NULL for
  password-only accounts (multiple NULLs are allowed by both PostgreSQL and
  SQLite, so existing rows stay valid).
* ``users.password_hash`` — becomes nullable, because a Google-only account has
  no password to store.
* ``users.auth_provider`` — how the account was created (``password`` or
  ``google``), used for display only. Authorization never reads it.
* ``users.avatar_url`` — the provider's profile picture, for the account menu.

Revision ID: 7c1f9b4e2d10
Revises: 1a2a6448d174
Create Date: 2026-10-04 13:00:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7c1f9b4e2d10'
down_revision: Union[str, None] = '1a2a6448d174'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``batch_alter_table`` keeps this migration working on SQLite, which cannot
    # ALTER a column in place and rebuilds the table instead.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supabase_user_id', sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                'auth_provider',
                sa.String(length=32),
                nullable=False,
                server_default='password',
            )
        )
        batch_op.add_column(sa.Column('avatar_url', sa.String(length=512), nullable=True))
        batch_op.alter_column(
            'password_hash',
            existing_type=sa.String(length=255),
            nullable=True,
        )
        batch_op.create_index('ix_users_supabase_user_id', ['supabase_user_id'], unique=True)


def downgrade() -> None:
    # Federated accounts have no password, so give them the empty marker (which
    # ``verify_password`` already treats as "cannot sign in with a password")
    # before restoring the NOT NULL constraint.
    op.execute("UPDATE users SET password_hash = '' WHERE password_hash IS NULL")
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_supabase_user_id')
        batch_op.alter_column(
            'password_hash',
            existing_type=sa.String(length=255),
            nullable=False,
        )
        batch_op.drop_column('avatar_url')
        batch_op.drop_column('auth_provider')
        batch_op.drop_column('supabase_user_id')
