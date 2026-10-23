"""add persisted ChatGPT SIWC attempts and stable host id

Revision ID: a73f29c4e61b
Revises: 8c51af7e2d39
Create Date: 2026-10-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a73f29c4e61b'
down_revision: Union[str, None] = '8c51af7e2d39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chatgpt_auth_attempt',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('pairing_code_hash', sa.String(), nullable=False),
        sa.Column('helper_token_hash', sa.String(), nullable=True),
        sa.Column('host_id', sa.String(), nullable=False),
        sa.Column('connection_id', sa.String(), nullable=True),
        sa.Column('expected_subject', sa.String(), nullable=True),
        sa.Column('expected_client_id', sa.String(), nullable=True),
        sa.Column('state', sa.String(), nullable=True),
        sa.Column('nonce', sa.String(), nullable=True),
        sa.Column('code_challenge', sa.String(), nullable=True),
        sa.Column('redirect_uri', sa.String(), nullable=True),
        sa.Column('client_id', sa.String(), nullable=True),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_chatgpt_auth_attempt_owner', 'chatgpt_auth_attempt', ['owner_id', 'created_at'], unique=False)
    op.create_index('idx_chatgpt_auth_attempt_expiry', 'chatgpt_auth_attempt', ['expires_at'], unique=False)
    op.create_table(
        'chatgpt_auth_host',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('host_id', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('host_id'),
    )


def downgrade() -> None:
    op.drop_table('chatgpt_auth_host')
    op.drop_index('idx_chatgpt_auth_attempt_expiry', table_name='chatgpt_auth_attempt')
    op.drop_index('idx_chatgpt_auth_attempt_owner', table_name='chatgpt_auth_attempt')
    op.drop_table('chatgpt_auth_attempt')
