"""add encrypted browser login PKCE verifier

Revision ID: d8a74b2e19f0
Revises: c4b9e2a70d31
"""
from alembic import op
import sqlalchemy as sa

revision = 'd8a74b2e19f0'
down_revision = 'c4b9e2a70d31'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('chatgpt_auth_attempt', sa.Column('pkce_verifier', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('chatgpt_auth_attempt', 'pkce_verifier')
