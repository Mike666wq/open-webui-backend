"""Resource lifecycle, durable tasks and per-member history boundaries."""

import json

from alembic import op
import sqlalchemy as sa

revision = '6f521b0d98ac'
down_revision = '0bd252b206fb'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'resource_entry',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('resource_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('deleted_at', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.BigInteger(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('data', sa.Text(), nullable=False),
        sa.UniqueConstraint('type', 'resource_id', 'user_id', name='uq_resource_identity'),
    )
    for field in ('user_id', 'state', 'expires_at'):
        op.create_index(f'ix_resource_entry_{field}', 'resource_entry', [field])
    op.create_table(
        'resource_task',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('lease_until', sa.BigInteger(), nullable=False),
        sa.Column('lease_token', sa.String(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('results', sa.Text(), nullable=False),
        sa.UniqueConstraint('user_id', 'key', name='uq_resource_task_key'),
    )
    for field in ('user_id', 'state'):
        op.create_index(f'ix_resource_task_{field}', 'resource_task', [field])
    op.create_table(
        'resource_rule',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('temporary_uploads', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_by', sa.String(), nullable=False),
    )
    # Old records contain no recoverable body. Preserve their identity and label
    # them honestly instead of silently deleting evidence of an old operation.
    conn = op.get_bind()
    names = sa.inspect(conn).get_table_names()
    target = sa.Table('resource_entry', sa.MetaData(), autoload_with=conn)
    for kind in ('chat', 'file', 'note', 'knowledge'):
        name = f'resource_recycle_{kind}'
        if name not in names:
            continue
        old = sa.Table(name, sa.MetaData(), autoload_with=conn)
        # Some old installations contain several tombstones for one identity.
        # Keep every old ID as an alias of a single, explicitly non-restorable
        # entry, rather than aborting the upgrade or inventing missing bodies.
        grouped = {}
        for row in conn.execute(sa.select(old).order_by(old.c.deleted_at.desc())).mappings():
            identity = (row['original_id'], row['user_id'])
            grouped.setdefault(identity, []).append(row)
        for rows in grouped.values():
            row = rows[0]
            conn.execute(target.insert().values(
                id=row['id'], type=kind, resource_id=row['original_id'],
                user_id=row['user_id'], state='legacy', title=row['title'],
                size_bytes=row['size_bytes'] or 0, deleted_at=row['deleted_at'],
                expires_at=row['expires_at'], generation=1,
                data=json.dumps({'legacy': True, 'reason': 'legacy_body_unavailable',
                                 'legacy_ids': [item['id'] for item in rows]}),
            ))


def downgrade():
    # Downgrading with recycled content would expose it to old clients.
    conn = op.get_bind()
    if conn.execute(sa.text(
        "SELECT count(*) FROM resource_entry WHERE state NOT IN ('active', 'purged')"
    )).scalar():
        raise RuntimeError('Resolve resource lifecycle entries or restore a pre-migration backup before downgrade')
    op.drop_table('resource_rule')
    op.drop_table('resource_task')
    op.drop_table('resource_entry')
