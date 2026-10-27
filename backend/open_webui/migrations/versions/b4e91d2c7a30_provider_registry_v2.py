"""provider registry v2: curated model whitelist and per-user catalog snapshots

Revision ID: b4e91d2c7a30
Revises: c46b7280a1f9
Create Date: 2026-10-27

Introduces the two tables that back the Cherry Studio style provider model:

- ``provider_model`` holds the curated whitelist per connection. A model is
  only offered to users after a row exists here; a catalog refresh never
  creates rows implicitly.
- ``provider_catalog_snapshot`` holds the candidate pool fetched from the
  provider, per user.

The data migration is idempotent and fails soft: a connection whose payload
cannot be interpreted keeps its legacy ``config`` keys and its ``models``
column, and the runtime falls back to the legacy read path for it. The
``models`` column is therefore deliberately **not** dropped here; it stays as
the fallback source until a later release has proven the fallback unnecessary.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b4e91d2c7a30'
down_revision: Union[str, None] = 'c46b7280a1f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_MODEL_KEYS = ('_model_enabled', '_model_overrides', '_model_tests', '_models_by_user', '_catalog_version')

PROTOCOL_BY_PROVIDER = {'chatgpt': 'openai-responses'}
DEFAULT_PROTOCOL = 'openai-compatible'


def upgrade() -> None:
    op.create_table(
        'provider_model',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('connection_id', sa.Text(), nullable=False),
        sa.Column('upstream_id', sa.Text(), nullable=False),
        sa.Column('display_name', sa.Text(), nullable=False, default=''),
        sa.Column('uses', sa.JSON(), nullable=False),
        sa.Column('capabilities', sa.JSON(), nullable=False),
        sa.Column('features', sa.JSON(), nullable=False),
        sa.Column('model_info', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, default=True),
        sa.Column('source', sa.Text(), nullable=False, default='provider'),
        sa.Column('verification', sa.JSON(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, default=0),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connection_id', 'upstream_id', name='uq_provider_model_connection_upstream'),
    )
    op.create_index('idx_provider_model_connection', 'provider_model', ['connection_id'], unique=False)

    op.create_table(
        'provider_catalog_snapshot',
        sa.Column('id', sa.Text(), nullable=False),
        sa.Column('connection_id', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.Text(), nullable=False, default=''),
        sa.Column('models', sa.JSON(), nullable=False),
        sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('generation', sa.Text(), nullable=False, default=''),
        sa.Column('updated_at', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connection_id', 'user_id', name='uq_provider_catalog_snapshot_connection_user'),
    )
    op.create_index(
        'idx_provider_catalog_snapshot_connection', 'provider_catalog_snapshot', ['connection_id'], unique=False
    )
    op.create_index('idx_provider_catalog_snapshot_user', 'provider_catalog_snapshot', ['user_id'], unique=False)

    op.add_column(
        'provider_connection',
        sa.Column('protocol', sa.Text(), nullable=True, server_default=DEFAULT_PROTOCOL),
    )
    op.add_column(
        'provider_connection',
        sa.Column('is_enabled', sa.Boolean(), nullable=True, server_default=sa.true()),
    )
    op.add_column('provider_connection', sa.Column('provider_template', sa.Text(), nullable=True))

    _backfill()


def downgrade() -> None:
    op.drop_column('provider_connection', 'provider_template')
    op.drop_column('provider_connection', 'is_enabled')
    op.drop_column('provider_connection', 'protocol')
    op.drop_index('idx_provider_catalog_snapshot_user', table_name='provider_catalog_snapshot')
    op.drop_index('idx_provider_catalog_snapshot_connection', table_name='provider_catalog_snapshot')
    op.drop_table('provider_catalog_snapshot')
    op.drop_index('idx_provider_model_connection', table_name='provider_model')
    op.drop_table('provider_model')


def _row_id(prefix: str, connection_id: str, upstream_id: str) -> str:
    import hashlib

    digest = hashlib.sha256(f'{connection_id}:{upstream_id}'.encode()).hexdigest()[:32]
    return f'{prefix}_{digest}'


def _load(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _migrate_connection(bind, row) -> bool:
    """Move one connection onto the new tables. Returns True when it moved."""
    connection_id = row['id']
    provider_id = row['provider_id']
    config = _load(row['config'])
    if not isinstance(config, dict):
        return False
    models = _load(row['models'])
    models = models if isinstance(models, list) else []

    enabled_map = config.get('_model_enabled') if isinstance(config.get('_model_enabled'), dict) else {}
    overrides = config.get('_model_overrides') if isinstance(config.get('_model_overrides'), dict) else {}
    tests = config.get('_model_tests') if isinstance(config.get('_model_tests'), dict) else {}
    selected = config.get('model_ids') if isinstance(config.get('model_ids'), list) else []

    whitelist = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        upstream_id = model.get('upstream_id')
        if not isinstance(upstream_id, str) or not upstream_id:
            continue
        # The legacy model was offered when it was not explicitly disabled.
        offered = enabled_map.get(upstream_id)
        if offered is None:
            offered = upstream_id in selected if selected else True
        if not offered:
            continue
        whitelist[upstream_id] = model
    for upstream_id in selected:
        if isinstance(upstream_id, str) and upstream_id and upstream_id not in whitelist:
            whitelist[upstream_id] = {'upstream_id': upstream_id, 'name': upstream_id}
    for upstream_id in overrides:
        if isinstance(upstream_id, str) and upstream_id and upstream_id not in whitelist:
            whitelist[upstream_id] = {'upstream_id': upstream_id, 'name': upstream_id}

    now = row['updated_at'] or 0
    for order, (upstream_id, model) in enumerate(whitelist.items()):
        verification = {}
        raw_tests = tests.get(upstream_id)
        if isinstance(raw_tests, dict):
            for use, result in raw_tests.items():
                if isinstance(result, dict):
                    verification[use] = result
        uses = model.get('uses') if isinstance(model.get('uses'), dict) else {}
        if not uses and isinstance(overrides.get(upstream_id), dict):
            uses = overrides[upstream_id]
        bind.execute(
            sa.text(
                'insert into provider_model (id, connection_id, upstream_id, display_name, uses, capabilities,'
                ' features, model_info, enabled, source, verification, sort_order, created_at, updated_at)'
                ' values (:id, :connection_id, :upstream_id, :display_name, :uses, :capabilities, :features,'
                ' :model_info, :enabled, :source, :verification, :sort_order, :created_at, :updated_at)'
                ' on conflict do nothing'
            ),
            {
                'id': _row_id('pm', connection_id, upstream_id),
                'connection_id': connection_id,
                'upstream_id': upstream_id,
                'display_name': model.get('name') or upstream_id,
                'uses': json.dumps(uses),
                'capabilities': json.dumps(model.get('capabilities') if isinstance(model.get('capabilities'), list) else []),
                'features': json.dumps(model.get('features') if isinstance(model.get('features'), list) else []),
                'model_info': json.dumps(model.get('model_info') if isinstance(model.get('model_info'), dict) else {}),
                'enabled': True,
                'source': 'provider',
                'verification': json.dumps(verification),
                'sort_order': order,
                'created_at': now,
                'updated_at': now,
            },
        )

    snapshots = config.get('_models_by_user')
    if isinstance(snapshots, dict):
        for user_id, entry in snapshots.items():
            if not isinstance(entry, dict):
                continue
            bind.execute(
                sa.text(
                    'insert into provider_catalog_snapshot'
                    ' (id, connection_id, user_id, fingerprint, models, error, generation, updated_at)'
                    ' values (:id, :connection_id, :user_id, :fingerprint, :models, :error, :generation, :updated_at)'
                    ' on conflict do nothing'
                ),
                {
                    'id': _row_id('pcs', connection_id, user_id),
                    'connection_id': connection_id,
                    'user_id': user_id,
                    'fingerprint': entry.get('fingerprint') or '',
                    'models': json.dumps(entry.get('models') if isinstance(entry.get('models'), list) else []),
                    'error': json.dumps(entry.get('error')) if entry.get('error') else None,
                    'generation': entry.get('generation') or '',
                    'updated_at': entry.get('updated_at') or 0,
                },
            )

    cleaned = {key: value for key, value in config.items() if key not in LEGACY_MODEL_KEYS}
    # Mark the connection as moved so the runtime stops using the legacy path.
    cleaned['_registry_version'] = 2
    protocol = PROTOCOL_BY_PROVIDER.get(provider_id, DEFAULT_PROTOCOL)
    bind.execute(
        sa.text(
            'update provider_connection set config = :config, protocol = :protocol,'
            ' provider_template = coalesce(provider_template, :provider_template),'
            ' is_enabled = :is_enabled where id = :id'
        ),
        {
            'config': json.dumps(cleaned),
            'protocol': protocol,
            'provider_template': provider_id,
            'is_enabled': row['status'] != 'disconnected',
            'id': connection_id,
        },
    )
    return True


def _backfill() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text('select id, provider_id, config, models, status, updated_at from provider_connection')
    ).fetchall()
    for row in rows:
        mapping = dict(row._mapping) if hasattr(row, '_mapping') else dict(row)
        try:
            _migrate_connection(bind, mapping)
        except Exception:
            # Keep the legacy payload for this connection; the runtime falls
            # back to the legacy read path instead of losing the models.
            continue
