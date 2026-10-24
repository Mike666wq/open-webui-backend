"""Typed model uses; discovery never performs paid inference."""

import hashlib
import json

ADAPTERS = {
    'chat': {'openai-chat', 'responses'},
    'transcription': {'openai-transcription'},
    'speech': {'openai-speech'},
    'image': {'openai-image', 'siliconflow-image'},
}
DEFAULT_ADAPTER = {key: sorted(value)[0] for key, value in ADAPTERS.items()}


def provider_family(connection):
    from urllib.parse import urlsplit

    if connection.provider_id == 'custom' and urlsplit((connection.config or {}).get('base_url', '')).hostname in (
        'api.siliconflow.cn',
        'api.siliconflow.com',
    ):
        return 'siliconflow'
    return connection.provider_id


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
    result = {**model, 'uses': uses, 'capability_source': source}
    if provider_family(connection) == 'siliconflow' and 'speech' in uses:
        result['voice_options'] = [
            {'id': f'{upstream}:{voice}', 'name': voice} for voice in SILICONFLOW_PRESET_VOICES
        ]
    test = config.get('_model_tests', {}).get(upstream, {})
    result['verification'] = {
        use: (
            {k: v for k, v in test.get(use, {}).items() if k != 'fingerprint'}
            if test.get(use, {}).get('fingerprint') == fingerprint(connection, result)
            else {'status': 'untested'}
        )
        for use in uses
    }
    return result
