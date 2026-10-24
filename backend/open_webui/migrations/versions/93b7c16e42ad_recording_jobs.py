"""Private durable recording transcription jobs."""

import sqlalchemy as sa
from alembic import op

revision = '93b7c16e42ad'
down_revision = 'd8a74b2e19f0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'recording',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('request_key', sa.String(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('duration', sa.Integer(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('source_present', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(), nullable=True),
        sa.Column('lease_until', sa.BigInteger(), nullable=False),
        sa.Column('summary_token', sa.String(), nullable=True),
        sa.Column('summary_until', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('note_id', sa.String(), nullable=False),
        sa.Column('data', sa.Text(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_key', name='uq_recording_request'),
    )
    op.create_index(
        'uq_recording_active_user',
        'recording',
        ['user_id'],
        unique=True,
        sqlite_where=sa.text('lease_token IS NOT NULL'),
        postgresql_where=sa.text('lease_token IS NOT NULL'),
    )
    for column in ('user_id', 'state'):
        op.create_index(f'ix_recording_{column}', 'recording', [column])


def downgrade():
    op.drop_table('recording')
