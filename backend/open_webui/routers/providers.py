"""Provider connection catalog and user-scoped connections."""

import time
import re

from fastapi import APIRouter, Depends, HTTPException
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrant, AccessGrants
from open_webui.models.provider_connections import (
    ProviderConnection,
    ProviderConnectionCreate,
    ProviderConnectionUpdate,
    new_provider_connection,
)
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.providers import (
    PROVIDER_CATALOG,
    can_access_connection,
    discover_connection_models,
    encrypt_credential,
    public_connection,
    require_connection_access,
    verify_provider_connection,
)
from open_webui.utils.apps import validate_app_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BAILIAN_REGIONS = {'cn-beijing', 'ap-southeast-1', 'cn-hongkong', 'eu-central-1', 'us-east-1', 'ap-northeast-1'}
_BAILIAN_WORKSPACE_LABEL = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')

router = APIRouter()


def _validate_form(form: ProviderConnectionCreate, user):
    if form.visibility == 'shared' and user.role != 'admin':
        raise HTTPException(403, 'Only administrators can create shared provider connections')
    if form.provider_id == 'chatgpt':
        raise HTTPException(400, 'Connect ChatGPT through its OAuth sign-in flow')
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
        config=config,
        credential=encrypt_credential(form.api_key.get_secret_value() if form.api_key else None),
        status='configured',
        models=[],
        is_active=True,
    )
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
        config=config,
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
    if connection.provider_id == 'chatgpt' and connection.auth_type == 'oauth':
        if set(values) - {'name'} or values.get('name') is None:
            raise HTTPException(400, 'Only the ChatGPT connection name can be edited here')
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
        if connection.provider_id == 'chatgpt' and connection.auth_type == 'oauth':
            raise HTTPException(400, 'Manage ChatGPT OAuth through its provider settings')
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
        connection.config = config
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
    if connection.provider_id == 'chatgpt' and connection.auth_type == 'oauth':
        raise HTTPException(400, 'Disconnect ChatGPT through its OAuth settings')
    await db.delete(connection)
    await AccessGrants.replace_access_grants(db, 'provider_connection', connection_id, [])
    await db.commit()
    return {'ok': True}


@router.post('/connections/{connection_id}/verify')
async def verify_connection(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_manage_access(connection_id, user, db)
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
    verified = await verify_provider_connection(connection, user)
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
async def get_visible_models(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    from open_webui.utils.providers import list_visible_models

    models = await list_visible_models(user, db)
    return {
        'object': 'list',
        'data': models,
    }
