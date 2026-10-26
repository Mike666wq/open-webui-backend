"""Explicit, transactional migration of compatible administrator OpenAI sources."""

import hashlib
import hmac
import json

from fastapi import HTTPException
from open_webui.env import WEBUI_SECRET_KEY
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
from open_webui.utils.providers import encrypt_credential, public_connection, stable_model_id
from sqlalchemy import select

_KEYS = ('openai.api_base_urls', 'openai.api_keys', 'openai.api_configs')
_SUPPORTED = {'enable', 'model_ids', 'prefix_id', 'tags', 'connection_type', 'provider'}


def signature(value):
    return hmac.new(WEBUI_SECRET_KEY.encode(), json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()


async def preview_migration(request, user, db):
    from open_webui.routers.openai import get_models_request

    values = await Config.get_many(*_KEYS)
    urls, keys, configs = (values.get(k) or ([] if k != _KEYS[2] else {}) for k in _KEYS)
    sources = []
    claims = {}
    connections = (await db.scalars(select(ProviderConnection))).all()
    for index, url in enumerate(urls):
        config = configs.get(str(index), configs.get(url, {}))
        key = keys[index] if index < len(keys) else ''
        stamp = signature({'index': index, 'url': url, 'key': key, 'config': config})
        migrated = next(
            (
                c
                for c in connections
                if (c.config or {}).get('_legacy_source') == stamp
                or ((c.config or {}).get('_legacy_index') == index and (c.config or {}).get('base_url') == url)
            ),
            None,
        )
        blocked = sorted(k for k, v in config.items() if k not in _SUPPORTED and v not in (None, '', False, [], {}))
        reason = 'Unsupported request configuration: ' + ', '.join(blocked) if blocked else None
        if not Config.persistent_enabled_for('openai.api_configs'):
            reason = 'Enable persistent configuration before migrating connections'
        if config.get('connection_type', 'external') != 'external':
            reason = 'Only server-side external OpenAI-compatible sources can be migrated'
        models = list(config.get('model_ids') or [])
        if not models and config.get('enable', True) and not reason:
            result = await get_models_request(request, url, key, user=user, config=config)
            if result:
                records = result if isinstance(result, list) else result.get('data', [])
                models = [r['id'] for r in records if isinstance(r, dict) and isinstance(r.get('id'), str)]
            else:
                reason = 'Could not load legacy catalog; retry preview before migration'
        if not models and not migrated:
            reason = reason or 'No models available for migration'
        prefix = config.get('prefix_id')
        aliases = {f'{prefix}.{mid}' if prefix else mid: mid for mid in models}
        for alias in aliases:
            claims.setdefault(alias, []).append(index)
        sources.append(
            {
                'index': index,
                'name': config.get('provider') or f'OpenAI-compatible {index + 1}',
                'base_url': url,
                'fingerprint': stamp,
                'models': models,
                'aliases': aliases,
                'sharing': 'Existing legacy model access rules; shared server source',
                'special_config': {k: v for k, v in config.items() if k in _SUPPORTED},
                'blocked_reason': reason,
                'migrated_connection': migrated.id if migrated else None,
            }
        )
    return {'sources': sources, 'conflicts': {alias: ids for alias, ids in claims.items() if len(ids) > 1}}


async def apply_migration(request, user, db, index, fingerprint, alias_owners):
    # Lock configuration rows and serialize against other migration transactions.
    await db.scalars(select(Config).where(Config.key.in_(_KEYS)).with_for_update())
    # Only a preview of the current configuration may apply.
    existing = (await db.scalars(select(ProviderConnection))).all()
    done = next((c for c in existing if (c.config or {}).get('_legacy_source') == fingerprint), None)
    if done:
        return public_connection(done)
    preview = await preview_migration(request, user, db)
    source = next((s for s in preview['sources'] if s['index'] == index), None)
    if not source or source['fingerprint'] != fingerprint:
        raise HTTPException(409, 'Legacy settings changed; preview migration again')
    if source['blocked_reason']:
        raise HTTPException(400, source['blocked_reason'])
    for alias, owners in preview['conflicts'].items():
        if index in owners and alias_owners.get(alias) not in owners:
            raise HTTPException(409, 'Select the source owning each conflicting legacy model ID')
    # Re-read credentials server-side. No credential is returned in previews.
    values = await Config.get_many(*_KEYS)
    keys = values.get('openai.api_keys') or []
    if index >= len(keys) or not keys[index]:
        raise HTTPException(400, 'Source has no API key')
    aliases = {
        alias: mid
        for alias, mid in source['aliases'].items()
        if alias not in preview['conflicts'] or alias_owners.get(alias) == index
    }
    connection = new_provider_connection(
        provider_id='custom',
        name=source['name'],
        auth_type='api_key',
        owner_id=user.id,
        visibility='shared',
        status='configured',
        is_active=True,
        credential=encrypt_credential(keys[index]),
        models=[],
        config={
            'base_url': source['base_url'],
            'model_ids': source['models'],
            '_catalog_version': 1,
            '_legacy_source': fingerprint,
            '_legacy_index': index,
            '_legacy_aliases': aliases,
            '_legacy_access_ids': source['aliases'],
            '_legacy_chat_ids': source['models'],
            '_legacy_names': {mid: alias for alias, mid in aliases.items()},
            '_model_enabled': {m: True for m in source['models']},
        },
    )
    connection.models = [
        {
            'id': stable_model_id(connection.id, mid),
            'upstream_id': mid,
            'name': alias,
            'tags': source['special_config'].get('tags', []),
        }
        for alias, mid in source['aliases'].items()
    ]
    db.add(connection)
    await AccessGrants.replace_access_grants(
        db,
        'provider_connection',
        connection.id,
        [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}],
    )
    configs = dict(values.get('openai.api_configs') or {})
    configs[str(index)] = {**source['special_config'], 'enable': False}
    await Config.upsert({'openai.api_configs': configs}, db=db)
    await db.commit()
    from open_webui.routers.openai import get_all_models

    await get_all_models.cache.clear()
    request.app.state.BASE_MODELS = []
    return public_connection(connection)
