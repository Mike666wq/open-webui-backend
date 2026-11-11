"""One authoritative exit per media purpose.

Transcript, speech, image and realtime voice each used to carry their own
`engine` + URL + plaintext key in the config table, while the same instance also
held provider connections that know how to speak those protocols. Two sources of
truth for one call path means the two drift: production ended up with speech
pointing at a transcription URL and image still holding a plaintext key, even
though a shared connection declaring both services was sitting right there.

This module makes the connection the single exit. The legacy values do not
disappear — they stay readable so an administrator can see what was configured
and revert — but they stop being an independent authority: once a purpose is
assigned, resolution reads the connection, never the legacy key.

The old automatic migration is retired rather than repaired. It guessed which
connection "probably" corresponded to a legacy block by comparing a full
endpoint against a base URL and a decrypted key byte-for-byte; it could not
match a shared connection at all, and when it did fire it wrote a private
connection that every other user then got a 403 from. Assignment is now
explicit: the administrator picks the connection and the model, and the server
validates instead of guessing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.models.config import Config
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.provider_capabilities import ADAPTER_PATHS

# A purpose is assignable when an adapter exists for it. Realtime voice has a
# provider key and an engine in this codebase but no media adapter, so it is
# listed and reported honestly rather than silently omitted — an invisible
# purpose is exactly the corner this module exists to remove.
TRANSCRIPTION = 'transcription'
SPEECH = 'speech'
IMAGE = 'image'
REALTIME = 'realtime'

PURPOSE_ORDER = (TRANSCRIPTION, SPEECH, IMAGE, REALTIME)

PURPOSE_SPECS: dict[str, dict] = {
    TRANSCRIPTION: {
        'prefix': 'audio.stt',
        'model_key': 'model',
        'label': '语音转写',
        'media_purpose': TRANSCRIPTION,
        'engine_key': 'audio.stt.engine',
        'url_key': 'audio.stt.openai.api_base_url',
        'secret_key': 'audio.stt.openai.api_key',
    },
    SPEECH: {
        'prefix': 'audio.tts',
        'model_key': 'model',
        'label': '语音合成',
        'media_purpose': SPEECH,
        'engine_key': 'audio.tts.engine',
        'url_key': 'audio.tts.openai.api_base_url',
        'secret_key': 'audio.tts.openai.api_key',
    },
    IMAGE: {
        'prefix': 'image_generation',
        'model_key': 'model',
        'label': '生图',
        'media_purpose': IMAGE,
        'engine_key': 'image_generation.engine',
        'url_key': 'image_generation.openai.api_base_url',
        'secret_key': 'image_generation.openai.api_key',
    },
    REALTIME: {
        'prefix': 'audio.realtime',
        'model_key': 'MODEL',
        'label': '实时语音',
        'media_purpose': None,
        'engine_key': None,
        'url_key': 'audio.realtime.openai.api_base_url',
        'secret_key': 'audio.realtime.openai.api_key',
    },
}

# Every media path an adapter may append. Used to recognise a stored URL that is
# really a full endpoint, so the relative path is never appended a second time.
_KNOWN_MEDIA_PATHS = tuple(sorted(set(ADAPTER_PATHS.values()), key=len, reverse=True))

ASSIGNMENT_ERROR_PRIVATE = 'MEDIA_CONNECTION_PRIVATE'
ASSIGNMENT_ERROR_MODEL = 'MEDIA_MODEL_NOT_ENABLED'
ASSIGNMENT_ERROR_NOT_CONFIGURED = 'MEDIA_NOT_CONFIGURED'
ASSIGNMENT_ERROR_NOT_ASSIGNABLE = 'MEDIA_PURPOSE_NEEDS_ADAPTER'
ASSIGNMENT_ERROR_NO_PREVIOUS = 'MEDIA_NO_PREVIOUS_ASSIGNMENT'
MIGRATION_RETIRED = 'MEDIA_MIGRATION_RETIRED'


def purpose_spec(purpose: str) -> dict:
    spec = PURPOSE_SPECS.get(purpose)
    if spec is None:
        raise HTTPException(400, 'Unsupported media purpose')
    return spec


def is_assignable(purpose: str) -> bool:
    return purpose_spec(purpose)['media_purpose'] is not None


def normalize_base_url(url: str, purpose: str) -> tuple[str, bool]:
    """Return the base URL of a possibly-complete endpoint.

    A legacy block may hold `https://host/v1/images/generations` while the
    adapter expects the base and appends `/images/generations` itself. Appending
    blindly produced `/v1/images/generations/images/generations`. The second
    value reports whether a known media path was stripped, so the caller can say
    so out loud instead of quietly rewriting the administrator's input.
    """
    value = (url or '').strip().rstrip('/')
    if not value:
        return '', False
    for path in _KNOWN_MEDIA_PATHS:
        if value.endswith(path):
            return value[: -len(path)].rstrip('/'), True
    return value, False


async def _read(key: str | None):
    return await Config.get(key) if key else None


async def legacy_values(purpose: str) -> dict:
    """Read the legacy block of one purpose. Never returns a decrypted secret."""
    spec = purpose_spec(purpose)
    engine_key = spec['engine_key']
    engine = await _read(engine_key)
    if engine_key is None:
        # Realtime has no engine key; "enabled" is the closest thing to one.
        engine = 'openai-realtime' if await Config.get('audio.realtime.enabled') else ''
    return {
        'engine': (engine or '') if isinstance(engine, str) else '',
        'url': (await _read(spec['url_key'])) or '',
        'has_api_key': bool(await _read(spec['secret_key'])),
        'model': (await _read(f"{spec['prefix']}.{spec['model_key']}")) or '',
        # Realtime has no engine key: the feature flag is its on/off switch.
        'enabled': bool(await Config.get('audio.realtime.enabled')) if engine_key is None else True,
    }


def legacy_state(purpose: str, values: dict) -> dict:
    base, normalized = normalize_base_url(values['url'], purpose)
    # "Fields are filled in" is not the same as "an engine is selected".
    # `audio.tts.engine` empty means the speech endpoint answers 404 outright,
    # so the block must not be reported as a configured legacy setup.
    has_engine = bool(values['engine'])
    return {
        'engine': values['engine'],
        'base_url': values['url'],
        'normalized_base_url': base,
        'model': values['model'],
        'has_api_key': values['has_api_key'],
        'configured': bool(values['url'] and values['has_api_key'] and values['model']),
        'has_engine': has_engine,
        'normalized': normalized,
    }


def _mode(purpose: str, values: dict) -> str:
    connection_id = values.get('connection_id') or ''
    if values['engine'] == 'provider' and connection_id:
        return 'provider'
    if purpose == REALTIME:
        # `audio.realtime` ships a default base URL, so a non-empty URL says
        # nothing about whether the feature is on. The flag decides.
        return 'legacy' if values.get('enabled') else 'unconfigured'
    # Without an engine the legacy endpoint answers 404, so this is not a
    # legacy setup at all — the feature was never configured.
    if values['engine']:
        return 'legacy'
    return 'unconfigured'


async def read_assignment(purpose: str) -> dict:
    """The raw stored assignment of one purpose."""
    spec = purpose_spec(purpose)
    prefix = spec['prefix']
    values = await legacy_values(purpose)
    values['connection_id'] = await Config.get(f'{prefix}.provider_connection_id') or ''
    values['fingerprint'] = await Config.get(f'{prefix}._assignment_fingerprint') or ''
    values['previous'] = await Config.get(f'{prefix}._previous')
    return values


def adapter_for(purpose: str, model_metadata: dict | None) -> str | None:
    """The adapter the model itself declares for this purpose."""
    media_purpose = purpose_spec(purpose)['media_purpose']
    if media_purpose is None:
        return None
    uses = (model_metadata or {}).get('uses') or {}
    return uses.get(media_purpose)


async def assignment_state(purpose: str, db: AsyncSession | None = None) -> dict:
    """Contract §3.1: one purpose as the UI needs to see it."""
    spec = purpose_spec(purpose)
    values = await read_assignment(purpose)
    mode = _mode(purpose, values)
    legacy = legacy_state(purpose, values)
    state = {
        'purpose': purpose,
        'label': spec['label'],
        'mode': mode,
        'connection_id': values['connection_id'] if mode == 'provider' else '',
        'connection_name': '',
        'model': values['model'] if mode == 'provider' else '',
        'upstream_model': '',
        'adapter': '',
        'legacy': {
            key: legacy[key]
            for key in (
                'engine',
                'base_url',
                'normalized_base_url',
                'model',
                'has_api_key',
                'configured',
                'has_engine',
            )
        },
        'ready': False,
        'reasons': [],
    }

    if not is_assignable(purpose):
        state['reasons'].append(
            {
                'code': ASSIGNMENT_ERROR_NOT_ASSIGNABLE,
                'message': '实时语音目前还没有服务适配器，无法指派到服务商连接；它仍按自己的引擎配置运行。',
            }
        )
        return state

    if mode == 'provider':
        connection, model_metadata = await _resolve_stored_assignment(purpose, values)
        if connection is not None:
            state['connection_name'] = connection.name
            state['upstream_model'] = (model_metadata or {}).get('upstream_id') or ''
            state['adapter'] = adapter_for(purpose, model_metadata) or ''
        state['ready'] = connection is not None and bool(state['adapter'])
        if not state['ready']:
            state['reasons'].append(
                {
                    'code': ASSIGNMENT_ERROR_MODEL,
                    'message': '指派的服务模型已不可用，请重新指派或回退到旧配置。',
                }
            )
        return state

    if mode == 'legacy':
        # A legacy block is a direct connection to the vendor, outside any
        # provider connection: nothing here has been resolved, checked or
        # tested. "The fields are filled in" is not "this works" — production
        # carried a legacy image block whose every field was set while the
        # vendor answered 400 because the default size was below its floor.
        # Reporting it as ready would be a green light over a broken feature.
        state['ready'] = False
        state['reasons'].append(
            {
                'code': 'MEDIA_LEGACY_UNVERIFIED',
                'message': '当前走旧配置直连，尚未验证连通性：字段齐全不代表调用可用。'
                '建议指派到服务商连接，并用「测试」确认后再依赖它。',
            }
        )
        if legacy['normalized']:
            state['reasons'].append(
                {
                    'code': 'MEDIA_LEGACY_ENDPOINT_NORMALIZED',
                    'message': '旧配置保存的是完整请求地址，出站时按 base 归一化，避免相对路径被追加两次。',
                }
            )
        return state

    state['reasons'].append(
        {'code': ASSIGNMENT_ERROR_NOT_CONFIGURED, 'message': '该用途还没有配置任何出口。'}
    )
    return state


async def _resolve_stored_assignment(purpose: str, values: dict):
    """Resolve a stored assignment to (connection, model_metadata) or (None, None).

    Unlike the call path this never raises: the status view must be able to say
    "the assigned model disappeared" instead of handing the administrator a 404
    with no context.
    """
    from open_webui.utils.providers import _provider_db_scope, resolve_media_model

    connection_id = values.get('connection_id') or ''
    model_id = values.get('model') or ''
    if not connection_id or not model_id:
        return None, None
    try:
        async with _provider_db_scope(None) as db:
            connection = await db.get(ProviderConnection, connection_id)
        if connection is None or not connection.is_active:
            return None, None
        resolved = await resolve_media_model(
            model_id, _system_owner(), purpose_spec(purpose)['media_purpose'], allow_disabled=True
        )
        return resolved.connection, resolved.model_metadata
    except Exception:
        return None, None


class _Owner:
    """A minimal admin principal, so the status view can resolve any connection.

    The status endpoint is already restricted to administrators; resolution
    here only needs the access check to pass, and an administrator is allowed to
    see every connection on the instance.
    """

    def __init__(self):
        self.id = ''
        self.role = 'admin'
        self.email = ''
        self.name = ''

    def __getattr__(self, _name):
        return None


def _system_owner():
    return _Owner()


async def assignment_candidates(purpose: str, db: AsyncSession) -> list[dict]:
    """Contract §3.2: connections that declare this purpose, plus their models."""
    from open_webui.utils.provider_capabilities import service_declaration
    from open_webui.utils.providers import connection_models, provider_base_url

    spec = purpose_spec(purpose)
    media_purpose = spec['media_purpose']
    if media_purpose is None:
        return []

    connections = (
        await db.scalars(
            select(ProviderConnection)
            .where(ProviderConnection.is_active.is_(True))
            .order_by(ProviderConnection.created_at)
        )
    ).all()

    result = []
    for connection in connections:
        if connection.auth_type != 'api_key':
            continue
        try:
            declaration = service_declaration(connection, media_purpose)
        except Exception:
            continue
        if not declaration.get('supported'):
            continue
        try:
            provider_base_url(connection)
        except HTTPException:
            continue
        models = []
        try:
            for model in await connection_models(connection, db):
                if media_purpose not in (model.get('uses') or {}):
                    continue
                if not model.get('enabled', True):
                    continue
                verification = (model.get('verification') or {}).get(media_purpose) or {}
                models.append(
                    {
                        'id': model.get('id'),
                        'upstream_id': model.get('upstream_id'),
                        'display_name': model.get('display_name') or model.get('upstream_id'),
                        'enabled': True,
                        'verification': verification.get('status') or 'untested',
                    }
                )
        except Exception:
            models = []
        result.append(
            {
                'connection_id': connection.id,
                'name': connection.name,
                'provider_id': connection.provider_id,
                'visibility': connection.visibility,
                'adapter': (declaration.get('adapters') or [None])[0],
                'supported': True,
                'models': models,
            }
        )
    return result


async def apply_assignment(purpose: str, connection_id: str, model_id: str, user, db: AsyncSession) -> dict:
    """Contract §4.2: validate, then write the assignment."""
    from open_webui.utils.providers import connection_models, resolve_media_model, stable_model_id

    spec = purpose_spec(purpose)
    if not is_assignable(purpose):
        raise HTTPException(400, ASSIGNMENT_ERROR_NOT_ASSIGNABLE)

    media_purpose = spec['media_purpose']
    if not connection_id:
        raise HTTPException(400, 'Select a provider connection')
    connection = await db.get(ProviderConnection, connection_id)
    if connection is None or not connection.is_active:
        raise HTTPException(404, 'Provider connection not found')
    # Same rule the connection endpoints use: a shared connection is
    # administrator-managed, a private one belongs to its owner.
    if connection.visibility == 'shared':
        if getattr(user, 'role', '') != 'admin':
            raise HTTPException(403, 'Only administrators can assign a shared provider connection')
    elif connection.owner_id != getattr(user, 'id', None):
        raise HTTPException(404, 'Provider connection not found')

    # A media purpose serves every user holding the feature permission. A
    # private connection would answer 403 to all of them, so it may not back it.
    if connection.visibility != 'shared':
        raise HTTPException(400, ASSIGNMENT_ERROR_PRIVATE)

    from open_webui.utils.provider_capabilities import service_declaration

    declaration = service_declaration(connection, media_purpose)
    if not declaration.get('supported'):
        raise HTTPException(400, f'该连接不支持{spec["label"]}')

    stable = model_id if isinstance(model_id, str) and model_id.startswith('provider:') else ''
    if not stable:
        if not model_id:
            raise HTTPException(400, 'Select a model for this provider connection')
        stable = stable_model_id(connection_id, model_id)

    usable = await connection_models(connection, db)
    upstream = stable.rsplit(':', 1)[-1]
    match = next((m for m in usable if m.get('upstream_id') == upstream), None)
    if (
        match is None
        or not match.get('enabled', True)
        or media_purpose not in (match.get('uses') or {})
    ):
        raise HTTPException(400, ASSIGNMENT_ERROR_MODEL)

    await resolve_media_model(stable, user, media_purpose)  # raises on any access problem

    prefix = spec['prefix']
    fingerprint = hashlib.sha256(
        f'{purpose}|{connection_id}|{stable}'.encode()
    ).hexdigest()

    current = await read_assignment(purpose)
    if current['fingerprint'] == fingerprint:
        # Idempotent: the same assignment twice must not overwrite _previous.
        return await assignment_state(purpose, db)

    previous = {
        'engine': current['engine'],
        'provider_connection_id': current['connection_id'],
        'model': current['model'],
    }
    await Config.upsert(
        {
            f'{prefix}.engine': 'provider',
            f'{prefix}.provider_connection_id': connection_id,
            f'{prefix}.{spec["model_key"]}': stable,
            f'{prefix}._assignment_fingerprint': fingerprint,
            f'{prefix}._previous': previous,
        },
        db=db,
    )
    await db.commit()
    return await assignment_state(purpose, db)


async def revert_assignment(purpose: str, db: AsyncSession) -> dict:
    """Contract §4.3: put the pre-assignment values back."""
    spec = purpose_spec(purpose)
    values = await read_assignment(purpose)
    previous = values.get('previous')
    if not isinstance(previous, dict):
        raise HTTPException(409, ASSIGNMENT_ERROR_NO_PREVIOUS)

    prefix = spec['prefix']
    updates = {
        f'{prefix}.provider_connection_id': previous.get('provider_connection_id') or '',
        f'{prefix}.{spec["model_key"]}': previous.get('model') or '',
        f'{prefix}._assignment_fingerprint': '',
        f'{prefix}._previous': None,
    }
    if spec['engine_key']:
        updates[f'{prefix}.engine'] = previous.get('engine') or ''
    await Config.upsert(updates, db=db)
    await db.commit()
    return await assignment_state(purpose, db)


@dataclass
class MediaTarget:
    purpose: str
    mode: str
    adapter: str | None
    base_url: str
    endpoint: str
    model: str
    upstream_model: str | None
    connection_id: str | None


async def resolve_media_target(purpose: str, user, resolved=None) -> MediaTarget:
    """The single place a media call asks "where does this go?".

    `resolved` lets a caller that already resolved the model (the image path
    does, to read `model_metadata`) pass it in instead of resolving twice.
    """
    from open_webui.utils.provider_media import resolve_service
    from open_webui.utils.providers import resolve_configured_media

    spec = purpose_spec(purpose)
    media_purpose = spec['media_purpose']
    if media_purpose is None:
        raise HTTPException(400, ASSIGNMENT_ERROR_NOT_ASSIGNABLE)

    values = await read_assignment(purpose)
    mode = _mode(purpose, values)

    if mode == 'provider':
        resolved = resolved or await resolve_configured_media(
            values['connection_id'], values['model'], user, media_purpose
        )
        adapter = adapter_for(purpose, resolved.model_metadata)
        if not adapter:
            raise HTTPException(400, ASSIGNMENT_ERROR_MODEL)
        service = resolve_service(resolved.connection, media_purpose, adapter)
        return MediaTarget(
            purpose=purpose,
            mode='provider',
            adapter=service.adapter,
            base_url=service.base_url,
            endpoint=service.endpoint,
            model=values['model'],
            upstream_model=resolved.upstream_model_id,
            connection_id=values['connection_id'],
        )

    if mode == 'legacy':
        base, _ = normalize_base_url(values['url'], purpose)
        return MediaTarget(
            purpose=purpose,
            mode='legacy',
            adapter=None,
            base_url=base,
            endpoint='',
            model=values['model'],
            upstream_model=values['model'] or None,
            connection_id=None,
        )

    raise HTTPException(400, ASSIGNMENT_ERROR_NOT_CONFIGURED)


# ── Secret redaction ────────────────────────────────────────────────────────
#
# The config GET endpoints used to hand the decrypted provider key back to the
# browser as plain text. Anyone with the admin session — or anything that could
# read the response — could lift a vendor credential straight out of a settings
# page. The field is now write-only: GET reports whether a key exists, never
# what it is.
#
# An empty string on save means "leave it alone". That is the only sensible
# reading of a blank password box, and it keeps the common edit (change the
# model, keep the key) from silently wiping the credential.

SECRET_SUFFIX = 'API_KEY'
CLEAR_SENTINEL = '__clear__'


def is_secret_field(name: str) -> bool:
    # Storage keys are lowercase dotted paths (`audio.tts.openai.api_key`) while
    # the settings payload uses uppercase field names (`OPENAI_API_KEY`). One
    # predicate has to recognise both, or a save would slip a key through.
    return isinstance(name, str) and name.upper().endswith(SECRET_SUFFIX)


def redact_secrets(values: dict) -> tuple[dict, dict]:
    """Split a config payload into (redacted copy, {field: has_key})."""
    redacted = {}
    present = {}
    for key, value in values.items():
        if is_secret_field(key):
            present[key] = bool(value)
            redacted[key] = ''
        else:
            redacted[key] = value
    return redacted, present


def redact_nested(payload: dict) -> tuple[dict, dict]:
    """Redact a payload that may contain nested per-purpose objects (audio)."""
    redacted = {}
    present = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            sub_redacted, sub_present = redact_nested(value)
            redacted[key] = sub_redacted
            for field, has_key in sub_present.items():
                present[f'{key}.{field}'] = has_key
        elif is_secret_field(key):
            present[key] = bool(value)
            redacted[key] = ''
        else:
            redacted[key] = value
    return redacted, present


async def restore_secrets(updates: dict, existing: dict) -> dict:
    """Apply the write-only rule to an incoming save.

    Empty or missing -> keep the stored value. `__clear__` -> delete it. Any
    other value -> store it.
    """
    result = {}
    for field, value in updates.items():
        if not is_secret_field(field):
            result[field] = value
            continue
        if value == CLEAR_SENTINEL:
            result[field] = ''
        elif value in (None, ''):
            # Leave the stored credential untouched rather than erasing it.
            continue
        else:
            result[field] = value
    return result
