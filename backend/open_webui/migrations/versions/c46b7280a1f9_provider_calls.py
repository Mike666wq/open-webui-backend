"""Private provider invocation receipts."""

import sqlalchemy as sa
from alembic import op

revision = 'c46b7280a1f9'
down_revision = '93b7c16e42ad'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'provider_catalog_lease',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('token', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
    )
    op.create_table(
        'provider_call',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('connection_id', sa.Text(), nullable=True),
        sa.Column('model_id', sa.Text(), nullable=False),
        sa.Column('chat_id', sa.Text(), nullable=True),
        sa.Column('message_id', sa.Text(), nullable=True),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('elapsed_ms', sa.BigInteger(), nullable=False),
        sa.Column('usage', sa.JSON(), nullable=True),
    )
    op.create_index('idx_provider_call_time', 'provider_call', ['created_at'])
    op.create_index('idx_provider_call_owner_time', 'provider_call', ['user_id', 'created_at'])


def downgrade():
    op.drop_table('provider_catalog_lease')
    op.drop_index('idx_provider_call_time', table_name='provider_call')
    op.drop_index('idx_provider_call_owner_time', table_name='provider_call')
    op.drop_table('provider_call')
