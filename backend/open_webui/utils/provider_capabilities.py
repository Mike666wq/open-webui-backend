"""Typed model uses; discovery never performs paid inference."""

import hashlib
import json

from fastapi import HTTPException
from open_webui.utils.provider_registry import (
    ADAPTER_OPERATIONS,
    MODEL_REQUIREMENT_NONE,
    resolve_template,
)

ADAPTERS = {
    'chat': {'openai-chat', 'responses'},
    'transcription': {'openai-transcription', 'minimax-transcription'},
    'speech': {'openai-speech', 'minimax-speech'},
    'image': {'openai-image', 'siliconflow-image', 'minimax-image'},
}
DEFAULT_ADAPTER = {key: sorted(value)[0] for key, value in ADAPTERS.items()}
MODEL_CAPABILITY_KEYS = frozenset({'vision', 'file_upload', 'web_search', 'file_context'})


def validate_model_capabilities(value: dict) -> dict[str, bool | None]:
    """Validate per-key updates; null deletes one explicit declaration."""
    if not isinstance(value, dict) or any(
        key not in MODEL_CAPABILITY_KEYS or (enabled is not None and type(enabled) is not bool)
        for key, enabled in value.items()
    ):
        raise HTTPException(400, 'Model capabilities must contain supported boolean or null flags')
    return dict(value)


def update_model_capability_declarations(model_info: dict, updates: dict | None) -> dict:
    """Preserve other model metadata while applying per-key declarations."""
    result = dict(model_info) if isinstance(model_info, dict) else {}
    if updates is None:
        result.pop('capabilities', None)
        return result
    declarations = result.get('capabilities')
    declarations = dict(declarations) if isinstance(declarations, dict) else {}
    for key, value in validate_model_capabilities(updates).items():
        if value is None:
            declarations.pop(key, None)
        else:
            declarations[key] = value
    if declarations:
        result['capabilities'] = declarations
    else:
        result.pop('capabilities', None)
    return result


def resolve_model_capabilities(connection, model_row, vendor_record) -> dict[str, bool]:
    """Resolve explicit declarations before vendor evidence, leaving unknown keys absent."""
    info = model_row.get('model_info') if isinstance(model_row, dict) else getattr(model_row, 'model_info', None)
    explicit = info.get('capabilities') if isinstance(info, dict) else None
    result = {'web_search': True}  # Platform search, independent of provider-hosted search.
    for record in (vendor_record, model_row):
        tags = record.get('capabilities') if isinstance(record, dict) else getattr(record, 'capabilities', None)
        if isinstance(tags, list) and any(tag in ('VU', 'Visual Understanding') for tag in tags):
            result['vision'] = True
            break
    if isinstance(explicit, dict):
        result.update({key: value for key, value in explicit.items() if key in MODEL_CAPABILITY_KEYS and type(value) is bool})
    return result

# The single place where an adapter's relative path is registered. An adapter
# appends its own path exactly once to a base URL; a complete endpoint is used
# verbatim. Nothing may guess a path from the shape of a URL.
ADAPTER_PATHS = {
    'openai-transcription': '/audio/transcriptions',
    'openai-speech': '/audio/speech',
    'openai-image': '/images/generations',
    'siliconflow-image': '/images/generations',
    'minimax-transcription': '/speech_to_text',
    'minimax-speech': '/t2a_v2',
    'minimax-image': '/image_generation',
}

# Adapters that speak an OpenAI-compatible media protocol. They may be used on
# a template that declares them; a native adapter never may.
NATIVE_ADAPTERS = {'minimax-transcription', 'minimax-speech', 'minimax-image'}

# The operations an image adapter supports. Text-to-image does not imply
# image editing: the two use different endpoints and different payloads. The
# table lives with the templates, so the metadata and the call path cannot
# drift apart.


def adapter_supports_operation(adapter: str, operation: str) -> bool:
    """Whether an adapter can serve one image operation."""
    if operation == 'generate':
        return True
    return operation in (ADAPTER_OPERATIONS.get(adapter) or ())


def provider_family(connection):
    from urllib.parse import urlsplit

    if connection.provider_id == 'custom' and urlsplit((connection.config or {}).get('base_url', '')).hostname in (
        'api.siliconflow.cn',
        'api.siliconflow.com',
    ):
        return 'siliconflow'
    return connection.provider_id


def connection_template_id(connection) -> str:
    """Return the template a connection was created from.

    A custom connection pointing at SiliconFlow behaves like the SiliconFlow
    template for service declarations, because that is the protocol it speaks.
    """
    template_id = getattr(connection, 'provider_template', None) or connection.provider_id
    if template_id == 'custom' and provider_family(connection) == 'siliconflow':
        return 'siliconflow'
    return template_id


def connection_services(connection) -> dict:
    """Return the service declarations that apply to a connection."""
    try:
        template = resolve_template(connection_template_id(connection))
    except Exception:
        return {}
    return template.get('services') or {}


def service_declaration(connection, purpose: str) -> dict:
    """Return one service declaration, or an explicit unsupported one."""
    services = connection_services(connection)
    declaration = services.get(purpose)
    if isinstance(declaration, dict):
        return declaration
    return {
        'supported': False,
        'unsupported_reason': '该提供商未声明此服务。',
        'adapters': [],
        'auth_types': [],
        'discovery': 'none',
        'model_requirement': MODEL_REQUIREMENT_NONE,
        'endpoint': {'kind': 'base_url', 'url': ''},
        'preset_models': [],
        'limits': {},
        'options': {},
        'notes': None,
    }


def validate_model_uses(connection, uses: dict, *, auth_type: str | None = None) -> dict:
    """Validate a set of purposes against the template's service declarations.

    Every write path uses this, so a purpose can never be smuggled in through
    `POST /connections` that the same connection could not get through the
    model endpoints. An OAuth connection can never obtain a media purpose.
    """
    if not isinstance(uses, dict):
        raise HTTPException(400, 'Model purposes must be an object')
    auth = auth_type if auth_type is not None else getattr(connection, 'auth_type', 'api_key')
    result = {}
    for purpose, adapter in uses.items():
        if purpose not in ADAPTERS:
            raise HTTPException(400, 'Unsupported purpose or adapter')
        if not isinstance(adapter, str) or adapter not in ADAPTERS[purpose]:
            raise HTTPException(400, 'Unsupported purpose or adapter')
        declaration = service_declaration(connection, purpose)
        if not declaration.get('supported'):
            raise HTTPException(
                400,
                declaration.get('unsupported_reason')
                or f'{connection_template_id(connection)} 不支持此服务。',
            )
        allowed = declaration.get('adapters') or []
        if adapter not in allowed:
            raise HTTPException(
                400,
                f'{connection_template_id(connection)} 的该服务不支持 {adapter} 适配器。',
            )
        if auth == 'oauth' and purpose != 'chat':
            raise HTTPException(400, '账号登录仅覆盖文本推理，不能用于媒体服务。')
        if auth != 'oauth' and 'oauth' in (declaration.get('auth_types') or ['api_key']):
            raise HTTPException(400, '该服务需要账号登录，而不是 API Key。')
        result[purpose] = adapter
    return result


def service_uses(declaration: dict) -> dict:
    """Return the purpose/adapter pair a preset model declares."""
    for model in declaration.get('preset_models') or []:
        uses = model.get('uses') or {}
        if uses:
            return dict(uses)
    return {}


SILICONFLOW_SPEECH = {'FunAudioLLM/CosyVoice2-0.5B', 'fnlp/MOSS-TTSD-v0.5'}
SILICONFLOW_PRESET_VOICES = ('alex', 'benjamin', 'charles', 'david', 'anna', 'bella', 'claire', 'diana')


def fingerprint(connection, model):
    # Never expose this hash of credentials; credentials may rotate without changing model uses.
    return hashlib.sha256(
        json.dumps(
            {
                'config': {k: v for k, v in (connection.config or {}).items() if not k.startswith('_')},
                'credential': connection.credential,
                'model': model['upstream_id'],
                'uses': model.get('uses', {}),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def public_verification(connection, model, uses, stored):
    """Expose verification results without the credential-derived fingerprint.

    A stored result is only trusted while its fingerprint still matches the
    current endpoint, credential and purposes.
    """
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for use in uses:
        entry = stored.get(use)
        if not isinstance(entry, dict):
            result[use] = {'status': 'untested'}
            continue
        if entry.get('fingerprint') != fingerprint(connection, model):
            result[use] = {'status': 'untested'}
            continue
        result[use] = {key: value for key, value in entry.items() if key != 'fingerprint'}
    return result


def normalize_whitelist_model(connection, row):
    """Build the public shape of a curated whitelist entry."""
    upstream = row['upstream_id']
    uses = dict(row.get('uses') or {})
    if connection.auth_type == 'oauth':
        uses = {'chat': uses.get('chat', 'openai-chat')}
    source = row.get('source') or 'provider'
    model = {
        'id': row.get('id') or f'provider:{connection.id}:{upstream}',
        'name': row.get('display_name') or upstream,
        'upstream_id': upstream,
        'provider_id': connection.provider_id,
        'connection_id': connection.id,
        'capabilities': row.get('capabilities') or [],
        'uses': uses,
        'features': row.get('features') or [],
        'model_info': row.get('model_info') or {},
    }
    result = {
        **model,
        'capability_source': 'unknown' if not uses else ('manual' if source == 'manual' else 'provider'),
        'enabled': bool(row.get('enabled', True)),
        'source': source,
    }
    if provider_family(connection) == 'siliconflow' and 'speech' in uses:
        result['voice_options'] = [
            {'id': f'{upstream}:{voice}', 'name': voice} for voice in SILICONFLOW_PRESET_VOICES
        ]
    result['verification'] = public_verification(connection, model, uses, row.get('verification') or {})
    return result


def normalize_model(connection, model):
    config = connection.config or {}
    upstream = model['upstream_id']
    raw = model.get('capabilities') or []
    if not isinstance(raw, list):
        raw = []
    uses = dict(model.get('uses') or {})
    source = 'provider' if uses else 'unknown'
    for capability in raw:
        aliases = {
            'TG': 'chat',
            'Text Generation': 'chat',
            'chat': 'chat',
            'speech-to-text': 'transcription',
            'text-to-speech': 'speech',
            'text-to-image': 'image',
        }
        if isinstance(capability, str) and capability in aliases:
            use = aliases[capability]
            uses[use] = DEFAULT_ADAPTER[use]
            source = 'provider'
    if connection.provider_id in ('chatgpt', 'kimi', 'deepseek'):
        uses = {'chat': 'responses' if connection.provider_id == 'chatgpt' else 'openai-chat'}
        source = 'provider-adapter'
    if upstream in SILICONFLOW_SPEECH and provider_family(connection) == 'siliconflow':
        uses['speech'] = 'openai-speech'
        source = 'provider-adapter'
    overrides = config.get('_model_overrides', {}).get(upstream)
    if overrides is not None and connection.auth_type != 'oauth':
        uses = overrides
        source = 'manual'
    if not uses and upstream in config.get(
        '_legacy_chat_ids',
        [m.get('upstream_id') for m in connection.models or []] if not config.get('_catalog_version') else [],
    ):
        uses = {'chat': 'responses' if config.get('api_type') == 'responses' else 'openai-chat'}
        source = 'legacy-unconfirmed'
    if connection.auth_type == 'oauth':
        uses = {'chat': uses.get('chat', 'openai-chat')}
    result = {**model, 'uses': uses, 'capability_source': source,
              'enabled': config.get('_model_enabled', {}).get(upstream,
                  not config.get('model_ids') or upstream in config['model_ids'])}
    if provider_family(connection) == 'siliconflow' and 'speech' in uses:
        result['voice_options'] = [
            {'id': f'{upstream}:{voice}', 'name': voice} for voice in SILICONFLOW_PRESET_VOICES
        ]
    test = config.get('_model_tests', {}).get(upstream, {})
    result['verification'] = public_verification(connection, result, uses, test)
    return result
