"""add Kimi Code device authorization attempts

Revision ID: c4b9e2a70d31
Revises: a73f29c4e61b
Create Date: 2026-10-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4b9e2a70d31'
down_revision: Union[str, None] = 'a73f29c4e61b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'kimi_auth_attempt',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('device_code', sa.Text(), nullable=False),
        sa.Column('user_code', sa.Text(), nullable=False),
        sa.Column('verification_uri', sa.Text(), nullable=False),
        sa.Column('verification_uri_complete', sa.Text(), nullable=False),
        sa.Column('device_id', sa.String(), nullable=False),
        sa.Column('poll_token', sa.String(), nullable=True),
        sa.Column('interval', sa.BigInteger(), nullable=False),
        sa.Column('next_poll_at', sa.BigInteger(), nullable=False),
        sa.Column('connection_id', sa.String(), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_kimi_auth_attempt_owner', 'kimi_auth_attempt', ['owner_id', 'created_at'], unique=False)
    op.create_index('idx_kimi_auth_attempt_expiry', 'kimi_auth_attempt', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_kimi_auth_attempt_expiry', table_name='kimi_auth_attempt')
    op.drop_index('idx_kimi_auth_attempt_owner', table_name='kimi_auth_attempt')
    op.drop_table('kimi_auth_attempt')
