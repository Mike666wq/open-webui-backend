"""Provider connection catalog and user-scoped connections."""

import re
import time
import uuid
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
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
from open_webui.utils.provider_capabilities import ADAPTERS, provider_family, validate_model_uses
from open_webui.utils.provider_errors import (
    ProviderRequestError,
    is_provider_error,
    provider_error_response,
)
from open_webui.utils.provider_media import (
    MEDIA_PURPOSES,
    resolve_service,
    validate_service_config,
)
from open_webui.utils.provider_registry import (
    GROUPS,
    SERVICE_CONTRACT_VERSION,
    require_supported_template,
    resolve_template,
)
from open_webui.utils.providers import (
    PROVIDER_CATALOG,
    can_access_connection,
    catalog_error,
    catalog_fingerprint,
    catalog_snapshot,
    catalog_status,
    connection_models,
    discover_connection_models,
    encrypt_credential,
    initialize_model_catalog,
    legacy_model_access,
    public_connection,
    refresh_connection_catalog,
    registry_migrated,
    require_connection_access,
    resolved_services,
    seed_connection_models,
    verify_provider_connection,
    visible_catalog_statuses,
    write_snapshot,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BAILIAN_REGIONS = {'cn-beijing', 'ap-southeast-1', 'cn-hongkong', 'eu-central-1', 'us-east-1', 'ap-northeast-1'}
_BAILIAN_WORKSPACE_LABEL = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')

# The purposes a connection can serve. `chat` keeps its legacy meaning; the
# three media purposes are configured one by one.
CONFIGURED_PURPOSES = ('chat', *MEDIA_PURPOSES)

router = APIRouter()


def _template_for(form: ProviderConnectionCreate) -> dict:
    """Resolve the provider template of a create request.

    A template this release cannot talk to is refused outright; the platform
    never falls back to the OpenAI protocol for it. The template named by the
    client wins over the legacy field, so a client that only sends
    `provider_id=custom` cannot erase the MiniMax template it picked.
    """
    try:
        template = resolve_template(form.provider_template, form.provider_id)
        template = require_supported_template(template['id'])
    except ProviderRequestError as exc:
        raise HTTPException(400, exc.detail) from None
    if form.protocol and form.protocol != template['protocol']:
        raise HTTPException(400, f'{template["name"]} uses the {template["protocol"]} protocol')
    return template


def _service_context(template: dict, config: dict, auth_type: str):
    """A connection-shaped view of a connection that is not saved yet.

    The service declarations of a template must be validated before the
    connection exists; this gives the validators the same view they will get
    once the row is written.
    """
    return SimpleNamespace(
        id='',
        provider_id=template['id'],
        provider_template=template['id'],
        auth_type=auth_type,
        config=config or {},
        models=[],
    )


def _validate_services(template: dict, config: dict, auth_type: str) -> dict:
    """Validate the `services` block of a connection configuration."""
    services = config.get('services')
    if services is None:
        return {}
    if not isinstance(services, dict):
        raise HTTPException(400, '服务配置必须是对象')
    unknown = set(services) - set(CONFIGURED_PURPOSES)
    if unknown:
        raise HTTPException(400, 'Unsupported provider service field')
    context = _service_context(template, config, auth_type)
    cleaned = {}
    for purpose, block in services.items():
        cleaned[purpose] = validate_service_config(context, purpose, block)
    return cleaned


def _validate_form(form: ProviderConnectionCreate, user):
    template = _template_for(form)
    if form.visibility == 'shared' and user.role != 'admin':
        raise HTTPException(403, 'Only administrators can create shared provider connections')
    if template['id'] in ('chatgpt', 'kimi'):
        raise HTTPException(400, f'Connect {template["id"]} through its OAuth sign-in flow')
    elif form.auth_type != 'api_key':
        raise HTTPException(400, 'This provider requires an API key')
    elif not form.api_key:
        raise HTTPException(400, 'An API key is required')
    config = dict(form.config or {})
    allowed = {'base_url', 'region', 'workspace_id', 'model_ids', 'api_type', 'services'}
    if set(config) - allowed:
        raise HTTPException(400, 'Unsupported provider configuration field')
    for key in ('region', 'workspace_id', 'api_type'):
        if key in config and (not isinstance(config[key], str) or len(config[key]) > 128):
            raise HTTPException(400, f'{key} must be a string no longer than 128 characters')
    if template['id'] == 'custom' and config.get('api_type', 'openai') not in ('openai', 'responses'):
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
    if template['id'] == 'custom' and not config.get('base_url'):
        raise HTTPException(400, 'A base URL is required for custom providers')
    _validate_bailian_config(template['id'], config)
    services = _validate_services(template, config, form.auth_type)
    if services:
        config['services'] = services
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


def _drop_redundant_legacy_models(connection):
    """Clear the legacy model column only when the curated whitelist is authoritative.

    A connection that has not been migrated yet keeps its legacy models: the
    migration still has to move them into the curated whitelist, so clearing
    them here would lose the user's model selection.
    """
    if registry_migrated(connection):
        connection.models = []


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
    """The provider templates, grouped by the protocol that decides a call."""
    return {
        'object': 'list',
        'data': PROVIDER_CATALOG,
        'groups': GROUPS,
        'service_contract_version': SERVICE_CONTRACT_VERSION,
    }


@router.get('/connections')
async def list_connections(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    connections = (
        await db.scalars(
            select(ProviderConnection).where(ProviderConnection.is_active.is_(True)).order_by(ProviderConnection.name)
        )
    ).all()
    from open_webui.models.models import Model

    disabled = set(await db.scalars(select(Model.id).where(Model.is_active.is_(False))))
    visible = []
    for connection in connections:
        if await can_access_connection(connection, user, db):
            item = await public_connection(connection, db)
            if user.role != 'admin' and connection.owner_id != user.id:
                item['models'] = [
                    m for m in item['models'] if await legacy_model_access(connection, m['upstream_id'], user, db)
                ]
            aliases = (connection.config or {}).get('_legacy_aliases', {})
            for model in item['models']:
                if model['id'] in disabled or any(
                    alias in disabled and upstream == model['upstream_id'] for alias, upstream in aliases.items()
                ):
                    model['enabled'] = False
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
    """Probe a connection without saving it.

    The probe follows the services the user configured. A service whose
    discovery is `preset` or `manual` never triggers a catalog call, so a
    MiniMax media connection can be added and saved without a remote model
    list. One failing service never hides the others.
    """
    config = _validate_form(form, user)
    template = _template_for(form)
    connection = new_provider_connection(
        provider_id=template['id'],
        name=form.name,
        auth_type=form.auth_type,
        visibility=form.visibility,
        owner_id=user.id,
        config={**config, '_registry_version': 2},
        credential=encrypt_credential(form.api_key.get_secret_value() if form.api_key else None),
        status='configured',
        models=[],
        is_active=True,
        protocol=template['protocol'],
        provider_template=template['id'],
    )
    await initialize_model_catalog(connection)
    services = resolved_services(connection)
    results: dict = {}
    failures: list = []
    models: list = []
    verified = True
    probeable = [
        purpose
        for purpose, entry in services.items()
        if entry['supported'] and entry['enabled'] and entry['discovery'] == 'remote'
    ]
    for purpose, entry in services.items():
        if not entry['supported']:
            results[purpose] = {'status': 'not_applicable'}
            continue
        if not entry['enabled']:
            results[purpose] = {'status': 'disabled'}
            continue
        if entry['discovery'] != 'remote':
            # A preset or manual service has no catalog to fetch. It reports
            # "not applicable", never a catalog failure.
            results[purpose] = {'status': 'not_applicable', 'models': entry['preset_models']}
            continue
        try:
            if purpose == 'chat':
                verified = await verify_provider_connection(connection, user)
                models = await discover_connection_models(connection, user)
                results[purpose] = {'status': 'connected' if verified else 'manual', 'models': models}
            else:
                await verify_provider_connection(connection, user)
                results[purpose] = {'status': 'connected'}
        except HTTPException as exc:
            # The taxonomy travels with the response: the wizard needs the
            # stage, the reason and the suggested action to offer a recovery
            # entry.
            if not is_provider_error(exc):
                raise
            failures.append((purpose, exc))
            results[purpose] = {'status': 'failed', 'error': catalog_error(exc)}
    if failures and probeable == ['chat']:
        # The legacy single-service flow keeps its error response.
        return provider_error_response(failures[0][1])
    ok = verified and not failures
    return {
        'ok': ok,
        'status': 'connected' if ok else ('failed' if failures else 'manual'),
        'models': models,
        'services': results,
    }


@router.post('/connections')
async def create_connection(
    form: ProviderConnectionCreate, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    config = _validate_form(form, user)
    template = _template_for(form)
    selections = form.model_selections or []
    model_ids = [item for item in (config.get('model_ids') or []) if isinstance(item, str)]
    if selections and model_ids:
        # The two fields coexist for compatibility, but a request that states
        # both differently is refused instead of being silently resolved.
        if {selection.upstream_id for selection in selections} != set(model_ids):
            raise HTTPException(400, '模型选择与 model_ids 不一致，请只提交其中一种')
    context = _service_context(template, config, form.auth_type)
    validated = []
    for selection in selections:
        validated.append(
            {
                'upstream_id': selection.upstream_id,
                'display_name': selection.display_name,
                'uses': validate_model_uses(context, selection.uses),
                'source': selection.source,
            }
        )
    connection = new_provider_connection(
        provider_id=template['id'],
        name=form.name,
        auth_type=form.auth_type,
        visibility=form.visibility,
        owner_id=user.id,
        config={**config, '_registry_version': 2},
        credential=encrypt_credential(form.api_key.get_secret_value() if form.api_key else None),
        status='configured',
        models=[],
        is_active=True,
        protocol=template['protocol'],
        provider_template=template['id'],
    )
    db.add(connection)
    if form.visibility == 'shared':
        await AccessGrants.replace_access_grants(db, 'provider_connection', connection.id, form.access_control)
    # The models the user picked become the curated whitelist, in the same
    # transaction as the connection and its sharing rules.
    upstream_ids = [selection['upstream_id'] for selection in validated] or model_ids
    await seed_connection_models(connection, upstream_ids, db, selections=validated or None)
    await db.commit()
    return await public_connection(connection, db)


@router.get('/connections/{connection_id}')
async def get_connection(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_access(connection_id, user, db)
    result = await public_connection(connection, db)
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
        allowed = {'base_url', 'region', 'workspace_id', 'model_ids', 'api_type', 'services'}
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
        if 'services' in config:
            template = resolve_template(connection.provider_template, connection.provider_id)
            context = _service_context(template, config, connection.auth_type)
            cleaned = {}
            for purpose, block in (config['services'] or {}).items():
                if purpose not in CONFIGURED_PURPOSES:
                    raise HTTPException(400, 'Unsupported provider service field')
                cleaned[purpose] = validate_service_config(context, purpose, block)
            config['services'] = cleaned
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
        _drop_redundant_legacy_models(connection)
        connection.status = 'configured'
    await db.commit()
    return await public_connection(connection, db)


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
    try:
        models = await refresh_connection_catalog(connection, user, db)
    except HTTPException as exc:
        if not is_provider_error(exc):
            raise
        return provider_error_response(exc)
    return {
        'ok': connection.status == 'connected',
        'status': connection.status,
        'models': models,
        'catalog': catalog_status(connection, user.id, await catalog_snapshot(connection, user.id, db)),
    }


@router.post('/connections/{connection_id}/models/refresh')
async def refresh_models(
    connection_id: str, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    connection = await _connection_with_manage_access(connection_id, user, db)
    models = await refresh_connection_catalog(connection, user, db)
    return {
        'models': models,
        'status': connection.status,
        'catalog': catalog_status(connection, user.id, await catalog_snapshot(connection, user.id, db)),
    }


@router.get('/models')
async def get_visible_models(
    purpose: str = 'chat',
    operation: str | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    from open_webui.utils.providers import list_visible_models, service_enabled

    errors = []
    if purpose == 'chat':
        models = await list_visible_models(user, db)
    else:
        from open_webui.utils.provider_capabilities import ADAPTER_OPERATIONS
        from open_webui.utils.providers import visible_connection_models

        if purpose not in (*ADAPTERS, 'all'):
            raise HTTPException(400, 'Unknown model purpose')
        if operation is not None and operation not in ('generate', 'edit'):
            raise HTTPException(400, 'Unknown model operation')
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
                    if (purpose == 'all' or (m.get('enabled', True) and purpose in m.get('uses', {})))
                    and service_enabled(connection, purpose)
                    and (
                        operation is None
                        or operation in (ADAPTER_OPERATIONS.get(m.get('uses', {}).get(purpose), ()) or ())
                    )
                )
    return {
        'object': 'list',
        'data': models,
        'errors': errors,
        'catalogs': await visible_catalog_statuses(user, db),
    }


class ModelEnabled(BaseModel):
    upstream_id: str = Field(min_length=1, max_length=256)
    enabled: bool


@router.put('/connections/{connection_id}/model-enabled')
async def set_model_enabled(
    connection_id: str,
    form: ModelEnabled,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    connection = await _connection_with_manage_access(connection_id, user, db)
    from open_webui.utils.providers import set_connection_model_enabled

    if not await set_connection_model_enabled(connection, form.upstream_id, form.enabled, db):
        if not any(m.get('upstream_id') == form.upstream_id for m in connection.models or []):
            raise HTTPException(404, 'Discover or add the model first')
        connection.config = {
            **(connection.config or {}),
            '_model_enabled': {
                **(connection.config or {}).get('_model_enabled', {}),
                form.upstream_id: form.enabled,
            },
        }
    from open_webui.models.models import Model
    from open_webui.utils.providers import stable_model_id
    from sqlalchemy import update

    ids = [
        stable_model_id(connection.id, form.upstream_id),
        *[
            alias
            for alias, upstream in (connection.config or {}).get('_legacy_aliases', {}).items()
            if upstream == form.upstream_id
        ],
    ]
    await db.execute(update(Model).where(Model.id.in_(ids)).values(is_active=form.enabled))
    await db.commit()
    return await public_connection(connection, db)


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
    # The same validation as the create path: a purpose the template does not
    # serve cannot be attached to one of its models.
    uses = validate_model_uses(connection, form.uses)
    from open_webui.utils.providers import set_connection_model_uses

    if not await set_connection_model_uses(connection, form.upstream_id, uses, db):
        config = dict(connection.config or {})
        config['_model_overrides'] = {**config.get('_model_overrides', {}), form.upstream_id: uses}
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
    return await public_connection(connection, db)


class ConnectionEnabled(BaseModel):
    enabled: bool


@router.put('/connections/{connection_id}/enabled')
async def set_connection_enabled(
    connection_id: str,
    form: ConnectionEnabled,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The provider level switch: when off, none of its models are offered."""
    connection = await _connection_with_manage_access(connection_id, user, db)
    connection.is_enabled = bool(form.enabled)
    connection.updated_at = int(time.time())
    await db.commit()
    return await public_connection(connection, db)


@router.get('/connections/{connection_id}/models')
async def list_connection_models(
    connection_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The curated whitelist of one provider."""
    connection = await _connection_with_access(connection_id, user, db)
    return {'object': 'list', 'data': await connection_models(connection, db)}


@router.post('/connections/{connection_id}/models/fetch')
async def fetch_connection_models(
    connection_id: str,
    purpose: str | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Fetch the candidate pool from the provider.

    The result never changes the whitelist: the user picks what to keep. A
    service whose models come from the official presets has no catalog to
    fetch; it answers "not applicable" instead of pretending a catalog call
    failed.
    """
    from open_webui.utils.provider_capabilities import service_declaration

    connection = await _connection_with_manage_access(connection_id, user, db)
    if purpose:
        declaration = service_declaration(connection, purpose)
        if not declaration.get('supported'):
            raise HTTPException(
                400, declaration.get('unsupported_reason') or '该提供商不支持此服务'
            )
        if declaration.get('discovery') != 'remote':
            return {
                'candidates': [
                    {
                        'upstream_id': model['upstream_id'],
                        'display_name': model.get('display_name') or model['upstream_id'],
                        'capabilities': [],
                        'uses': model.get('uses') or {},
                    }
                    for model in declaration.get('preset_models') or []
                ],
                'error': None,
                'status': 'not_applicable',
                'fetched_at': int(time.time()),
            }
    stamp = catalog_fingerprint(connection)
    try:
        candidates = await discover_connection_models(connection, user)
    except Exception as exc:
        error = catalog_error(exc)
        await write_snapshot(
            connection,
            user.id,
            {'fingerprint': stamp, 'models': [], 'error': error, 'generation': '', 'updated_at': int(time.time())},
            db,
        )
        await db.commit()
        return {'candidates': [], 'error': error, 'status': 'failed', 'fetched_at': int(time.time())}
    await write_snapshot(
        connection,
        user.id,
        {
            'fingerprint': stamp,
            'models': candidates,
            'error': None,
            'generation': str(uuid.uuid4()),
            'updated_at': int(time.time()),
        },
        db,
    )
    connection.status = 'connected'
    await db.commit()
    return {
        'candidates': [
            {
                'upstream_id': candidate['upstream_id'],
                'display_name': candidate.get('name') or candidate['upstream_id'],
                'capabilities': candidate.get('capabilities') or [],
                'uses': candidate.get('uses') or {},
            }
            for candidate in candidates
        ],
        'error': None,
        'status': 'ok',
        'fetched_at': int(time.time()),
    }


class ModelAdd(BaseModel):
    upstream_ids: list[str] = Field(min_length=1, max_length=500)
    display_names: dict[str, str] = Field(default_factory=dict)
    uses: dict[str, str] | None = None
    source: Literal['provider', 'preset', 'manual'] = 'provider'


@router.post('/connections/{connection_id}/models')
async def add_connection_models(
    connection_id: str,
    form: ModelAdd,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Add the models the user picked to the curated whitelist."""
    connection = await _connection_with_manage_access(connection_id, user, db)
    if form.uses is not None:
        form.uses = validate_model_uses(connection, form.uses)
    if form.source == 'preset':
        from open_webui.utils.provider_capabilities import service_declaration

        declared = [
            item
            for purpose in (form.uses or {})
            for item in service_declaration(connection, purpose).get('preset_models') or []
        ]
        if not form.uses or any(
            not any(item.get('upstream_id') == model and item.get('uses') == form.uses for item in declared)
            for model in form.upstream_ids
        ):
            raise HTTPException(400, 'Select an official preset for this service')
    added = await seed_connection_models(
        connection,
        form.upstream_ids,
        db,
        display_names=form.display_names,
        uses=form.uses,
        source=form.source,
        user_id=user.id,
    )
    await db.commit()
    return {'object': 'list', 'added': added, 'data': await connection_models(connection, db)}


class ModelUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=256)
    uses: dict[str, str] | None = None
    enabled: bool | None = None


@router.put('/connections/{connection_id}/models/{upstream_id}')
async def update_connection_model(
    connection_id: str,
    upstream_id: str,
    form: ModelUpdate,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Rename a curated model, change its purposes or switch it off."""
    connection = await _connection_with_manage_access(connection_id, user, db)
    if form.uses is not None:
        # The same validation as the create path: a purpose the template does
        # not serve cannot be attached to one of its models.
        form.uses = validate_model_uses(connection, form.uses)
    from open_webui.models.provider_connections import ProviderModel

    row = (
        await db.scalars(
            select(ProviderModel).where(
                ProviderModel.connection_id == connection.id,
                ProviderModel.upstream_id == upstream_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, 'Model not found')
    if form.display_name is not None:
        row.display_name = form.display_name
    if form.uses is not None:
        row.uses = dict(form.uses)
    if form.enabled is not None:
        row.enabled = bool(form.enabled)
    row.updated_at = int(time.time())
    await db.commit()
    return {'object': 'list', 'data': await connection_models(connection, db)}


@router.delete('/connections/{connection_id}/models/{upstream_id}')
async def remove_connection_model(
    connection_id: str,
    upstream_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Remove a model from the curated whitelist. The provider keeps it."""
    connection = await _connection_with_manage_access(connection_id, user, db)
    from open_webui.models.provider_connections import ProviderModel

    row = (
        await db.scalars(
            select(ProviderModel).where(
                ProviderModel.connection_id == connection.id,
                ProviderModel.upstream_id == upstream_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, 'Model not found')
    await db.delete(row)
    await db.commit()
    return {'object': 'list', 'data': await connection_models(connection, db)}


class ModelTest(BaseModel):
    upstream_id: str = Field(min_length=1, max_length=256)
    purpose: str
    voice: str = Field(default='', max_length=256)
    audio_base64: str | None = Field(default=None, max_length=2_800_000)
    filename: str | None = Field(default=None, max_length=256)
    response_format: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=32)
    text: str | None = Field(default=None, max_length=20_000)
    prompt: str | None = Field(default=None, max_length=4_000)
    size: str | None = Field(default=None, max_length=32)
    count: int | None = Field(default=None, ge=1, le=9)
    aspect_ratio: str | None = Field(default=None, max_length=16)


def _parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = size.lower().split('x', 1)
        return int(width), int(height)
    except (AttributeError, ValueError):
        raise HTTPException(400, '图片尺寸格式无效，请使用 宽x高 的形式') from None


def _media_test_payload(form: ModelTest, adapter: str) -> dict:
    """Build the input of an explicit media test.

    The test uses the same adapter and the same limits as the daily feature;
    only the input differs, and it comes from the operator.
    """
    if form.purpose == 'transcription':
        import base64
        from pathlib import Path

        try:
            audio = base64.b64decode(form.audio_base64 or '', validate=True)
        except ValueError:
            raise HTTPException(400, 'Invalid audio sample') from None
        if not audio or len(audio) > 2 * 1024 * 1024:
            raise HTTPException(400, 'Select a short audio sample under 2 MB')
        extension = Path(form.filename or '').suffix.lower()
        if extension not in ('.wav', '.mp3', '.m4a', '.ogg', '.webm', '.flac', '.aac'):
            raise HTTPException(400, 'Unsupported audio sample format')
        return {
            'audio': audio,
            'filename': form.filename or f'sample{extension}',
            'response_format': form.response_format,
            'language': form.language,
        }
    if form.purpose == 'speech':
        return {'text': form.text or '你好，这是语音测试。', 'voice': form.voice or None}
    if form.purpose == 'image':
        payload = {
            'prompt': form.prompt or 'A simple blue circle on white.',
            'n': form.count or 1,
        }
        size = form.size or '1024x1024'
        if adapter == 'minimax-image':
            width, height = _parse_size(size)
            payload['width'] = width
            payload['height'] = height
            if form.aspect_ratio:
                payload['aspect_ratio'] = form.aspect_ratio
        else:
            payload['size'] = size
        return payload
    raise HTTPException(400, 'Unsupported test purpose')


def _media_test_usage(purpose: str, result: dict, payload: dict) -> dict | None:
    """Collect the units the provider actually reported."""
    if purpose == 'transcription':
        duration = (result or {}).get('duration')
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            return {'duration_seconds': round(float(duration), 3)}
        return None
    if purpose == 'speech':
        characters = len((payload or {}).get('text') or '')
        return {'characters': characters} if characters else None
    if purpose == 'image':
        images = len((result or {}).get('images') or [])
        return {'images': images} if images else None
    return None


@router.post('/connections/{connection_id}/model-test')
async def test_model(
    connection_id: str, form: ModelTest, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    return await _test_model(connection_id, form, user, db)


async def _test_model(
    connection_id: str, form: ModelTest, user, db, *, include_sample=False, start_time=None, expected_fingerprint=None
):
    import time

    from open_webui.utils.provider_capabilities import fingerprint
    from open_webui.utils.provider_media import call_media_service, resolve_service
    from open_webui.utils.providers import (
        get_media_api_connection,
        provider_chat_completion,
        resolve_media_model,
        resolve_model,
        stable_model_id,
    )

    connection = await _connection_with_manage_access(connection_id, user, db)
    if connection.auth_type != 'api_key' and form.purpose != 'chat':
        raise HTTPException(400, 'Account connections support chat testing only')
    model_id = stable_model_id(connection_id, form.upstream_id)
    resolved = (
        await resolve_model(model_id, user, db, allow_disabled=True)
        if form.purpose == 'chat'
        else await resolve_media_model(model_id, user, form.purpose, db, allow_disabled=True)
    )
    before = fingerprint(connection, resolved.model_metadata)
    if expected_fingerprint is not None and before != expected_fingerprint:
        raise HTTPException(409, 'Configuration changed before the test; no request was sent')
    started = start_time if start_time is not None else time.monotonic()
    if form.purpose == 'chat':
        import asyncio

        from open_webui.utils.providers import catalog_error

        outcome, error, usage = 'passed', None, None
        try:
            async with asyncio.timeout(120):
                result = await provider_chat_completion(
                    model_id,
                    {'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'stream': False, 'max_tokens': 32},
                    user,
                    db,
                    allow_disabled=True,
                )
            if not isinstance(result, dict) or not (result.get('choices') or result.get('output')):
                raise HTTPException(502, 'Provider did not return a chat response')
            usage = result.get('usage') if isinstance(result, dict) else None
        except Exception as exc:
            outcome, error = 'failed', catalog_error(exc)
        return await _save_model_test(
            connection, resolved.model_metadata, before, form.purpose, outcome, error, started, user, db, usage
        )
    connection, key = await get_media_api_connection(connection_id, user, db)
    outcome, error, usage = 'passed', None, None
    adapter = resolved.model_metadata['uses'].get(form.purpose)
    if not adapter:
        raise HTTPException(400, 'Select a model supporting this purpose')
    payload = _media_test_payload(form, adapter)
    service = resolve_service(connection, form.purpose, adapter)
    params_fingerprint = _params_fingerprint(payload)
    outcome, error, usage = 'passed', None, None
    try:
        # The explicit test and the daily feature share the adapter, the
        # address resolution, the input limits and the response parsing.
        result = await call_media_service(
            connection, service, model=form.upstream_id, credential=key, payload=payload
        )
        usage = _media_test_usage(form.purpose, result, payload)
    except Exception as exc:
        from open_webui.utils.providers import catalog_error

        outcome, error = 'failed', catalog_error(exc)
    response = await _save_model_test(
        connection,
        resolved.model_metadata,
        before,
        form.purpose,
        outcome,
        error,
        started,
        user,
        db,
        usage,
        params_fingerprint=params_fingerprint,
    )
    # The operator sees which service and model were really used. The internal
    # adapter identifier stays out of the interface.
    response['service'] = {
        'purpose': form.purpose,
        'endpoint': service.endpoint,
        'model': form.upstream_id,
    }
    if include_sample and outcome == 'passed':
        if form.purpose == 'transcription' and isinstance(result.get('text'), str):
            response['sample_text'] = result['text'].encode('utf-8')[:8192].decode('utf-8', errors='ignore')
        elif form.purpose == 'speech' and isinstance(result.get('audio'), bytes):
            import base64

            response['sample_audio_base64'] = base64.b64encode(result['audio']).decode('ascii')
            response['sample_audio_type'] = result.get('content_type') or 'audio/mpeg'
    return response


@router.post('/connections/{connection_id}/model-test-stream')
async def test_model_stream(
    connection_id: str, form: ModelTest, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)
):
    """Stream only observed events; never start a second inference to obtain timings."""
    import json

    from open_webui.models.provider_calls import reported_usage
    from open_webui.utils.provider_capabilities import fingerprint
    from open_webui.utils.provider_stream import encode_event
    from open_webui.utils.providers import provider_chat_completion, resolve_media_model, resolve_model, stable_model_id

    connection = await _connection_with_manage_access(connection_id, user, db)
    if connection.auth_type != 'api_key' and form.purpose != 'chat':
        raise HTTPException(400, 'Account connections support chat testing only')
    model_id = stable_model_id(connection_id, form.upstream_id)
    resolved = (
        await resolve_model(model_id, user, db, allow_disabled=True)
        if form.purpose == 'chat'
        else await resolve_media_model(model_id, user, form.purpose, db, allow_disabled=True)
    )
    if form.purpose != 'chat':
        adapter = resolved.model_metadata.get('uses', {}).get(form.purpose)
        if not adapter:
            raise HTTPException(400, 'Select a model supporting this purpose')
        _media_test_payload(form, adapter)
    before = fingerprint(connection, resolved.model_metadata)
    test_id = str(uuid.uuid4())

    async def events():
        import asyncio

        started = time.monotonic()
        seq = 0
        timings = {'upstream_headers_ms': None, 'first_output_ms': None, 'upstream_total_ms': None}

        def emit(phase, data=None):
            nonlocal seq
            seq += 1
            return encode_event({'test_id': test_id, 'seq': seq, 'phase': phase,
                                 'elapsed_ms': int((time.monotonic() - started) * 1000), 'data': data or {}})

        yield emit('accepted')
        if form.purpose != 'chat':
            try:
                result = await _test_model(
                    connection_id, form, user, db, include_sample=True, start_time=started, expected_fingerprint=before
                )
                result['timings'] = timings
                yield emit('completed' if result.get('status') == 'passed' else 'failed', result)
            except Exception as exc:
                yield emit('failed', {'status': 'failed', 'elapsed_ms': int((time.monotonic() - started) * 1000),
                                      'error': catalog_error(exc), 'timings': timings})
            return

        outcome, error, usage = 'failed', None, None
        terminal = False
        preview_bytes = 0
        stream = None
        try:
            start_signal = asyncio.Event()
            task = asyncio.create_task(provider_chat_completion(
                model_id, {'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'stream': True,
                           'max_tokens': 32}, user, db, allow_disabled=True, on_request_started=start_signal.set
            ))
            signal_task = asyncio.create_task(start_signal.wait())
            try:
                await asyncio.wait((task, signal_task), return_when=asyncio.FIRST_COMPLETED)
                if start_signal.is_set():
                    yield emit('request_started')
                stream = await task
            finally:
                signal_task.cancel()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            timings['upstream_headers_ms'] = int((time.monotonic() - started) * 1000)
            yield emit('response_headers')
            async for frame in stream.body_iterator:
                raw = frame.decode('utf-8') if isinstance(frame, bytes) else frame
                if not raw.startswith('data:'):
                    raise ValueError('invalid stream frame')
                data = raw[5:].strip()
                if data == '[DONE]':
                    terminal = True
                    break
                event = json.loads(data)
                if not isinstance(event, dict) or event.get('error') or event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                    raise ValueError('upstream stream failed')
                delta = ''
                if event.get('type') == 'response.output_text.delta':
                    delta = event.get('delta') or ''
                elif isinstance(event.get('choices'), list):
                    for choice in event['choices']:
                        content = (choice.get('delta') or {}).get('content')
                        if isinstance(content, str):
                            delta += content
                if isinstance(event.get('usage'), dict):
                    usage = event['usage']
                if event.get('type') == 'response.completed':
                    terminal = True
                    usage = (event.get('response') or {}).get('usage') or usage
                if isinstance(delta, str) and delta:
                    if timings['first_output_ms'] is None:
                        timings['first_output_ms'] = int((time.monotonic() - started) * 1000)
                    available = max(0, 8192 - preview_bytes)
                    clipped = delta.encode('utf-8')[:available].decode('utf-8', errors='ignore')
                    preview_bytes += len(clipped.encode('utf-8'))
                    if clipped:
                        yield emit('output_delta', {'text': clipped})
                if terminal:
                    break
            if not terminal or timings['first_output_ms'] is None:
                raise ValueError('incomplete stream')
            timings['upstream_total_ms'] = int((time.monotonic() - started) * 1000)
            outcome = 'passed'
        except Exception as exc:
            error = catalog_error(exc)
        finally:
            if stream is not None and hasattr(stream.body_iterator, 'aclose'):
                await stream.body_iterator.aclose()
        try:
            result = await _save_model_test(connection, resolved.model_metadata, before, form.purpose,
                                            outcome, error, started, user, db, usage)
            result['timings'] = timings
            result['usage'] = reported_usage(usage)
            yield emit('completed' if outcome == 'passed' else 'failed', result)
        except Exception as exc:
            yield emit('failed', {'status': 'failed', 'elapsed_ms': int((time.monotonic() - started) * 1000),
                                  'error': catalog_error(exc), 'timings': timings})

    return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})


class VoiceFetch(BaseModel):
    purpose: str = Field(default='speech')
    voice_type: str = Field(default='system', max_length=64)


@router.post('/connections/{connection_id}/voices/fetch')
async def fetch_connection_voices(
    connection_id: str,
    form: VoiceFetch,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """List the voices of a speech service.

    The call is a read-only metadata request: it never activates a voice and
    never runs an inference. An adapter that cannot answer the question says
    so locally, without touching the provider.
    """
    import time

    from open_webui.utils.provider_media import fetch_voices, resolve_service
    from open_webui.utils.providers import get_media_api_connection

    if form.purpose != 'speech':
        raise HTTPException(400, '只有语音合成服务支持音色查询')
    connection = await _connection_with_access(connection_id, user, db)
    connection, key = await get_media_api_connection(connection_id, user, db)
    service = resolve_service(connection, 'speech')
    try:
        voices = await fetch_voices(connection, service, key, form.voice_type)
    except HTTPException:
        raise
    except Exception as exc:
        from open_webui.utils.providers import catalog_error

        error = catalog_error(exc)
        return {'data': [], 'error': error, 'fetched_at': int(time.time())}
    return {'data': voices, 'error': None, 'fetched_at': int(time.time())}


async def _save_model_test(
    connection, metadata, before, purpose, outcome, error, started, user, db, usage, params_fingerprint=None
):
    import time

    from open_webui.models.provider_calls import reported_usage, save_call
    from open_webui.utils.provider_capabilities import fingerprint

    elapsed = int((time.monotonic() - started) * 1000)
    await db.refresh(connection, with_for_update=True)
    # Refresh a curated model as well as the connection: its uses may have
    # changed while the upstream request was in flight.
    from open_webui.models.provider_connections import ProviderModel
    from open_webui.utils.providers import connection_models, registry_migrated

    if registry_migrated(connection):
        await db.execute(
            select(ProviderModel)
            .where(ProviderModel.connection_id == connection.id, ProviderModel.upstream_id == metadata['upstream_id'])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    current = next(
        (item for item in await connection_models(connection, db) if item['upstream_id'] == metadata['upstream_id']),
        None,
    )
    if current is None:
        raise HTTPException(409, 'Configuration changed during the test; result was not saved')
    if fingerprint(connection, current) != before:
        raise HTTPException(409, 'Configuration changed during the test; result was not saved')
    result = {
        'status': outcome,
        'tested_at': int(time.time()),
        'fingerprint': before,
        'elapsed_ms': elapsed,
        'error': error,
    }
    if params_fingerprint:
        result['params_fingerprint'] = params_fingerprint
    from open_webui.utils.providers import set_connection_model_verification

    if not await set_connection_model_verification(connection, metadata['upstream_id'], purpose, result, db):
        config = dict(connection.config or {})
        tests = dict(config.get('_model_tests', {}))
        tests[metadata['upstream_id']] = {**tests.get(metadata['upstream_id'], {}), purpose: result}
        connection.config = {**config, '_model_tests': tests}
    await db.commit()
    await save_call(
        user_id=user.id,
        connection_id=connection.id,
        model_id=metadata['id'],
        chat_id=None,
        message_id=None,
        purpose='test:' + purpose,
        status=outcome,
        created_at=int(time.time()),
        elapsed_ms=elapsed,
        usage=reported_usage(usage),
    )
    return {
        'status': outcome,
        'elapsed_ms': elapsed,
        'error': error,
        'message': 'No automatic retry. A failed or timed-out request may already have been charged.',
    }


def _params_fingerprint(payload: dict) -> str:
    """Fingerprint the input of a test.

    The sample content never leaves the server, but its size and digest are
    enough to tell one test input from another.
    """
    import hashlib
    import json

    summary = {}
    for key, value in (payload or {}).items():
        if key == 'audio' and isinstance(value, (bytes, bytearray)):
            summary[key] = {
                'bytes': len(value),
                'sha256': hashlib.sha256(bytes(value)).hexdigest(),
            }
        else:
            summary[key] = value
    return hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()


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
        return await public_connection(existing, db)
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
            '_registry_version': 2,
            '_migration': form.fingerprint,
        },
    )
    connection.id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'media-migration:{user.id}:{form.fingerprint}'))
    db.add(connection)
    await seed_connection_models(
        connection,
        [values['model']],
        db,
        display_names={values['model']: values['model']},
        uses={purpose: adapter},
        source='manual',
    )
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
    return await public_connection(connection, db)


@router.get('/calls/history')
async def get_call_history(
    connection_id: str | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
    admin_summary: bool = False,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    import time

    from open_webui.models.provider_calls import ProviderCall

    filters = [ProviderCall.created_at >= int(time.time()) - 30 * 86400]
    if admin_summary:
        if user.role != 'admin':
            raise HTTPException(403, 'Administrator access required')
        # Private connections remain owner-only even to administrators.
        shared = (
            await db.scalars(select(ProviderConnection.id).where(ProviderConnection.visibility == 'shared'))
        ).all()
        from sqlalchemy import or_

        filters.append(or_(ProviderCall.user_id == user.id, ProviderCall.connection_id.in_(shared)))
    else:
        filters.append(ProviderCall.user_id == user.id)
    if connection_id:
        await _connection_with_access(connection_id, user, db)
        filters.append(ProviderCall.connection_id == connection_id)
    if chat_id:
        filters.append(ProviderCall.chat_id == chat_id)
    if message_id:
        filters.append(ProviderCall.message_id == message_id)
    rows = (
        await db.scalars(select(ProviderCall).where(*filters).order_by(ProviderCall.created_at.desc()).limit(500))
    ).all()
    from sqlalchemy import func

    summaries = (
        (
            await db.execute(
                select(
                    ProviderCall.model_id,
                    ProviderCall.purpose,
                    func.count(ProviderCall.id).label('count'),
                    func.count(ProviderCall.usage['input_tokens'].as_integer()).label('input_reported'),
                    func.count(ProviderCall.usage['output_tokens'].as_integer()).label('output_reported'),
                    func.sum(ProviderCall.usage['input_tokens'].as_integer()).label('input_tokens'),
                    func.sum(ProviderCall.usage['output_tokens'].as_integer()).label('output_tokens'),
                )
                .where(*filters)
                .group_by(ProviderCall.model_id, ProviderCall.purpose)
            )
        )
        .mappings()
        .all()
    )
    return {
        'summary': [dict(row) for row in summaries],
        'data': [
            {
                k: getattr(row, k)
                for k in (
                    'id',
                    'connection_id',
                    'model_id',
                    'chat_id',
                    'message_id',
                    'purpose',
                    'status',
                    'created_at',
                    'elapsed_ms',
                    'usage',
                )
            }
            for row in rows
        ],
        'retention_days': 30,
        'limit': 500,
    }


@router.get('/legacy-migration/preview')
async def get_legacy_migration(
    request: Request, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)
):
    from open_webui.utils.provider_migration import preview_migration

    return await preview_migration(request, user, db)


class LegacyMigration(BaseModel):
    index: int = Field(ge=0)
    fingerprint: str = Field(min_length=64, max_length=64)
    alias_owners: dict[str, int] = Field(default_factory=dict)


@router.post('/legacy-migration/apply')
async def migrate_legacy(
    request: Request, form: LegacyMigration, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)
):
    from open_webui.utils.provider_migration import apply_migration

    return await apply_migration(request, user, db, form.index, form.fingerprint, form.alias_owners)
