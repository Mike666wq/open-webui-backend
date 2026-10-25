"""Provider connection catalog and user-scoped connections."""

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrant, AccessGrants
from open_webui.models.provider_connections import (
    ProviderConnection,
    ProviderConnectionCreate,
    ProviderConnectionUpdate,
    new_provider_connection,
)
from open_webui.utils.apps import validate_app_url
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_capabilities import ADAPTERS, normalize_model, provider_family
from open_webui.utils.providers import (
    PROVIDER_CATALOG,
    can_access_connection,
    discover_connection_models,
    encrypt_credential,
    initialize_model_catalog,
    public_connection,
    require_connection_access,
    verify_provider_connection,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BAILIAN_REGIONS = {'cn-beijing', 'ap-southeast-1', 'cn-hongkong', 'eu-central-1', 'us-east-1', 'ap-northeast-1'}
_BAILIAN_WORKSPACE_LABEL = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')

router = APIRouter()


def _validate_form(form: ProviderConnectionCreate, user):
    if form.visibility == 'shared' and user.role != 'admin':
        raise HTTPException(403, 'Only administrators can create shared provider connections')
    if form.provider_id in ('chatgpt', 'kimi'):
        raise HTTPException(400, f'Connect {form.provider_id} through its OAuth sign-in flow')
    elif form.auth_type != 'api_key':
        raise HTTPException(400, 'This provider requires an API key')
    elif not form.api_key:
        raise HTTPException(400, 'An API key is required')
    config = dict(form.config or {})
    allowed = {'base_url', 'region', 'workspace_id', 'model_ids', 'api_type'}
    if set(config) - allowed:
        raise HTTPException(400, 'Unsupported provider configuration field')
    for key in ('region', 'workspace_id', 'api_type'):
        if key in config and (not isinstance(config[key], str) or len(config[key]) > 128):
            raise HTTPException(400, f'{key} must be a string no longer than 128 characters')
    if form.provider_id == 'custom' and config.get('api_type', 'openai') not in ('openai', 'responses'):
        raise HTTPException(400, 'Custom API type must be openai or responses')
    if config.get('base_url'):
        try:
            validate_app_url(config['base_url'].rstrip('/'))
        except ValueError:
            raise HTTPException(400, 'Provider URL must be a valid HTTPS endpoint') from None
    model_ids = config.get('model_ids', [])
    if (
        not isinstance(model_ids, list)
        or len(model_ids) > 500
        or any(not isinstance(x, str) or not x or len(x) > 256 for x in model_ids)
    ):
        raise HTTPException(400, 'model_ids must be a list of at most 500 non-empty strings')
    if form.provider_id == 'custom' and not config.get('base_url'):
        raise HTTPException(400, 'A base URL is required for custom providers')
    _validate_bailian_config(form.provider_id, config)
    return config


def _validate_bailian_config(provider_id, config):
    if provider_id != 'bailian':
        return
    region = config.get('region') or 'cn-beijing'
    if region not in _BAILIAN_REGIONS:
        raise HTTPException(400, 'Unsupported Bailian region')
    workspace = config.get('workspace_id')
    if workspace and (not isinstance(workspace, str) or not _BAILIAN_WORKSPACE_LABEL.fullmatch(workspace)):
        raise HTTPException(400, 'Invalid Bailian workspace ID')
    if region in {'eu-central-1', 'us-east-1', 'ap-northeast-1'} and not workspace and not config.get('base_url'):
        raise HTTPException(400, 'Bailian requires a workspace ID for this region')


async def _connection_with_access(connection_id: str, user, db):
    connection = await db.get(ProviderConnection, connection_id)
    return await require_connection_access(connection, user, db)


async def _connection_with_manage_access(connection_id: str, user, db):
    connection = await db.get(ProviderConnection, connection_id)
    if not connection:
        raise HTTPException(404, 'Provider connection not found')
    if connection.visibility == 'shared':
        if user.role != 'admin':
            raise HTTPException(403, 'Only administrators can manage shared provider connections')
    elif connection.owner_id != user.id:
        raise HTTPException(404, 'Provider connection not found')
    return connection


@router.get('')
async def get_provider_catalog(user=Depends(get_verified_user)):
    return PROVIDER_CATALOG


@router.get('/connections')
async def list_connections(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    connections = (
        await db.scalars(
            select(ProviderConnection).where(ProviderConnection.is_active.is_(True)).order_by(ProviderConnection.name)
        )
    ).all()
    visible = []
    for connection in connections:
        if await can_access_connection(connection, user, db):
            item = public_connection(connection)
            if user.role == 'admin' and connection.visibility == 'shared':
                item['access_control'] = [
                    {
                        'principal_type': grant.principal_type,
                        'principal_id': grant.principal_id,
                        'permission': grant.permission,
                    }
                    for grant in (
                        await db.scalars(
                            select(AccessGrant).where(
                                AccessGrant.resource_type == 'provider_connection',
                                AccessGrant.resource_id == connection.id,
                            )
                        )
                    ).all()
                ]
            visible.append(item)
    return visible


@router.post('/connections/verify')
async def verify_ephemeral_connection(form: ProviderConnectionCreate, user=Depends(get_verified_user)):
    config = _validate_form(form, user)
    connection = new_provider_connection(
        provider_id=form.provider_id,
        name=form.name,
        auth_type=form.auth_type,
        visibility=form.visibility,
        owner_id=user.id,
        config={**config, '_catalog_version': 1},
        credential=encrypt_credential(form.api_key.get_secret_value() if form.api_key else None),
        status='configured',
        models=[],
        is_active=True,
    )
    await initialize_model_catalog(connection)
    verified = await verify_provider_connection(connection, user)
    models = await discover_connection_models(connection, user)
    return {'ok': verified, 'status': 'connected' if verified else 'manual', 'models': models}


@router.post('/connections')
async def create_connection(
    form: ProviderConnectionCreate, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    config = _validate_form(form, user)
    connection = new_provider_connection(
        provider_id=form.provider_id,
        name=form.name,
        auth_type=form.auth_type,
        visibility=form.visibility,
        owner_id=user.id,
        config={**config, '_catalog_version': 1},
        credential=encrypt_credential(form.api_key.get_secret_value() if form.api_key else None),
        status='configured',
        models=[],
        is_active=True,
    )
    db.add(connection)
    if form.visibility == 'shared':
        await AccessGrants.replace_access_grants(db, 'provider_connection', connection.id, form.access_control)
    await db.commit()
    return public_connection(connection)


@router.get('/connections/{connection_id}')
async def get_connection(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_access(connection_id, user, db)
    result = public_connection(connection)
    if user.role == 'admin' and connection.visibility == 'shared':
        result['access_control'] = [
            {'principal_type': grant.principal_type, 'principal_id': grant.principal_id, 'permission': grant.permission}
            for grant in (
                await db.scalars(
                    select(AccessGrant).where(
                        AccessGrant.resource_type == 'provider_connection', AccessGrant.resource_id == connection_id
                    )
                )
            ).all()
        ]
    return result


@router.patch('/connections/{connection_id}')
async def update_connection(
    connection_id: str,
    form: ProviderConnectionUpdate,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    connection = await db.get(ProviderConnection, connection_id)
    if not connection:
        raise HTTPException(404, 'Provider connection not found')
    if connection.visibility == 'shared':
        if user.role != 'admin':
            raise HTTPException(403, 'Only administrators can edit shared provider connections')
    elif connection.owner_id != user.id:
        raise HTTPException(404, 'Provider connection not found')
    values = form.model_dump(exclude_unset=True)
    for field in ('name', 'visibility', 'config'):
        if field in values and values[field] is None:
            raise HTTPException(400, f'{field} cannot be null')
    if connection.auth_type == 'oauth':
        if set(values) - {'name'} or values.get('name') is None:
            raise HTTPException(400, 'Only the OAuth connection name can be edited here')
    if values.get('visibility') == 'shared' and user.role != 'admin':
        raise HTTPException(403, 'Only administrators can create shared provider connections')
    grants = values.pop('access_control', None)
    api_key = values.pop('api_key', None)
    if api_key:
        connection.credential = encrypt_credential(api_key.get_secret_value())
    if 'name' in values:
        connection.name = values['name']
    if 'visibility' in values:
        connection.visibility = values['visibility']
    if 'config' in values:
        config = values['config'] or {}
        if connection.auth_type == 'oauth':
            raise HTTPException(400, 'Manage OAuth through its provider settings')
        allowed = {'base_url', 'region', 'workspace_id', 'model_ids', 'api_type'}
        if set(config) - allowed:
            raise HTTPException(400, 'Unsupported provider configuration field')
        for key in ('region', 'workspace_id', 'api_type'):
            if key in config and (not isinstance(config[key], str) or len(config[key]) > 128):
                raise HTTPException(400, f'{key} must be a string no longer than 128 characters')
        if connection.provider_id == 'custom' and config.get('api_type', 'openai') not in ('openai', 'responses'):
            raise HTTPException(400, 'Custom API type must be openai or responses')
        if config.get('base_url'):
            try:
                validate_app_url(config['base_url'].rstrip('/'))
            except ValueError:
                raise HTTPException(400, 'Provider URL must be a valid HTTPS endpoint') from None
        if connection.provider_id == 'custom' and not config.get('base_url'):
            raise HTTPException(400, 'A base URL is required for custom providers')
        _validate_bailian_config(connection.provider_id, config)
        model_ids = config.get('model_ids', [])
        if (
            not isinstance(model_ids, list)
            or len(model_ids) > 500
            or any(not isinstance(x, str) or not x or len(x) > 256 for x in model_ids)
        ):
            raise HTTPException(400, 'model_ids must be a list of at most 500 non-empty strings')
        connection.config = {**{k: v for k, v in (connection.config or {}).items() if k.startswith('_')}, **config}
    if connection.provider_id == 'chatgpt' and connection.auth_type == 'oauth':
        if api_key or 'visibility' in values or grants is not None:
            raise HTTPException(400, 'Manage ChatGPT OAuth through its provider settings')
    if connection.visibility == 'private' and grants is not None:
        raise HTTPException(400, 'Private provider connections do not use access_control')
    if grants is not None:
        if connection.visibility != 'shared' or user.role != 'admin':
            raise HTTPException(403, 'Only administrators can edit shared provider access')
        await AccessGrants.replace_access_grants(db, 'provider_connection', connection.id, grants)
    connection.updated_at = max(int(time.time()), connection.updated_at + 1)
    if api_key or 'config' in values:
        connection.config = {key: value for key, value in (connection.config or {}).items() if key != '_models_by_user'}
        connection.models = []
        connection.status = 'configured'
    await db.commit()
    return public_connection(connection)


@router.delete('/connections/{connection_id}')
async def delete_connection(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await db.get(ProviderConnection, connection_id)
    if not connection:
        raise HTTPException(404, 'Provider connection not found')
    if connection.visibility == 'shared':
        if user.role != 'admin':
            raise HTTPException(403, 'Only administrators can delete shared provider connections')
    elif connection.owner_id != user.id:
        raise HTTPException(404, 'Provider connection not found')
    if connection.auth_type == 'oauth':
        raise HTTPException(400, 'Disconnect this account through its OAuth settings')
    await db.delete(connection)
    await AccessGrants.replace_access_grants(db, 'provider_connection', connection_id, [])
    await db.commit()
    return {'ok': True}


@router.post('/connections/{connection_id}/verify')
async def verify_connection(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_manage_access(connection_id, user, db)
    await initialize_model_catalog(connection)
    verified = await verify_provider_connection(connection, user)
    models = await discover_connection_models(connection, user)
    connection.status = 'connected' if verified else 'configured'
    connection.models = models
    connection.updated_at = max(int(time.time()), connection.updated_at + 1)
    await db.commit()
    return {'ok': verified, 'status': connection.status, 'models': models}


@router.post('/connections/{connection_id}/models/refresh')
async def refresh_models(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_manage_access(connection_id, user, db)
    await initialize_model_catalog(connection)
    verified = await verify_provider_connection(connection, user)
    await initialize_model_catalog(connection)
    models = await discover_connection_models(connection, user)
    cache = dict((connection.config or {}).get('_models_by_user') or {})
    cache[user.id] = models
    connection.config = {**(connection.config or {}), '_models_by_user': cache}
    connection.models = models
    connection.status = 'connected' if verified else 'configured'
    connection.updated_at = max(int(time.time()), connection.updated_at + 1)
    await db.commit()
    return {'models': models, 'status': connection.status}


@router.get('/models')
async def get_visible_models(
    purpose: str = 'chat', user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    from open_webui.utils.providers import list_visible_models

    errors = []
    if purpose == 'chat':
        models = await list_visible_models(user, db)
    else:
        from open_webui.utils.provider_capabilities import ADAPTERS
        from open_webui.utils.providers import visible_connection_models

        if purpose not in (*ADAPTERS, 'all'):
            raise HTTPException(400, 'Unknown model purpose')
        models = []
        for connection in (
            await db.scalars(select(ProviderConnection).where(ProviderConnection.is_active.is_(True)))
        ).all():
            if await can_access_connection(connection, user, db):
                try:
                    connection_models = await visible_connection_models(connection, user, db)
                except HTTPException as exc:
                    # One unavailable catalog must not hide other authorized models.
                    errors.append(
                        {
                            'connection_id': connection.id,
                            'connection_name': connection.name,
                            'status': exc.status_code,
                        }
                    )
                    continue
                models.extend(
                    {**m, 'connection_name': connection.name}
                    for m in connection_models
                    if purpose == 'all' or purpose in m.get('uses', {})
                )
    return {
        'object': 'list',
        'data': models,
        'errors': errors,
    }


class ModelUses(BaseModel):
    upstream_id: str = Field(min_length=1, max_length=256)
    uses: dict[str, str]


@router.put('/connections/{connection_id}/model-uses')
async def set_model_uses(
    connection_id: str, form: ModelUses, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_manage_access(connection_id, user, db)
    if connection.auth_type != 'api_key':
        raise HTTPException(400, 'Account model capabilities cannot be overridden')
    if any(use not in ADAPTERS or adapter not in ADAPTERS[use] for use, adapter in form.uses.items()):
        raise HTTPException(400, 'Unsupported purpose or adapter')
    if form.uses.get('image') == 'siliconflow-image' and provider_family(connection) != 'siliconflow':
        raise HTTPException(400, 'SiliconFlow image adapter requires a SiliconFlow connection')
    config = dict(connection.config or {})
    config['_model_overrides'] = {**config.get('_model_overrides', {}), form.upstream_id: form.uses}
    config.pop('_models_by_user', None)
    if not any(m['upstream_id'] == form.upstream_id for m in connection.models or []):
        from open_webui.utils.providers import stable_model_id

        config['model_ids'] = list(dict.fromkeys([*config.get('model_ids', []), form.upstream_id]))
        connection.models = [
            *(connection.models or []),
            {
                'id': stable_model_id(connection.id, form.upstream_id),
                'upstream_id': form.upstream_id,
                'name': form.upstream_id,
            },
        ]
    connection.config = config
    await db.commit()
    return public_connection(connection)


class ModelTest(BaseModel):
    upstream_id: str = Field(min_length=1, max_length=256)
    purpose: str
    voice: str = Field(default='', max_length=256)
    audio_base64: str | None = Field(default=None, max_length=2_800_000)
    filename: str | None = Field(default=None, max_length=256)


@router.post('/connections/{connection_id}/model-test')
async def test_model(
    connection_id: str, form: ModelTest, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    import base64

    import aiohttp
    from open_webui.utils.provider_capabilities import fingerprint
    from open_webui.utils.providers import (
        get_media_api_connection,
        media_http_session,
        provider_base_url,
        provider_http_error,
        read_bounded_response,
        resolve_media_model,
        resolve_model,
        stable_model_id,
    )

    connection = await _connection_with_manage_access(connection_id, user, db)
    if connection.auth_type != 'api_key':
        raise HTTPException(400, 'Use an ordinary chat to test account models')
    model_id = stable_model_id(connection_id, form.upstream_id)
    resolved = (
        await resolve_model(model_id, user, db)
        if form.purpose == 'chat'
        else await resolve_media_model(model_id, user, form.purpose, db)
    )
    before = fingerprint(connection, resolved.model_metadata)
    connection, key = await get_media_api_connection(connection_id, user, db)
    base = provider_base_url(connection)
    payload = None
    data = None
    if form.purpose == 'chat':
        if resolved.model_metadata['uses']['chat'] == 'responses':
            path, payload = '/responses', {'model': form.upstream_id, 'input': 'Reply OK.', 'max_output_tokens': 32}
        else:
            path, payload = (
                '/chat/completions',
                {'model': form.upstream_id, 'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 32},
            )
    elif form.purpose == 'transcription':
        try:
            audio = base64.b64decode(form.audio_base64 or '', validate=True)
        except ValueError:
            raise HTTPException(400, 'Invalid audio sample') from None
        if not audio or len(audio) > 2 * 1024 * 1024:
            raise HTTPException(400, 'Select a short audio sample under 2 MB')
        from pathlib import Path

        extension = Path(form.filename or '').suffix.lower()
        if extension not in ('.wav', '.mp3', '.m4a', '.ogg', '.webm', '.flac', '.aac'):
            raise HTTPException(400, 'Unsupported audio sample format')
        data = aiohttp.FormData()
        data.add_field('model', form.upstream_id)
        data.add_field('file', audio, filename='sample' + extension, content_type='application/octet-stream')
        path = '/audio/transcriptions'
    elif form.purpose == 'speech':
        if not form.voice.strip():
            raise HTTPException(400, 'Select a voice for this model')
        path, payload = (
            '/audio/speech',
            {'model': form.upstream_id, 'voice': form.voice, 'input': '你好，这是语音测试。', 'response_format': 'mp3'},
        )
    elif form.purpose == 'image':
        silicon = resolved.model_metadata['uses']['image'] == 'siliconflow-image'
        path, payload = (
            '/images/generations',
            {
                'model': form.upstream_id,
                'prompt': 'A simple blue circle on white.',
                **({'image_size': '1024x1024'} if silicon else {'size': '1024x1024', 'n': 1}),
            },
        )
    else:
        raise HTTPException(400, 'Unsupported test purpose')
    outcome = 'passed'
    try:
        # Explicit test only: exactly one upstream inference, never retry uncertain calls.
        async with media_http_session(connection) as session:
            async with session.post(
                base + path,
                json=payload,
                data=data,
                headers={'Authorization': f'Bearer {key}'},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if not 200 <= response.status < 300:
                    raise provider_http_error(response.status)
                raw = await read_bounded_response(response)
                if form.purpose == 'speech':
                    if not raw or not response.headers.get('Content-Type', '').startswith('audio/'):
                        raise HTTPException(502, 'Provider did not return audio')
                else:
                    import json

                    result = json.loads(raw)
                    if form.purpose == 'transcription' and not isinstance(result.get('text'), str):
                        raise HTTPException(502, 'Provider did not return transcription text')
                    if form.purpose == 'image' and not (result.get('images') or result.get('data')):
                        raise HTTPException(502, 'Provider did not return images')
                    if form.purpose == 'chat' and not (result.get('choices') or result.get('output')):
                        raise HTTPException(502, 'Provider did not return a chat response')
    except Exception:
        # Never persist or expose upstream error bodies which may contain credentials.
        outcome = 'failed'
    await db.refresh(connection)
    current = normalize_model(connection, resolved.model_metadata)
    if fingerprint(connection, current) != before:
        raise HTTPException(409, 'Configuration changed during the test; result was not saved')
    config = dict(connection.config or {})
    tests = dict(config.get('_model_tests', {}))
    tests[form.upstream_id] = {
        **tests.get(form.upstream_id, {}),
        form.purpose: {'status': outcome, 'tested_at': int(time.time()), 'fingerprint': before},
    }
    connection.config = {**config, '_model_tests': tests}
    await db.commit()
    return {
        'status': outcome,
        'message': 'No automatic retry. A failed or timed-out request may already have been charged.',
    }


_MEDIA_SETTINGS = {
    'transcription': (
        'audio.stt',
        'engine',
        'openai',
        'openai.api_base_url',
        'openai.api_key',
        'model',
        'provider_connection_id',
    ),
    'speech': (
        'audio.tts',
        'engine',
        'openai',
        'openai.api_base_url',
        'openai.api_key',
        'model',
        'provider_connection_id',
    ),
    'image': ('image_generation', 'engine', 'openai', None, None, 'model', 'provider_connection_id'),
}


async def _migration_source(purpose, db=None):
    import hashlib
    import json

    from open_webui.models.config import Config

    if purpose not in _MEDIA_SETTINGS:
        raise HTTPException(400, 'Unsupported migration purpose')
    prefix, engine, old_engine, url, key, model, reference = _MEDIA_SETTINGS[purpose]
    url_key = f'{prefix}.{url}' if url else 'image_generation.openai.api_base_url'
    secret_key = f'{prefix}.{key}' if key else 'image_generation.openai.api_key'

    async def read(key):
        if db is None or not Config.persistent_enabled_for(key):
            return await Config.get(key)
        row = await db.get(Config, key)
        return row.value if row else Config.default_value(key)

    values = {
        'engine': await read(f'{prefix}.{engine}'),
        'url': await read(url_key),
        'key': await read(secret_key),
        'model': await read(f'{prefix}.{model}'),
    }
    digest = hashlib.sha256(json.dumps({'purpose': purpose, **values}, sort_keys=True).encode()).hexdigest()
    return values, digest


@router.get('/media-migration')
async def migration_preview(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    from open_webui.utils.providers import decrypt_credential, provider_base_url

    result = []
    connections = (
        await db.scalars(
            select(ProviderConnection).where(
                ProviderConnection.owner_id == user.id,
                ProviderConnection.visibility == 'private',
                ProviderConnection.is_active.is_(True),
            )
        )
    ).all()
    for purpose in _MEDIA_SETTINGS:
        values, digest = await _migration_source(purpose, db)
        if values['engine'] != 'openai':
            continue
        candidates = [
            c.id
            for c in connections
            if c.auth_type == 'api_key'
            and provider_base_url(c) == (values['url'] or '').rstrip('/')
            and decrypt_credential(c.credential) == values['key']
        ]
        result.append(
            {
                'purpose': purpose,
                'base_url': values['url'],
                'model': values['model'],
                'configured': bool(values['url'] and values['key'] and values['model']),
                'fingerprint': digest,
                'visibility': 'private',
                'matching_connections': candidates,
            }
        )
    return result


class MediaMigration(BaseModel):
    purpose: str
    fingerprint: str = Field(min_length=64, max_length=64)


@router.post('/media-migration')
async def migrate_media(
    form: MediaMigration, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)
):
    import uuid
    from urllib.parse import urlsplit

    from open_webui.models.config import Config
    from open_webui.utils.providers import stable_model_id

    purpose = form.purpose
    values, digest = await _migration_source(purpose, db)
    # A deterministic migration marker makes retries idempotent, without exposing secrets.
    rows = (await db.scalars(select(ProviderConnection).where(ProviderConnection.owner_id == user.id))).all()
    existing = next((c for c in rows if (c.config or {}).get('_migration') == form.fingerprint and c.is_active), None)
    if values['engine'] == 'provider' and existing:
        return public_connection(existing)
    if values['engine'] != 'openai' or digest != form.fingerprint:
        raise HTTPException(409, 'Legacy settings changed; refresh migration preview')
    if not all(values.get(key) for key in ('url', 'key', 'model')):
        raise HTTPException(400, 'Legacy configuration is incomplete')
    host = urlsplit(values['url']).hostname
    provider = 'siliconflow' if host in ('api.siliconflow.cn', 'api.siliconflow.com') else 'custom'
    validate_app_url(values['url'])
    from open_webui.utils.provider_capabilities import DEFAULT_ADAPTER

    adapter = 'siliconflow-image' if provider == 'siliconflow' and purpose == 'image' else DEFAULT_ADAPTER[purpose]
    connection = new_provider_connection(
        provider_id=provider,
        name=f'Imported {purpose}',
        auth_type='api_key',
        visibility='private',
        owner_id=user.id,
        credential=encrypt_credential(values['key']),
        status='configured',
        is_active=True,
        models=[],
        config={
            'base_url': values['url'],
            'model_ids': [values['model']],
            '_catalog_version': 1,
            '_migration': form.fingerprint,
            '_model_overrides': {values['model']: {purpose: adapter}},
        },
    )
    connection.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'media-migration:{user.id}:{form.fingerprint}'))
    db.add(connection)
    prefix, engine, _, _, _, model, reference = _MEDIA_SETTINGS[purpose]
    await Config.upsert(
        {
            f'{prefix}.{engine}': 'provider',
            f'{prefix}.{reference}': connection.id,
            f'{prefix}.{model}': stable_model_id(connection.id, values['model']),
        },
        db=db,
    )
    await db.commit()
    return public_connection(connection)
