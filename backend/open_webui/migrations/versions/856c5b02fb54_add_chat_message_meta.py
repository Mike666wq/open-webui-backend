"""add chat message meta

Revision ID: 856c5b02fb54
Revises: 42e2978c7933
Create Date: 2026-07-16 01:39:39.291935

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '856c5b02fb54'
down_revision: Union[str, None] = '42e2978c7933'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A database restored from a snapshot or touched by hand can already have
    # the column while its alembic version still lags. Adding it again would
    # abort startup, so check first (same pattern as 4c5ce3d2f27f).
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('chat_message')}
    if 'meta' not in columns:
        op.add_column('chat_message', sa.Column('meta', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_message', 'meta')
