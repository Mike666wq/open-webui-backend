"""add provider connections

Revision ID: 8c51af7e2d39
Revises: 7b20d6e498af
Create Date: 2026-10-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '8c51af7e2d39'
down_revision: Union[str, None] = '7b20d6e498af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'provider_connection',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('provider_id', sa.Text(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('auth_type', sa.Text(), nullable=False),
        sa.Column('visibility', sa.Text(), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('credential', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('models', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_provider_connection_owner', 'provider_connection', ['owner_id'], unique=False)
    op.create_index('idx_provider_connection_visibility', 'provider_connection', ['visibility'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_provider_connection_visibility', table_name='provider_connection')
    op.drop_index('idx_provider_connection_owner', table_name='provider_connection')
    op.drop_table('provider_connection')
