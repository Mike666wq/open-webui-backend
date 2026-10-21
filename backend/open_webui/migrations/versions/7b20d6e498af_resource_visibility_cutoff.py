"""Separate nanosecond conversation boundary from capacity counters."""

from alembic import op
import sqlalchemy as sa

revision = '7b20d6e498af'
down_revision = '6f521b0d98ac'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('resource_entry', sa.Column('cutoff_at', sa.BigInteger(), nullable=False, server_default='0'))
    op.execute(sa.text("UPDATE resource_entry SET cutoff_at = size_bytes, size_bytes = 0 WHERE type = 'channel_clear'"))
    op.create_index('ix_resource_visibility', 'resource_entry', ['type', 'resource_id', 'state'])


def downgrade():
    op.execute(sa.text("UPDATE resource_entry SET size_bytes = cutoff_at WHERE type = 'channel_clear'"))
    op.drop_index('ix_resource_visibility', table_name='resource_entry')
    op.drop_column('resource_entry', 'cutoff_at')
