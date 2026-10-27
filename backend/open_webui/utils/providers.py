"""Provider discovery, authorization and OpenAI-compatible request forwarding."""

import asyncio
import base64
import hashlib
import json
import logging
import platform
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from open_webui.env import VERSION, WEBUI_SECRET_KEY
from open_webui.internal.db import get_async_db_context
from open_webui.models.access_grants import AccessGrants
from open_webui.models.models import Model
from open_webui.models.provider_connections import (
    ProviderCatalogSnapshot,
    ProviderConnection,
    ProviderModel,
    new_provider_connection,
)
from open_webui.utils.apps import validate_app_url
from open_webui.utils.provider_capabilities import (
    normalize_model,
    normalize_whitelist_model,
    provider_family,
)
from open_webui.utils.provider_errors import (
    AUTHENTICATION,
    PROVIDER_NETWORK,
    RATE_LIMIT,
    UPSTREAM,
    ProviderRequestError,
    error_from_exception,
)
from open_webui.utils.provider_http import (
    ProviderAddressBlocked,
    ProviderTCPConnector,
    allowed_ip_networks,
    provider_request_json,
)
from open_webui.utils.provider_registry import default_base_urls, public_templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

PROVIDER_CATALOG = public_templates()
_DEFAULTS = default_base_urls()
_BAILIAN_REGIONS = {
    'cn-beijing': 'dashscope.aliyuncs.com',
    'ap-southeast-1': 'dashscope-intl.aliyuncs.com',
    'cn-hongkong': 'cn-hongkong.dashscope.aliyuncs.com',
    'eu-central-1': None,
    'us-east-1': None,
    'ap-northeast-1': None,
}
_BAILIAN_WORKSPACE_LABEL = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')


@asynccontextmanager
async def _provider_db_scope(db: AsyncSession | None):
    if db is not None:
        yield db
    else:
        async with get_async_db_context() as session:
            yield session


@dataclass
class ResolvedProviderModel:
    connection: ProviderConnection
    upstream_model_id: str
    user_id: str
    model_metadata: dict


def _cipher() -> Fernet:
    if not WEBUI_SECRET_KEY:
        raise RuntimeError('A stable WEBUI_SECRET_KEY is required for provider credentials')
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b'open-webui-provider-credentials-v1').derive(
        WEBUI_SECRET_KEY.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_credential(value: str | None) -> str | None:
    return _cipher().encrypt(value.encode()).decode() if value else None


def decrypt_credential(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except Exception:
        log.error('Unable to decrypt provider credential')
        raise HTTPException(500, 'Provider credential could not be read') from None


REGISTRY_VERSION = 2


def registry_migrated(connection: ProviderConnection) -> bool:
    """Whether a connection already moved onto the curated whitelist tables."""
    return (connection.config or {}).get('_registry_version') == REGISTRY_VERSION


def legacy_snapshot(connection: ProviderConnection, user_id: str | None = None) -> dict:
    """Read the per-user catalog snapshot from the legacy config payload."""
    entry = (connection.config or {}).get('_models_by_user', {}).get(user_id or connection.owner_id, {})
    return entry if isinstance(entry, dict) else {}


async def read_snapshot(connection: ProviderConnection, user_id: str, db: AsyncSession | None) -> dict:
    """Read the candidate pool for a user from whichever store is in use."""
    if registry_migrated(connection):
        return await catalog_snapshot(connection, user_id, db)
    return legacy_snapshot(connection, user_id)


async def write_snapshot(
    connection: ProviderConnection, user_id: str, entry: dict, db: AsyncSession | None
) -> None:
    """Persist the candidate pool for a user in whichever store is in use."""
    if registry_migrated(connection):
        await store_catalog_snapshot(connection, user_id, entry, db)
        return
    config = dict(connection.config or {})
    cache = dict(config.get('_models_by_user') or {})
    cache[user_id] = entry
    connection.config = {**config, '_models_by_user': cache}


async def catalog_snapshot(connection: ProviderConnection, user_id: str | None, db: AsyncSession | None) -> dict:
    """Read the candidate pool for a user, preferring the new snapshot table."""
    if isinstance(db, AsyncSession):
        row = (
            await db.scalars(
                select(ProviderCatalogSnapshot).where(
                    ProviderCatalogSnapshot.connection_id == connection.id,
                    ProviderCatalogSnapshot.user_id == (user_id or connection.owner_id),
                )
            )
        ).first()
        if row is not None:
            return {
                'fingerprint': row.fingerprint or '',
                'models': row.models if isinstance(row.models, list) else [],
                'error': row.error,
                'generation': row.generation or '',
                'updated_at': row.updated_at or 0,
            }
    return legacy_snapshot(connection, user_id)


async def store_catalog_snapshot(
    connection: ProviderConnection, user_id: str, entry: dict, db: AsyncSession | None
) -> None:
    """Persist the candidate pool for a user."""
    if not isinstance(db, AsyncSession):
        return
    if not registry_migrated(connection):
        return
    existing = (
        await db.scalars(
            select(ProviderCatalogSnapshot).where(
                ProviderCatalogSnapshot.connection_id == connection.id,
                ProviderCatalogSnapshot.user_id == user_id,
            )
        )
    ).first()
    payload = {
        'fingerprint': entry.get('fingerprint') or '',
        'models': entry.get('models') if isinstance(entry.get('models'), list) else [],
        'error': entry.get('error'),
        'generation': entry.get('generation') or '',
        'updated_at': entry.get('updated_at') or 0,
    }
    if existing is None:
        db.add(
            ProviderCatalogSnapshot(
                id=str(uuid.uuid4()),
                connection_id=connection.id,
                user_id=user_id,
                fingerprint=payload['fingerprint'],
                models=payload['models'],
                error=payload['error'],
                generation=payload['generation'],
                updated_at=payload['updated_at'],
            )
        )
        return
    existing.fingerprint = payload['fingerprint']
    existing.models = payload['models']
    existing.error = payload['error']
    existing.generation = payload['generation']
    existing.updated_at = payload['updated_at']


async def connection_models(connection: ProviderConnection, db: AsyncSession | None = None) -> list[dict]:
    """Return the curated whitelist of a connection.

    A migrated connection is authoritative in `provider_model`: the catalog
    refresh never adds rows on its own. A connection that has not been moved
    yet keeps using the legacy payload, so a failed migration never hides
    models from the user.
    """
    if isinstance(db, AsyncSession) and registry_migrated(connection):
        rows = (
            await db.scalars(
                select(ProviderModel)
                .where(ProviderModel.connection_id == connection.id)
                .order_by(ProviderModel.sort_order, ProviderModel.created_at)
            )
        ).all()
        return [normalize_whitelist_model(connection, _whitelist_row(row)) for row in rows]
    return [normalize_model(connection, model) for model in connection.models or []]


def _whitelist_row(row: ProviderModel) -> dict:
    return {
        # The stable id is what the chat selector and the saved switch use.
        'id': stable_model_id(row.connection_id, row.upstream_id),
        'upstream_id': row.upstream_id,
        'display_name': row.display_name,
        'uses': row.uses if isinstance(row.uses, dict) else {},
        'enabled': bool(row.enabled),
        'source': row.source,
        'verification': row.verification if isinstance(row.verification, dict) else {},
        'sort_order': row.sort_order,
        'capabilities': row.capabilities if isinstance(row.capabilities, list) else [],
        'features': row.features if isinstance(row.features, list) else [],
        'model_info': row.model_info if isinstance(row.model_info, dict) else {},
    }


async def seed_connection_models(
    connection: ProviderConnection,
    upstream_ids: list,
    db: AsyncSession,
    *,
    display_names: dict | None = None,
    uses: dict | None = None,
    source: str = 'provider',
    user_id: str | None = None,
) -> list[str]:
    """Create whitelist rows for the given upstream model ids.

    Only the models the user picked end up in the whitelist; a catalog refresh
    never calls this on its own. The purposes are taken from the candidate
    pool unless the caller states them.
    """
    now = int(time.time())
    candidates = {}
    snapshot = await read_snapshot(connection, user_id or connection.owner_id, db)
    for candidate in snapshot.get('models') or []:
        if isinstance(candidate, dict) and candidate.get('upstream_id'):
            candidates[candidate['upstream_id']] = candidate
    added = []
    existing = {
        row.upstream_id: row
        for row in (
            await db.scalars(select(ProviderModel).where(ProviderModel.connection_id == connection.id))
        ).all()
    }
    order = len(existing)
    for upstream_id in upstream_ids:
        if not isinstance(upstream_id, str) or not upstream_id:
            continue
        row = existing.get(upstream_id)
        if row is not None:
            added.append(upstream_id)
            continue
        candidate = candidates.get(upstream_id) or {}
        resolved_uses = uses if uses is not None else (candidate.get('uses') or {})
        if not resolved_uses:
            resolved_uses = {'chat': 'openai-chat'}
        db.add(
            ProviderModel(
                id=str(uuid.uuid4()),
                connection_id=connection.id,
                upstream_id=upstream_id,
                display_name=(display_names or {}).get(upstream_id)
                or candidate.get('name')
                or upstream_id,
                uses=dict(resolved_uses),
                capabilities=[],
                features=[],
                model_info={},
                enabled=True,
                source=source,
                verification={},
                sort_order=order,
                created_at=now,
                updated_at=now,
            )
        )
        order += 1
        added.append(upstream_id)
    return added


async def set_connection_model_enabled(
    connection: ProviderConnection, upstream_id: str, enabled: bool, db: AsyncSession
) -> bool:
    """Toggle one curated model. Returns True when the whitelist table was used."""
    if not registry_migrated(connection) or not isinstance(db, AsyncSession):
        return False
    row = (
        await db.scalars(
            select(ProviderModel).where(
                ProviderModel.connection_id == connection.id,
                ProviderModel.upstream_id == upstream_id,
            )
        )
    ).first()
    if row is None:
        return False
    row.enabled = bool(enabled)
    row.updated_at = int(time.time())
    return True


async def set_connection_model_verification(
    connection: ProviderConnection, upstream_id: str, purpose: str, result: dict, db: AsyncSession
) -> bool:
    """Store the outcome of one model test on the whitelist row."""
    if not registry_migrated(connection) or not isinstance(db, AsyncSession):
        return False
    row = (
        await db.scalars(
            select(ProviderModel).where(
                ProviderModel.connection_id == connection.id,
                ProviderModel.upstream_id == upstream_id,
            )
        )
    ).first()
    if row is None:
        return False
    verification = dict(row.verification) if isinstance(row.verification, dict) else {}
    verification[purpose] = result
    row.verification = verification
    row.updated_at = int(time.time())
    return True


async def set_connection_model_uses(
    connection: ProviderConnection, upstream_id: str, uses: dict, db: AsyncSession
) -> bool:
    """Replace the purposes of one curated model."""
    if not registry_migrated(connection) or not isinstance(db, AsyncSession):
        return False
    row = (
        await db.scalars(
            select(ProviderModel).where(
                ProviderModel.connection_id == connection.id,
                ProviderModel.upstream_id == upstream_id,
            )
        )
    ).first()
    if row is None:
        return False
    row.uses = dict(uses)
    row.updated_at = int(time.time())
    return True


async def public_connection(connection: ProviderConnection, db: AsyncSession | None = None) -> dict:
    config = dict(connection.config or {})
    config = {key: value for key, value in config.items() if not key.startswith('_')}
    for key in ('host_id', 'client_id', 'subject', 'device_id'):
        config.pop(key, None)
    snapshot = await catalog_snapshot(connection, connection.owner_id, db)
    return {
        'id': connection.id,
        'provider_id': connection.provider_id,
        'provider_template': connection.provider_template or connection.provider_id,
        'protocol': connection.protocol or 'openai-compatible',
        'is_enabled': connection.is_enabled is not False,
        'name': connection.name,
        'auth_type': connection.auth_type,
        'visibility': connection.visibility,
        'owner_id': connection.owner_id,
        'config': config,
        'status': connection.status,
        'models': await connection_models(connection, db),
        'catalog': catalog_status(connection, snapshot=snapshot),
        'configured': bool(connection.credential)
        or (connection.auth_type == 'oauth' and connection.status != 'disconnected'),
        'created_at': connection.created_at,
        'updated_at': connection.updated_at,
    }


def provider_base_url(connection: ProviderConnection) -> str:
    config = connection.config or {}
    value = config.get('base_url') or _DEFAULTS.get(connection.provider_id, '')
    if connection.provider_id == 'bailian' and not config.get('base_url'):
        value = _bailian_base_url(connection)
    if not value and connection.provider_id == 'chatgpt':
        return ''
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ('http', 'https')
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        validate_app_url(value)
    except Exception:
        raise HTTPException(400, 'Provider URL must be a valid HTTPS endpoint') from None
    return value.rstrip('/')


async def can_access_connection(connection: ProviderConnection, user, db: AsyncSession, permission='read') -> bool:
    if connection.owner_id == user.id:
        return True
    if connection.visibility != 'shared':
        return False
    if getattr(user, 'role', '') == 'admin':
        return True
    return await AccessGrants.has_anyone_access(
        'provider_connection', connection.id, permission, db=db
    ) or await AccessGrants.has_access(user.id, 'provider_connection', connection.id, permission, db=db)


async def require_connection_access(connection: ProviderConnection | None, user, db: AsyncSession, permission='read'):
    if not connection or not await can_access_connection(connection, user, db, permission):
        raise HTTPException(404, 'Provider connection not found')
    return connection


def stable_model_id(connection_id: str, upstream_model_id: str) -> str:
    return f'provider:{connection_id}:{quote(upstream_model_id, safe="")}'


def parse_model_id(model_id: str) -> tuple[str, str]:
    try:
        prefix, connection_id, encoded = model_id.split(':', 2)
        upstream_id = unquote(encoded)
        if prefix != 'provider' or not connection_id or not upstream_id:
            raise ValueError()
        return connection_id, upstream_id
    except ValueError:
        raise HTTPException(404, 'Provider model not found') from None


def _bailian_host(connection: ProviderConnection) -> str:
    config = connection.config or {}
    region = config.get('region') or 'cn-beijing'
    if region not in _BAILIAN_REGIONS:
        raise HTTPException(400, 'Unsupported Bailian region')
    workspace = config.get('workspace_id')
    if workspace:
        if not isinstance(workspace, str) or not _BAILIAN_WORKSPACE_LABEL.fullmatch(workspace):
            raise HTTPException(400, 'Invalid Bailian workspace ID')
        return f'{workspace}.{region}.maas.aliyuncs.com'
    host = _BAILIAN_REGIONS[region]
    if not host:
        raise HTTPException(400, 'Bailian requires a workspace ID for this region')
    return host


def _bailian_base_url(connection: ProviderConnection) -> str:
    # Workspace endpoints are region-specific, including Beijing/Singapore/HK.
    return f'https://{_bailian_host(connection)}/compatible-mode/v1'


def _bailian_models_url(connection: ProviderConnection) -> str:
    config = connection.config or {}
    explicit_base = config.get('base_url')
    if explicit_base:
        # Use the explicitly selected endpoint's own origin. Never infer a
        # DashScope workspace from an arbitrary hostname suffix.
        base = provider_base_url(connection)
        parsed = urlsplit(base)
        return f'{parsed.scheme}://{parsed.netloc}/api/v1/models'
    return f'https://{_bailian_host(connection)}/api/v1/models'


def kimi_device_headers(device_id: str | None = None) -> dict[str, str]:
    """Return the stable, honest client identity expected by Kimi Code.

    The device id is derived from the deployment secret so it survives worker
    restarts without storing another credential. The platform is explicitly
    Open WebUI; the adapter does not impersonate the official Kimi CLI.
    """

    if not device_id:
        device_id = hashlib.sha256(f'open-webui-kimi-device:{WEBUI_SECRET_KEY}'.encode()).hexdigest()[:32]

    def ascii_header(value: str, fallback: str = 'unknown') -> str:
        cleaned = value.encode('ascii', errors='ignore').decode('ascii').strip()
        return cleaned or fallback

    system = platform.system() or 'Unknown'
    machine = platform.machine() or 'unknown'
    release = platform.release() or 'unknown'
    model = ' '.join(part for part in (system, release, machine) if part).strip()
    return {
        'X-Msh-Platform': 'open_webui',
        'X-Msh-Version': ascii_header(VERSION, 'unknown'),
        'X-Msh-Device-Name': ascii_header(socket.gethostname()),
        'X-Msh-Device-Model': ascii_header(model),
        'X-Msh-Os-Version': ascii_header(platform.version()),
        'X-Msh-Device-Id': ascii_header(device_id, 'unknown'),
    }


def provider_request_headers(connection: ProviderConnection, token: str) -> dict[str, str]:
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    if connection.provider_id == 'kimi':
        headers.update(kimi_device_headers((connection.config or {}).get('device_id')))
    return headers


async def _authorization(connection: ProviderConnection, user) -> str:
    if connection.auth_type == 'oauth':
        if connection.status == 'disconnected' or not connection.credential:
            raise HTTPException(401, 'Reconnect this provider account')
        try:
            if connection.provider_id == 'kimi':
                from open_webui.utils.kimi_auth import get_provider_access_token
            else:
                from open_webui.utils.chatgpt_auth import get_provider_access_token

            return await get_provider_access_token(connection, user)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(401, 'Reconnect this provider account') from None
    key = decrypt_credential(connection.credential)
    if not key:
        raise HTTPException(400, 'Provider connection has no API key')
    return key


async def read_bounded_response(response, limit: int = 2_000_000) -> bytes:
    """Read through EOF, including chunked/SSE bodies, with a hard memory limit."""
    chunks = []
    size = 0
    async for chunk in response.content.iter_chunked(16384):
        size += len(chunk)
        if size > limit:
            raise HTTPException(502, 'Provider response is too large')
        chunks.append(chunk)
    return b''.join(chunks)


def provider_http_error(status: int) -> HTTPException:
    """Build an HTTP error that keeps the upstream status and its category.

    The category is attached so that `catalog_error` can tell a genuine
    upstream fault apart from a local gateway failure. Without it every
    upstream status collapsed into a generic "network" error.
    """
    if status in (401, 403):
        error = HTTPException(status, 'Provider rejected the configured credentials or permissions')
        category = AUTHENTICATION
    elif status == 429:
        error = HTTPException(429, 'Provider usage limit reached; wait or check your plan quota')
        category = RATE_LIMIT
    elif status == 402:
        error = HTTPException(402, 'Provider quota or balance is insufficient')
        category = RATE_LIMIT
    else:
        error = HTTPException(502, f'Provider request failed with HTTP {status}')
        category = UPSTREAM
    error.provider_status = status
    error.provider_category = category
    return error


def _provider_http_error(exc) -> HTTPException:
    """Translate a transport failure into an HTTP error with its category."""
    status = exc.status if exc.status in (401, 403, 402, 429) else 502
    error = HTTPException(status, exc.detail)
    error.provider_status = exc.upstream_status
    error.provider_category = exc.category
    error.provider_hint = exc.hint
    return error


async def _request_json(connection: ProviderConnection, user, url: str, *, method='GET', body=None, params=None):
    token = await _authorization(connection, user)
    headers = provider_request_headers(connection, token)
    try:
        return await provider_request_json(url, headers=headers, method=method, body=body, params=params)
    except ProviderRequestError as exc:
        raise _provider_http_error(exc) from None


async def discover_connection_models(connection: ProviderConnection, user) -> list[dict]:
    discovery_error = None
    if connection.provider_id == 'chatgpt':
        try:
            from open_webui.utils.chatgpt_auth import discover_chatgpt_models

            records = await discover_chatgpt_models(connection, user)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, 'Could not discover ChatGPT models') from None
    elif provider_family(connection) == 'siliconflow':
        base = provider_base_url(connection)
        payload = await _request_json(connection, user, f'{base}/models')
        records_by_id = {
            r['id']: dict(r, uses={}) for r in payload.get('data', []) if isinstance(r, dict) and r.get('id')
        }
        for sub_type, use, adapter in (
            ('chat', 'chat', 'openai-chat'),
            ('speech-to-text', 'transcription', 'openai-transcription'),
            ('text-to-image', 'image', 'siliconflow-image'),
        ):
            typed = await _request_json(connection, user, f'{base}/models', params={'sub_type': sub_type})
            for record in typed.get('data', []):
                if isinstance(record, dict) and record.get('id'):
                    item = records_by_id.setdefault(record['id'], dict(record, uses={}))
                    item['uses'][use] = adapter
        records = list(records_by_id.values())
    elif connection.provider_id == 'bailian':
        models_url = _bailian_models_url(connection)
        records = []
        page = 1
        total = None
        try:
            while page <= 10:
                payload = await _request_json(
                    connection, user, models_url, params={'capabilities': 'TG', 'page_no': page, 'page_size': 100}
                )
                output = payload.get('output', {}) if isinstance(payload, dict) else {}
                current = output.get('models', []) if isinstance(output, dict) else []
                records.extend(current if isinstance(current, list) else [])
                total = output.get('total') if isinstance(output, dict) else None
                if not current or (isinstance(total, int) and len(records) >= total):
                    break
                page += 1
        except HTTPException as exc:
            if (
                exc.status_code in (401, 403, 429)
                or exc.detail == 'Provider returned an invalid model catalog'
                or not (connection.config or {}).get('model_ids')
            ):
                raise
            discovery_error = catalog_error(exc)
            records = []
    else:
        base = provider_base_url(connection)
        try:
            payload = await _request_json(connection, user, f'{base}/models')
            records = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise HTTPException(502, 'Provider returned an invalid model catalog')
        except HTTPException as exc:
            if (
                exc.status_code in (401, 403, 429)
                or exc.detail == 'Provider returned an invalid model catalog'
                or not (connection.config or {}).get('model_ids')
            ):
                raise
            discovery_error = catalog_error(exc)
            records = []
    models = []
    selected = (connection.config or {}).get('model_ids') or []
    for record in records or []:
        upstream_id = (record.get('model') or record.get('id')) if isinstance(record, dict) else None
        if isinstance(upstream_id, str) and upstream_id:
            capabilities = record.get('capabilities') or []
            features = record.get('features') or []
            models.append(
                {
                    'id': stable_model_id(connection.id, upstream_id),
                    'name': record.get('name') or upstream_id,
                    'upstream_id': upstream_id,
                    'provider_id': connection.provider_id,
                    'connection_id': connection.id,
                    'capabilities': capabilities,
                    'uses': record.get('uses', {}),
                    'features': features,
                    'model_info': record.get('model_info') if isinstance(record.get('model_info'), dict) else {},
                }
            )
    # Explicit IDs allow providers without a compatible /models endpoint and are
    # also useful when access is intentionally restricted to a selected subset.
    for upstream_id in selected:
        if isinstance(upstream_id, str) and upstream_id and not any(m['upstream_id'] == upstream_id for m in models):
            models.append(
                {
                    'id': stable_model_id(connection.id, upstream_id),
                    'name': upstream_id,
                    'upstream_id': upstream_id,
                    'provider_id': connection.provider_id,
                    'connection_id': connection.id,
                }
            )
    return [normalize_model(connection, {**model, 'catalog_error': discovery_error}) for model in models]


async def verify_provider_connection(connection: ProviderConnection, user) -> bool:
    """Probe a provider catalog endpoint without treating manual IDs as verified."""
    if connection.provider_id == 'chatgpt':
        await discover_connection_models(connection, user)
        return True
    if connection.provider_id == 'bailian':
        # Bailian's OpenAI-compatible endpoint does not expose GET /models;
        # use its paginated native text-model catalog instead.
        try:
            await _request_json(
                connection,
                user,
                _bailian_models_url(connection),
                params={'capabilities': 'TG', 'page_no': 1, 'page_size': 1},
            )
            return True
        except HTTPException:
            if (connection.config or {}).get('model_ids'):
                return False
            raise
    try:
        await _request_json(connection, user, f'{provider_base_url(connection)}/models')
        return True
    except HTTPException:
        if (connection.config or {}).get('model_ids'):
            return False
        raise


async def initialize_model_catalog(connection):
    if registry_migrated(connection):
        return
    if (connection.config or {}).get('_catalog_version'):
        return
    from open_webui.models.config import Config

    config = dict(connection.config or {})
    overrides = dict(config.get('_model_overrides', {}))
    for purpose, prefix in (('transcription', 'audio.stt'), ('speech', 'audio.tts'), ('image', 'image_generation')):
        if (
            await Config.get(f'{prefix}.engine') == 'provider'
            and await Config.get(f'{prefix}.provider_connection_id') == connection.id
        ):
            model = await Config.get(f'{prefix}.model')
            if model and not model.startswith('provider:'):
                adapter = {'transcription': 'openai-transcription', 'speech': 'openai-speech', 'image': 'openai-image'}[
                    purpose
                ]
                overrides[model] = {**overrides.get(model, {}), purpose: adapter}
    connection.config = {
        **config,
        '_catalog_version': 1,
        '_model_overrides': overrides,
        '_legacy_chat_ids': [m['upstream_id'] for m in connection.models or [] if m.get('upstream_id')],
    }


CATALOG_TTL = 15 * 60
_catalog_refresh_tasks = {}
_catalog_locks = {}


def catalog_fingerprint(connection):
    return hashlib.sha256(
        json.dumps(
            {
                'credential': connection.credential,
                'config': {k: v for k, v in (connection.config or {}).items() if not k.startswith('_')},
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def catalog_status(connection, user_id=None, snapshot=None):
    entry = snapshot if isinstance(snapshot, dict) else legacy_snapshot(connection, user_id)
    if not isinstance(entry, dict):
        entry = {}
    valid = entry.get('fingerprint') == catalog_fingerprint(connection)
    return {
        'updated_at': entry.get('updated_at') if valid and not entry.get('imported') else None,
        'stale': not valid or bool(entry.get('error')) or time.time() - entry.get('updated_at', 0) >= CATALOG_TTL,
        'error': entry.get('error') if valid else None,
        'request_path': 'server',
    }


def catalog_error(exc):
    """Classify a provider failure into the five documented categories.

    Never persists upstream bodies, URLs with credentials, or exception
    strings; only the category, the statuses and a display-ready hint.
    """
    return error_from_exception(exc)


@asynccontextmanager
async def catalog_lease(connection_id, user_id, db):
    if not isinstance(db, AsyncSession):
        yield True
        return
    from open_webui.models.provider_connections import ProviderCatalogLease
    from sqlalchemy import delete, update
    from sqlalchemy.exc import IntegrityError

    key, token = f'{connection_id}:{user_id}', str(uuid.uuid4())
    owned = False
    async with get_async_db_context() as session:
        result = await session.execute(
            update(ProviderCatalogLease)
            .where(ProviderCatalogLease.id == key, ProviderCatalogLease.expires_at < int(time.time()))
            .values(token=token, expires_at=int(time.time()) + 300)
        )
        if result.rowcount:
            owned = True
            await session.commit()
        else:
            try:
                session.add(ProviderCatalogLease(id=key, token=token, expires_at=int(time.time()) + 300))
                await session.commit()
                owned = True
            except IntegrityError:
                await session.rollback()
    try:
        yield owned
    finally:
        if owned:
            async with get_async_db_context() as session:
                await session.execute(
                    delete(ProviderCatalogLease).where(
                        ProviderCatalogLease.id == key, ProviderCatalogLease.token == token
                    )
                )
                await session.commit()


async def refresh_connection_catalog(connection, user, db):
    entry = await read_snapshot(connection, user.id, db)
    generation = entry.get('generation') if isinstance(entry, dict) else None
    lock = _catalog_locks.setdefault(connection.id, asyncio.Lock())
    async with lock, catalog_lease(connection.id, 'catalog', db) as acquired:
        if isinstance(db, AsyncSession):
            await db.refresh(connection)
        entry = await read_snapshot(connection, user.id, db)
        if not acquired or (
            isinstance(entry, dict)
            and entry.get('generation') != generation
            and entry.get('fingerprint') == catalog_fingerprint(connection)
        ):
            if (
                isinstance(entry, dict)
                and entry.get('fingerprint') == catalog_fingerprint(connection)
                and isinstance(entry.get('models'), list)
            ):
                return [normalize_model(connection, m) for m in entry['models']]
            raise HTTPException(409, 'Catalog refresh already running; retry shortly')
        return await _refresh_connection_catalog(connection, user, db)


async def _refresh_connection_catalog(connection, user, db):
    await initialize_model_catalog(connection)
    stamp = catalog_fingerprint(connection)
    previous = await read_snapshot(connection, user.id, db)
    previous = previous if isinstance(previous, dict) and previous.get('fingerprint') == stamp else {}
    try:
        models = await discover_connection_models(connection, user)
    except Exception as exc:
        if isinstance(db, AsyncSession):
            await db.refresh(connection)
            if catalog_fingerprint(connection) != stamp:
                raise HTTPException(409, 'Connection changed during refresh') from None
        await write_snapshot(connection, user.id, {**previous, 'fingerprint': stamp, 'error': catalog_error(exc)}, db)
        await db.commit()
        raise
    if isinstance(db, AsyncSession):
        await db.refresh(connection)
        if (
            catalog_fingerprint(connection) != stamp
            or not connection.is_active
            or not await can_access_connection(connection, user, db)
        ):
            raise HTTPException(409, 'Connection changed during refresh; fetch the catalog again')
    discovery_error = next((m.get('catalog_error') for m in models if m.get('catalog_error')), None)
    if discovery_error and previous.get('models'):
        await write_snapshot(connection, user.id, {**previous, 'error': discovery_error}, db)
        await db.commit()
        return [normalize_model(connection, m) for m in previous['models']]
    if registry_migrated(connection):
        # The catalog is a candidate pool only. The curated whitelist is never
        # changed by a refresh, so newly published models stay out until the
        # user picks them.
        await write_snapshot(
            connection,
            user.id,
            {
                'fingerprint': stamp,
                'models': models,
                'updated_at': 0 if discovery_error else int(time.time()),
                'generation': str(uuid.uuid4()),
                'error': discovery_error,
            },
            db,
        )
        connection.status = 'configured' if discovery_error else 'connected'
        await db.commit()
        return models
    config = dict(connection.config or {})
    enabled = dict(config.get('_model_enabled', {}))
    old_ids = {m['upstream_id'] for m in connection.models or []}
    selected = config.get('model_ids', [])
    for model in models:
        upstream = model['upstream_id']
        enabled.setdefault(upstream, upstream in selected if selected else upstream in old_ids)
    # Preserve manually added models even when upstream no longer lists them.
    found = {m['upstream_id'] for m in models}
    models.extend(
        m
        for m in connection.models or []
        if m['upstream_id'] not in found and m['upstream_id'] in config.get('_model_overrides', {})
    )
    await write_snapshot(
        connection,
        user.id,
        {
            'fingerprint': stamp,
            'models': models,
            'updated_at': 0 if discovery_error else int(time.time()),
            'generation': str(uuid.uuid4()),
            'error': discovery_error,
        },
        db,
    )
    # Keep the snapshot that was just written; only the enabled map changes.
    connection.config = {**(connection.config or {}), '_model_enabled': enabled}
    connection.models = models
    connection.status = 'configured' if discovery_error else 'connected'
    await db.commit()
    return [normalize_model(connection, model) for model in models]


async def _refresh_catalog_background(connection_id, user, expected):
    try:
        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, connection_id)
            if (
                connection
                and connection.is_active
                and catalog_fingerprint(connection) == expected
                and await can_access_connection(connection, user, db)
            ):
                await refresh_connection_catalog(connection, user, db)
    except Exception:
        log.info('Provider catalog background refresh failed for %s', connection_id)


def _schedule_catalog_refresh(connection: ProviderConnection, user, stamp: str) -> None:
    """Refresh the candidate pool in the background, coalesced per connection."""
    key = (connection.id, user.id)
    if key not in _catalog_refresh_tasks or _catalog_refresh_tasks[key].done():
        task = asyncio.create_task(_refresh_catalog_background(connection.id, user, stamp))
        _catalog_refresh_tasks[key] = task
        task.add_done_callback(
            lambda done: (
                _catalog_refresh_tasks.pop(key, None) if _catalog_refresh_tasks.get(key) is done else None
            )
        )


async def visible_connection_models(connection: ProviderConnection, user, db: AsyncSession) -> list[dict]:
    if connection.status == 'disconnected' or not connection.credential:
        return []
    await initialize_model_catalog(connection)
    if registry_migrated(connection):
        # The curated whitelist decides what is offered. The catalog is only a
        # candidate pool and is refreshed in the background.
        entry = await read_snapshot(connection, user.id, db)
        stamp = catalog_fingerprint(connection)
        fresh = isinstance(entry, dict) and entry.get('fingerprint') == stamp and isinstance(entry.get('models'), list)
        age = time.time() - max(entry.get('updated_at', 0), (entry.get('error') or {}).get('at', 0))
        if not fresh or age >= CATALOG_TTL:
            _schedule_catalog_refresh(connection, user, stamp)
        return await connection_models(connection, db)
    entry = (connection.config or {}).get('_models_by_user', {}).get(user.id, {})
    stamp = catalog_fingerprint(connection)
    legacy_hash = hashlib.sha256((connection.credential or '').encode()).hexdigest()
    if (
        isinstance(entry, dict)
        and entry.get('credential_hash') == legacy_hash
        and isinstance(entry.get('models'), list)
    ):
        config = dict(connection.config or {})
        enabled = dict(config.get('_model_enabled', {}))
        for model in entry['models']:
            enabled.setdefault(model['upstream_id'], True)
        snapshot = {'fingerprint': stamp, 'models': entry['models'], 'updated_at': int(time.time()), 'imported': True}
        connection.config = {
            **config,
            '_model_enabled': enabled,
            '_models_by_user': {**config.get('_models_by_user', {}), user.id: snapshot},
        }
        if connection.owner_id == user.id:
            connection.models = entry['models']
        await db.commit()
        entry = snapshot
    if isinstance(entry, dict) and entry.get('fingerprint') == stamp and isinstance(entry.get('models'), list):
        if time.time() - max(entry.get('updated_at', 0), (entry.get('error') or {}).get('at', 0)) >= CATALOG_TTL:
            _schedule_catalog_refresh(connection, user, stamp)
        return [normalize_model(connection, model) for model in entry['models']]
    # Upgrade existing owner catalogs without a network dependency. Other users
    # never inherit an owner's per-user discovery snapshot.
    if connection.owner_id == user.id and connection.models and not entry:
        config = dict(connection.config or {})
        config['_models_by_user'] = {
            **config.get('_models_by_user', {}),
            user.id: {'fingerprint': stamp, 'models': connection.models, 'updated_at': 0},
        }
        connection.config = config
        await db.commit()
        return await visible_connection_models(connection, user, db)
    return await refresh_connection_catalog(connection, user, db)


async def disabled_provider_model_ids(db: AsyncSession) -> set[str]:
    # Provider models are request-private and appended after the normal model
    # overlay. Read saved switches here as well, including on every inference.
    return set(await db.scalars(select(Model.id).where(Model.is_active.is_(False), Model.id.startswith('provider:'))))


async def _list_visible_models(user, db: AsyncSession) -> list[dict]:
    connections = (await db.scalars(select(ProviderConnection).where(ProviderConnection.is_active.is_(True)))).all()
    disabled_ids = await disabled_provider_model_ids(db)
    result = []
    for connection in connections:
        if connection.is_enabled is False:
            continue
        if await can_access_connection(connection, user, db):
            try:
                snapshot = await catalog_snapshot(connection, user.id, db)
                result.extend(
                    {
                        **model,
                        'connection_id': connection.id,
                        'object': 'model',
                        'owned_by': connection.provider_id,
                        'connection_type': 'external',
                        'provider_visibility': connection.visibility,
                        'connection_name': connection.name,
                        'catalog': catalog_status(connection, user.id, snapshot),
                        'info': {
                            'meta': {
                                'capabilities': {
                                    'vision': bool(
                                        {'VU', 'Visual Understanding'} & set(model.get('capabilities') or [])
                                    ),
                                    'file_upload': False,
                                    # This gates Open WebUI's search UI/tools,
                                    # not the provider's hosted search feature.
                                    'web_search': True,
                                }
                            }
                        },
                    }
                    for model in await visible_connection_models(connection, user, db)
                    if model.get('enabled', True)
                    and 'chat' in model.get('uses', {})
                    and model['id'] not in disabled_ids
                )
            except HTTPException as exc:
                log.info(
                    'Skipping provider model discovery for connection %s (HTTP %s)', connection.id, exc.status_code
                )
    aliases = []
    for item in result:
        connection = next(c for c in connections if c.id == item['connection_id'])
        if not await legacy_model_access(connection, item['upstream_id'], user, db):
            continue
        mappings = (connection.config or {}).get('_legacy_aliases', {})
        matches = [alias for alias, upstream in mappings.items() if upstream == item['upstream_id']]
        if matches:
            for alias in matches:
                from open_webui.models.models import Models

                saved = await Models.get_model_by_id(alias, db=db)
                if saved and not saved.is_active:
                    continue
                info = saved.model_dump() if saved else item.get('info', {})
                aliases.append({**item, 'id': alias, 'provider_model_id': item['id'], 'info': info})
        else:
            aliases.append(item)
    return aliases


async def list_visible_models(user, db: AsyncSession | None = None) -> list[dict]:
    async with _provider_db_scope(db) as session:
        return await _list_visible_models(user, session)


async def legacy_model_access(connection, upstream_id, user, db):
    aliases = (connection.config or {}).get('_legacy_access_ids', (connection.config or {}).get('_legacy_aliases', {}))
    for alias, upstream in aliases.items():
        if upstream == upstream_id:
            from open_webui.models.models import Models
            from open_webui.utils.models import check_model_access

            saved = await Models.get_model_by_id(alias, db=db)
            if saved and not saved.is_active:
                return False
            if user.role != 'admin':
                if not saved:
                    return False
                try:
                    await check_model_access(user, {'id': alias}, model_info=saved, db=db)
                except Exception:
                    return False
    return True


async def _resolve_model(model_id: str, user, db: AsyncSession, allow_disabled=False) -> ResolvedProviderModel:
    connection_id, upstream_id = parse_model_id(model_id)
    connection = await db.get(ProviderConnection, connection_id)
    await require_connection_access(connection, user, db)
    if (
        not connection.is_active
        or connection.is_enabled is False
        or (not allow_disabled and model_id in await disabled_provider_model_ids(db))
    ):
        raise HTTPException(404, 'Provider model not found')
    error = catalog_status(connection, user.id, await catalog_snapshot(connection, user.id, db)).get('error')
    if error and error['category'] == 'authentication':
        raise HTTPException(401, 'Provider authentication failed; refresh after correcting credentials')
    available = await visible_connection_models(connection, user, db)
    metadata = next((model for model in available if model['upstream_id'] == upstream_id), None)
    if (
        metadata is None
        or (not allow_disabled and not metadata.get('enabled', True))
        or 'chat' not in metadata.get('uses', {})
    ):
        raise HTTPException(404, 'Provider model not found')
    if not await legacy_model_access(connection, upstream_id, user, db):
        raise HTTPException(404, 'Provider model not found')
    metadata = {
        **metadata,
        'object': 'model',
        'owned_by': connection.provider_id,
        'connection_type': 'external',
        'provider_visibility': connection.visibility,
        'connection_name': connection.name,
        'info': {
            'meta': {
                'capabilities': {
                    'vision': bool({'VU', 'Visual Understanding'} & set(metadata.get('capabilities') or [])),
                    'file_upload': False,
                    'web_search': True,
                }
            }
        },
    }
    return ResolvedProviderModel(connection, upstream_id, user.id, metadata)


async def resolve_model(
    model_id: str, user, db: AsyncSession | None = None, *, allow_disabled=False
) -> ResolvedProviderModel:
    async with _provider_db_scope(db) as session:
        return await _resolve_model(model_id, user, session, allow_disabled=allow_disabled)


async def get_connection_credentials(
    connection_id: str, user, db: AsyncSession | None = None
) -> tuple[ProviderConnection, str | None]:
    async with _provider_db_scope(db) as session:
        connection = await session.get(ProviderConnection, connection_id)
        await require_connection_access(connection, user, session)
        return connection, decrypt_credential(connection.credential)


def _openai_payload(payload: dict, upstream_model_id: str) -> dict:
    allowed = {
        'messages',
        'stream',
        'temperature',
        'top_p',
        'n',
        'stop',
        'max_tokens',
        'max_completion_tokens',
        'presence_penalty',
        'frequency_penalty',
        'logit_bias',
        'user',
        'seed',
        'response_format',
        'tools',
        'tool_choice',
        'parallel_tool_calls',
        'stream_options',
        'reasoning_effort',
        'reasoning',
        'thinking',
        'text',
    }
    return {key: value for key, value in payload.items() if key in allowed} | {'model': upstream_model_id}


def _blocked_address_error(exc: ProviderAddressBlocked) -> HTTPException:
    error = HTTPException(502, 'Provider endpoint resolves to a prohibited address')
    error.provider_category = PROVIDER_NETWORK
    error.provider_hint = (
        f'地址 {exc.host} 解析到 {"、".join(exc.addresses[:4])}，落在保留网段（代理 fake-ip 常用该段），'
        '已被服务器安全策略拦截。'
    )
    return error


async def _post_provider_request(connection, target_url: str, body, headers) -> tuple:
    """Open a provider session and post the request, mapping transport failures."""
    allow_private = validate_app_url(target_url)
    session = aiohttp.ClientSession(
        connector=ProviderTCPConnector(allow_private=allow_private, allowed_networks=allowed_ip_networks()),
        timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=300),
        trust_env=False,
    )
    try:
        response = await session.post(target_url, json=body, headers=headers, allow_redirects=False)
    except ProviderAddressBlocked as exc:
        await session.close()
        raise _blocked_address_error(exc) from None
    except (aiohttp.ClientError, TimeoutError):
        await session.close()
        raise HTTPException(502, 'Could not reach the provider') from None
    return response, session


async def provider_chat_completion(
    model_id: str, payload: dict, user, db: AsyncSession | None = None, *, allow_disabled=False
):
    """Authorize and forward a completion, returning JSON or an SSE response."""
    resolved = (
        await resolve_model(model_id, user, db, allow_disabled=True)
        if allow_disabled
        else await resolve_model(model_id, user, db)
    )
    if resolved.connection.provider_id == 'chatgpt':
        from open_webui.utils.chatgpt_auth import chatgpt_chat_completion

        result = await chatgpt_chat_completion(
            resolved.connection, resolved.upstream_model_id, _openai_payload(payload, resolved.upstream_model_id), user
        )
        # Adapter returns an aiohttp response/session pair, matching the API-key path.
        response, session = result
    else:
        connection = resolved.connection
        token = await _authorization(connection, user)
        body = _openai_payload(payload, resolved.upstream_model_id)
        api_type = (
            'responses'
            if resolved.model_metadata.get('uses', {}).get('chat') == 'responses'
            else (connection.config or {}).get('api_type', 'openai')
        )
        if api_type == 'responses':
            from open_webui.routers.openai import convert_to_responses_payload

            body = convert_to_responses_payload(body)
            body['model'] = resolved.upstream_model_id
            target_path = '/responses'
            headers = {**provider_request_headers(connection, token), 'Content-Type': 'application/json'}
        else:
            target_path = '/chat/completions'
            headers = {**provider_request_headers(connection, token), 'Content-Type': 'application/json'}
        target_url = f'{provider_base_url(connection)}{target_path}'
        response, session = await _post_provider_request(connection, target_url, body, headers)
    if response.status < 200 or response.status >= 300:
        response.release()
        await session.close()
        raise provider_http_error(response.status)
    if payload.get('stream'):
        from open_webui.utils.provider_stream import provider_stream

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in ('cache-control', 'x-request-id', 'openai-processing-ms')
        }
        headers.setdefault('Cache-Control', 'no-cache')
        return StreamingResponse(
            provider_stream(
                response,
                session,
                require_completed=(
                    resolved.connection.provider_id == 'chatgpt'
                    or resolved.model_metadata.get('uses', {}).get('chat') == 'responses'
                    or (resolved.connection.config or {}).get('api_type') == 'responses'
                ),
            ),
            status_code=response.status,
            media_type='text/event-stream',
            headers=headers,
        )
    try:
        raw = await read_bounded_response(response)
        if len(raw) > 2_000_000:
            raise HTTPException(502, 'Provider response is too large')
        if resolved.connection.provider_id == 'chatgpt':
            completed = None
            failed = False
            for line in raw.decode('utf-8', errors='replace').splitlines():
                if not line.startswith('data:'):
                    continue
                value = line[5:].strip()
                if value == '[DONE]':
                    continue
                try:
                    event = json.loads(value)
                except ValueError:
                    continue
                if event.get('type') == 'response.completed':
                    completed = event.get('response')
                elif event.get('type') == 'response.failed':
                    failed = True
            if completed is None:
                if failed:
                    raise HTTPException(502, 'ChatGPT request failed')
                raise HTTPException(502, 'ChatGPT returned an incomplete response')
            from open_webui.routers.openai import convert_responses_result

            result = convert_responses_result(completed)
            result['model'] = model_id
            return result
        api_type = (
            'responses'
            if resolved.model_metadata.get('uses', {}).get('chat') == 'responses'
            else (resolved.connection.config or {}).get('api_type', 'openai')
        )
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(502, 'Provider returned an invalid response') from None
        if isinstance(result, dict):
            if api_type == 'responses':
                from open_webui.routers.openai import convert_responses_result

                result = convert_responses_result(result)
            result['model'] = model_id
        return result
    finally:
        response.release()
        await session.close()


async def get_oauth_credentials(
    connection: ProviderConnection,
    user,
    db: AsyncSession | None = None,
    *,
    provider_id: str = 'chatgpt',
) -> dict:
    """Read the encrypted OAuth credential document for its owner."""
    async with _provider_db_scope(db) as session:
        current = await session.get(ProviderConnection, connection.id)
        if (
            not current
            or current.provider_id != provider_id
            or current.auth_type != 'oauth'
            or current.owner_id != user.id
        ):
            raise HTTPException(404, 'Provider connection not found')
        raw = decrypt_credential(current.credential)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            raise HTTPException(500, 'Provider credentials could not be read') from None
        return value if isinstance(value, dict) else {}


async def replace_oauth_credentials(
    connection: ProviderConnection,
    user,
    credentials: dict,
    db: AsyncSession | None = None,
    *,
    provider_id: str = 'chatgpt',
) -> ProviderConnection:
    """Atomically replace the encrypted OAuth credential document."""
    async with _provider_db_scope(db) as session:
        current = await session.get(ProviderConnection, connection.id, with_for_update=True)
        if (
            not current
            or current.provider_id != provider_id
            or current.auth_type != 'oauth'
            or current.owner_id != user.id
        ):
            raise HTTPException(404, 'Provider connection not found')
        current.credential = encrypt_credential(json.dumps(credentials, separators=(',', ':')))
        current.status = 'connected'
        current.updated_at += 1
        if db is None:
            await session.commit()
        return current


async def create_provider_connection(
    user,
    config: dict | None = None,
    db: AsyncSession | None = None,
    credentials: dict | None = None,
    *,
    provider_id: str = 'chatgpt',
    name: str | None = None,
):
    async with _provider_db_scope(db) as session:
        connection = new_provider_connection(
            provider_id=provider_id,
            name=name or ('Kimi Code' if provider_id == 'kimi' else 'ChatGPT'),
            auth_type='oauth',
            visibility='private',
            owner_id=user.id,
            config=config or {},
            credential=encrypt_credential(json.dumps(credentials or {}, separators=(',', ':'))),
            status='connected',
            models=[],
            is_active=True,
        )
        session.add(connection)
        await session.flush()
        if db is None:
            await session.commit()
        return connection


async def get_provider_connection(
    connection_id: str,
    user,
    db: AsyncSession | None = None,
    *,
    provider_id: str = 'chatgpt',
):
    async with _provider_db_scope(db) as session:
        connection = await session.get(ProviderConnection, connection_id)
        if not connection or connection.provider_id != provider_id or connection.owner_id != user.id:
            raise HTTPException(404, 'Provider connection not found')
        return connection


async def list_provider_connections(
    user,
    db: AsyncSession | None = None,
    *,
    provider_id: str = 'chatgpt',
):
    async with _provider_db_scope(db) as session:
        return list(
            (
                await session.scalars(
                    select(ProviderConnection).where(
                        ProviderConnection.owner_id == user.id,
                        ProviderConnection.provider_id == provider_id,
                        ProviderConnection.auth_type == 'oauth',
                    )
                )
            ).all()
        )


async def delete_provider_connection(
    connection_id: str,
    user,
    db: AsyncSession | None = None,
    *,
    provider_id: str = 'chatgpt',
):
    async with _provider_db_scope(db) as session:
        connection = await session.get(ProviderConnection, connection_id, with_for_update=True)
        if not connection or connection.provider_id != provider_id or connection.owner_id != user.id:
            raise HTTPException(404, 'Provider connection not found')
        connection.credential = None
        connection.is_active = True
        connection.models = []
        connection.config = {key: value for key, value in (connection.config or {}).items() if key != '_models_by_user'}
        connection.status = 'disconnected'
        await session.commit()
        return connection


async def get_media_api_connection(
    connection_id: str,
    user,
    db: AsyncSession | None = None,
) -> tuple[ProviderConnection, str]:
    """Resolve a server-saved API-key connection for image or audio media."""
    if user is None:
        raise HTTPException(401, 'Sign in to use this provider connection')
    if not connection_id:
        raise HTTPException(400, 'Select a provider connection for this media feature')
    async with _provider_db_scope(db) as session:
        connection = await session.get(ProviderConnection, connection_id)
        await require_connection_access(connection, user, session)
        if not connection.is_active:
            raise HTTPException(404, 'Provider connection not found')
        if connection.auth_type != 'api_key' or not connection.credential:
            raise HTTPException(
                400,
                'Media features require the provider API-key connection. Account sign-in only covers supported text inference.',
            )
        return connection, decrypt_credential(connection.credential)


@asynccontextmanager
async def media_http_session(connection: ProviderConnection | None = None):
    """Keep user-configured media endpoints behind the provider DNS guard."""
    if connection is None:
        from open_webui.utils.session_pool import get_session

        yield await get_session()
        return
    allow_private = validate_app_url(provider_base_url(connection))
    async with aiohttp.ClientSession(
        connector=ProviderTCPConnector(
            allow_private=allow_private, allowed_networks=allowed_ip_networks()
        ),
        timeout=aiohttp.ClientTimeout(total=300, connect=20),
        trust_env=False,
    ) as session:
        yield session


async def resolve_media_model(model_id, user, purpose, db=None, *, allow_disabled=False):
    """Resolve stable IDs with server-side purpose and connection authorization."""
    async with _provider_db_scope(db) as session:
        connection_id, upstream_id = parse_model_id(model_id)
        connection = await session.get(ProviderConnection, connection_id)
        await require_connection_access(connection, user, session)
        if (
            not connection.is_active
            or connection.is_enabled is False
            or (not allow_disabled and model_id in await disabled_provider_model_ids(session))
        ):
            raise HTTPException(404, 'Provider model not found')
        if connection.auth_type != 'api_key':
            raise HTTPException(400, 'This connection does not support media API calls')
        models = await visible_connection_models(connection, user, session)
        model = next((m for m in models if m['upstream_id'] == upstream_id), None)
        if not model or (not allow_disabled and not model.get('enabled', True)) or purpose not in model.get('uses', {}):
            raise HTTPException(400, 'Select a model supporting this purpose')
        return ResolvedProviderModel(connection, upstream_id, user.id, model)


async def resolve_configured_media(connection_id, model_id, user, purpose):
    # Existing provider settings store a connection and raw upstream ID. New clients
    # submit a stable model reference in the same model field.
    if not connection_id or not model_id:
        raise HTTPException(400, 'Select a connection and model')
    stable_id = (
        model_id
        if isinstance(model_id, str) and model_id.startswith('provider:')
        else stable_model_id(connection_id, model_id)
    )
    resolved = await resolve_media_model(stable_id, user, purpose)
    if resolved.connection.id != connection_id:
        raise HTTPException(400, 'Model does not belong to the selected connection')
    return resolved


async def visible_catalog_statuses(user, db=None):
    async with _provider_db_scope(db) as session:
        result = []
        for connection in (
            await session.scalars(select(ProviderConnection).where(ProviderConnection.is_active.is_(True)))
        ).all():
            if await can_access_connection(connection, user, session):
                snapshot = await catalog_snapshot(connection, user.id, session)
                result.append(
                    {
                        'connection_id': connection.id,
                        'connection_name': connection.name,
                        **catalog_status(connection, user.id, snapshot),
                    }
                )
        return result
