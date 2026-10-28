"""Provider templates.

The catalog groups the providers the way the official documentation does: by
the protocol that decides how a call is made. Each template carries the type,
the supported authentication, the default endpoint and whether this release
can actually talk to it.

A template whose ``supported`` flag is false is listed so the interface can
explain the situation, but adding such a connection is refused. The platform
never falls back to the OpenAI protocol for a provider it cannot speak.
"""

from open_webui.utils.provider_errors import ProviderRequestError

PROTOCOL_OPENAI_COMPATIBLE = 'openai-compatible'
PROTOCOL_OPENAI_RESPONSES = 'openai-responses'
PROTOCOL_ANTHROPIC = 'anthropic-messages'
PROTOCOL_GEMINI = 'gemini'
PROTOCOL_AZURE_OPENAI = 'azure-openai'
PROTOCOL_BEDROCK = 'bedrock-converse'
PROTOCOL_LOCAL = 'local'

PROTOCOLS = {
    PROTOCOL_OPENAI_COMPATIBLE: {'label': 'OpenAI 兼容', 'supported': True},
    PROTOCOL_OPENAI_RESPONSES: {'label': 'OpenAI Responses', 'supported': True},
    PROTOCOL_ANTHROPIC: {'label': 'Anthropic 兼容', 'supported': False},
    PROTOCOL_GEMINI: {'label': 'Google Gemini', 'supported': False},
    PROTOCOL_AZURE_OPENAI: {'label': 'Azure OpenAI', 'supported': False},
    PROTOCOL_BEDROCK: {'label': 'AWS Bedrock', 'supported': False},
    PROTOCOL_LOCAL: {'label': '本地推理', 'supported': False},
}

GROUP_OFFICIAL = 'official'
GROUP_CN = 'cn'
GROUP_GATEWAY = 'gateway'
GROUP_OTHER = 'other'

GROUPS = [
    {'id': GROUP_OFFICIAL, 'label': '官方', 'order': 0},
    {'id': GROUP_CN, 'label': '国产', 'order': 1},
    {'id': GROUP_GATEWAY, 'label': '聚合与特殊网关', 'order': 2},
    {'id': GROUP_OTHER, 'label': '其他', 'order': 3},
]

UNSUPPORTED_REASON = '该提供商类型尚未实装，请改用 OpenAI 兼容端点接入。'

# Version of the service contract the templates describe. The frontend reads
# this to know whether it may rely on the per-service metadata below.
SERVICE_CONTRACT_VERSION = 1

PURPOSE_CHAT = 'chat'
PURPOSE_TRANSCRIPTION = 'transcription'
PURPOSE_SPEECH = 'speech'
PURPOSE_IMAGE = 'image'

PURPOSES = (PURPOSE_CHAT, PURPOSE_TRANSCRIPTION, PURPOSE_SPEECH, PURPOSE_IMAGE)

# Which operations one adapter can serve.
#
# The image service is the only one with more than one operation: a provider
# can generate images without being able to edit them. The table is the single
# authority, so the templates, the connection metadata and the call path all
# agree about what an adapter can do.
ADAPTER_OPERATIONS = {
    'openai-image': ('generate', 'edit'),
    'siliconflow-image': ('generate',),
    'minimax-image': ('generate',),
}

# How the models of a service are obtained. `remote` is the only one that
# talks to the provider's model list; the others never trigger a catalog call
# and must not report a catalog failure.
DISCOVERY_REMOTE = 'remote'
DISCOVERY_PRESET = 'preset'
DISCOVERY_MANUAL = 'manual'
DISCOVERY_NONE = 'none'

# Whether the model parameter has to travel with the request. `defaulted`
# means the vendor has a default value but the parameter is still required on
# the wire; `none` means the protocol really has no model parameter.
MODEL_REQUIREMENT_REQUIRED = 'required'
MODEL_REQUIREMENT_DEFAULTED = 'defaulted'
MODEL_REQUIREMENT_NONE = 'none'

MEDIA_UNSUPPORTED_OAUTH = '账号登录仅覆盖文本推理，不能用于语音转写、语音合成或生图。'
MEDIA_UNSUPPORTED_NONE = '该提供商未提供此服务。'
MEDIA_UNSUPPORTED_LATER = '该提供商的原生媒体接口本期未接入。'


def _service(
    *,
    supported: bool = True,
    adapters: tuple = (),
    auth_types: tuple = ('api_key',),
    discovery: str = DISCOVERY_REMOTE,
    model_requirement: str = MODEL_REQUIREMENT_REQUIRED,
    endpoint: dict | None = None,
    preset_models: tuple = (),
    limits: dict | None = None,
    options: dict | None = None,
    unsupported_reason: str | None = None,
    notes: str | None = None,
) -> dict:
    """Build one service declaration of a provider template.

    The declaration is the single authority on what a template can do. The
    frontend renders it; the backend validates against it. Nothing here is
    derived from the root chat protocol: an OpenAI-compatible chat endpoint
    says nothing about whether the vendor has a transcription API.
    """
    declaration = {
        'supported': supported,
        'unsupported_reason': unsupported_reason,
        'adapters': list(adapters),
        'auth_types': list(auth_types),
        'discovery': discovery,
        'model_requirement': model_requirement,
        'endpoint': endpoint or {'kind': 'base_url', 'url': ''},
        'preset_models': [dict(model) for model in preset_models],
        'limits': dict(limits or {}),
        'options': {key: list(values) for key, values in (options or {}).items()},
        'notes': notes,
    }
    # A service with a single operation does not need the field: the purpose
    # already names it. The image service declares what its adapters can do, so
    # the interface never offers editing for a generate-only adapter.
    operations = sorted({operation for adapter in adapters for operation in ADAPTER_OPERATIONS.get(adapter, ())})
    if operations:
        declaration['operations'] = operations
    return declaration


def _unsupported_service(reason: str) -> dict:
    return _service(supported=False, unsupported_reason=reason, discovery=DISCOVERY_NONE)


_OPENAI_CHAT = _service(
    adapters=('openai-chat', 'responses'),
    endpoint={'kind': 'base_url', 'url': 'https://api.openai.com/v1'},
)
_OPENAI_TRANSCRIPTION = _service(
    adapters=('openai-transcription',),
    endpoint={'kind': 'base_url', 'url': 'https://api.openai.com/v1'},
    options={'response_format': ['json', 'text', 'verbose_json']},
)
_OPENAI_SPEECH = _service(
    adapters=('openai-speech',),
    endpoint={'kind': 'base_url', 'url': 'https://api.openai.com/v1'},
    options={'response_format': ['mp3', 'wav', 'opus', 'aac', 'flac', 'pcm']},
)
_OPENAI_IMAGE = _service(
    adapters=('openai-image',),
    endpoint={'kind': 'base_url', 'url': 'https://api.openai.com/v1'},
    options={
        'size': ['auto', '1024x1024', '1024x1536', '1536x1024'],
        'response_format': ['b64_json', 'url'],
    },
)

_MINIMAX_MEDIA_ROOT = 'https://api.minimax.cn/v1'

_MINIMAX_TRANSCRIPTION = _service(
    adapters=('minimax-transcription',),
    discovery=DISCOVERY_PRESET,
    model_requirement=MODEL_REQUIREMENT_DEFAULTED,
    endpoint={'kind': 'base_url', 'url': _MINIMAX_MEDIA_ROOT},
    preset_models=(
        {
            'upstream_id': 'asr-1.0',
            'display_name': 'asr-1.0',
            'uses': {'transcription': 'minimax-transcription'},
        },
    ),
    limits={
        'max_bytes': 50 * 1024 * 1024,
        'max_seconds': 500,
        'input_formats': ['wav', 'aiff', 'flac', 'm4a', 'mp3', 'aac', 'opus', 'ogg'],
    },
    options={'response_format': ['json', 'verbose_json']},
    notes='model 仍必须随请求发送；语言走请求头，未指定即混合语言识别。',
)

_MINIMAX_SPEECH = _service(
    adapters=('minimax-speech',),
    discovery=DISCOVERY_PRESET,
    endpoint={'kind': 'base_url', 'url': _MINIMAX_MEDIA_ROOT},
    preset_models=tuple(
        {
            'upstream_id': model,
            'display_name': model,
            'uses': {'speech': 'minimax-speech'},
        }
        for model in (
            'speech-2.8-hd',
            'speech-2.8-turbo',
            'speech-2.6-hd',
            'speech-2.6-turbo',
            'speech-02-hd',
            'speech-02-turbo',
            'speech-01-hd',
            'speech-01-turbo',
        )
    ),
    limits={'max_text_chars': 10000},
    options={'format': ['mp3'], 'output_format': ['hex'], 'voice_id': 'manual'},
    notes='同步非流式合成；音色 ID 独立于模型，不自动激活或克隆音色。',
)

_MINIMAX_IMAGE = _service(
    adapters=('minimax-image',),
    discovery=DISCOVERY_PRESET,
    endpoint={'kind': 'base_url', 'url': _MINIMAX_MEDIA_ROOT},
    preset_models=tuple(
        {
            'upstream_id': model,
            'display_name': model,
            'uses': {'image': 'minimax-image'},
        }
        for model in ('image-01', 'image-01-live')
    ),
    limits={
        'max_prompt_chars': 1500,
        'min_dimension': 512,
        'max_dimension': 2048,
        'dimension_multiple': 8,
        'max_images': 9,
    },
    options={
        'response_format': ['base64', 'url'],
        'aspect_ratio': ['1:1', '16:9', '4:3', '3:2', '2:3', '3:4', '9:16', '21:9'],
        'n': [1, 2, 3, 4, 5, 6, 7, 8, 9],
    },
    notes='本期仅文生图，不支持图生图或图片编辑；国际区只登记已确认的 image-01。',
)

TEMPLATES = [
    {
        'id': 'openai',
        'name': 'OpenAI',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_OFFICIAL,
        'auth_types': ['api_key'],
        'default_base_url': 'https://api.openai.com/v1',
        'docs_url': 'https://platform.openai.com/docs',
        'supported': True,
        'services': {
            'chat': _OPENAI_CHAT,
            'transcription': _OPENAI_TRANSCRIPTION,
            'speech': _OPENAI_SPEECH,
            'image': _OPENAI_IMAGE,
        },
    },
    {
        'id': 'chatgpt',
        'name': 'ChatGPT',
        'protocol': PROTOCOL_OPENAI_RESPONSES,
        'group': GROUP_OFFICIAL,
        'auth_types': ['oauth'],
        'default_base_url': '',
        'docs_url': 'https://help.openai.com/en/articles/6825453-chatgpt-release-notes',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('responses',),
                auth_types=('oauth',),
                endpoint={'kind': 'base_url', 'url': ''},
            ),
            'transcription': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
            'speech': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
            'image': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
        },
    },
    {
        'id': 'minimax',
        'name': 'MiniMax',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_CN,
        'auth_types': ['api_key'],
        'default_base_url': 'https://api.minimax.chat/v1',
        'docs_url': 'https://platform.minimaxi.com/document',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://api.minimax.chat/v1'},
            ),
            'transcription': _MINIMAX_TRANSCRIPTION,
            'speech': _MINIMAX_SPEECH,
            'image': _MINIMAX_IMAGE,
        },
    },
    {
        'id': 'deepseek',
        'name': 'DeepSeek',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_CN,
        'auth_types': ['api_key'],
        'default_base_url': 'https://api.deepseek.com/v1',
        'docs_url': 'https://api-docs.deepseek.com/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://api.deepseek.com/v1'},
            ),
            'transcription': _unsupported_service(MEDIA_UNSUPPORTED_NONE),
            'speech': _unsupported_service(MEDIA_UNSUPPORTED_NONE),
            'image': _unsupported_service(MEDIA_UNSUPPORTED_NONE),
        },
    },
    {
        'id': 'siliconflow',
        'name': '硅基流动',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_CN,
        'auth_types': ['api_key'],
        'default_base_url': 'https://api.siliconflow.cn/v1',
        'docs_url': 'https://docs.siliconflow.cn/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://api.siliconflow.cn/v1'},
            ),
            'transcription': _service(
                adapters=('openai-transcription',),
                endpoint={'kind': 'base_url', 'url': 'https://api.siliconflow.cn/v1'},
            ),
            'speech': _service(
                adapters=('openai-speech',),
                endpoint={'kind': 'base_url', 'url': 'https://api.siliconflow.cn/v1'},
            ),
            'image': _service(
                adapters=('siliconflow-image', 'openai-image'),
                endpoint={'kind': 'base_url', 'url': 'https://api.siliconflow.cn/v1'},
            ),
        },
    },
    {
        'id': 'bailian',
        'name': '百炼',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_CN,
        'auth_types': ['api_key'],
        'default_base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'docs_url': 'https://help.aliyun.com/zh/model-studio/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://dashscope.aliyuncs.com/compatible-mode/v1'},
            ),
            'transcription': _unsupported_service(MEDIA_UNSUPPORTED_LATER),
            'speech': _unsupported_service(MEDIA_UNSUPPORTED_LATER),
            'image': _unsupported_service(MEDIA_UNSUPPORTED_LATER),
        },
    },
    {
        'id': 'kimi',
        'name': 'Kimi Code',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_CN,
        'auth_types': ['oauth'],
        'default_base_url': 'https://api.kimi.com/coding/v1',
        'docs_url': 'https://www.kimi.com/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                auth_types=('oauth',),
                endpoint={'kind': 'base_url', 'url': 'https://api.kimi.com/coding/v1'},
            ),
            'transcription': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
            'speech': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
            'image': _unsupported_service(MEDIA_UNSUPPORTED_OAUTH),
        },
    },
    {
        'id': 'custom',
        'name': 'New API / OpenAI 兼容',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_GATEWAY,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': '',
        'supported': True,
        'services': {
            'chat': _service(adapters=('openai-chat',)),
            'transcription': _service(adapters=('openai-transcription',)),
            'speech': _service(adapters=('openai-speech',)),
            'image': _service(adapters=('openai-image',)),
        },
    },
    {
        'id': 'newapi',
        'name': 'New API',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_GATEWAY,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': 'https://docs.newapi.pro/',
        'supported': True,
        'services': {
            'chat': _service(adapters=('openai-chat',)),
            'transcription': _service(adapters=('openai-transcription',)),
            'speech': _service(adapters=('openai-speech',)),
            'image': _service(adapters=('openai-image',)),
        },
    },
    {
        'id': 'oneapi',
        'name': 'One API',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_GATEWAY,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': '',
        'supported': True,
        'services': {
            'chat': _service(adapters=('openai-chat',)),
            'transcription': _service(adapters=('openai-transcription',)),
            'speech': _service(adapters=('openai-speech',)),
            'image': _service(adapters=('openai-image',)),
        },
    },
    {
        'id': 'aihubmix',
        'name': 'AiHubMix',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_GATEWAY,
        'auth_types': ['api_key'],
        'default_base_url': 'https://aihubmix.com/v1',
        'docs_url': 'https://docs.aihubmix.com/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://aihubmix.com/v1'},
            ),
            'transcription': _service(adapters=('openai-transcription',)),
            'speech': _service(adapters=('openai-speech',)),
            'image': _service(adapters=('openai-image',)),
        },
    },
    {
        'id': 'dmxapi',
        'name': 'DMXAPI',
        'protocol': PROTOCOL_OPENAI_COMPATIBLE,
        'group': GROUP_GATEWAY,
        'auth_types': ['api_key'],
        'default_base_url': 'https://www.dmxapi.com/v1',
        'docs_url': 'https://www.dmxapi.com/',
        'supported': True,
        'services': {
            'chat': _service(
                adapters=('openai-chat',),
                endpoint={'kind': 'base_url', 'url': 'https://www.dmxapi.com/v1'},
            ),
            'transcription': _service(adapters=('openai-transcription',)),
            'speech': _service(adapters=('openai-speech',)),
            'image': _service(adapters=('openai-image',)),
        },
    },
    {
        'id': 'anthropic',
        'name': 'Anthropic',
        'protocol': PROTOCOL_ANTHROPIC,
        'group': GROUP_OTHER,
        'auth_types': ['api_key'],
        'default_base_url': 'https://api.anthropic.com',
        'docs_url': 'https://docs.anthropic.com/',
        'supported': False,
        'services': {
            'chat': _unsupported_service(UNSUPPORTED_REASON),
            'transcription': _unsupported_service(UNSUPPORTED_REASON),
            'speech': _unsupported_service(UNSUPPORTED_REASON),
            'image': _unsupported_service(UNSUPPORTED_REASON),
        },
    },
    {
        'id': 'gemini',
        'name': 'Google Gemini',
        'protocol': PROTOCOL_GEMINI,
        'group': GROUP_OTHER,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': 'https://ai.google.dev/docs',
        'supported': False,
        'services': {
            'chat': _unsupported_service(UNSUPPORTED_REASON),
            'transcription': _unsupported_service(UNSUPPORTED_REASON),
            'speech': _unsupported_service(UNSUPPORTED_REASON),
            'image': _unsupported_service(UNSUPPORTED_REASON),
        },
    },
    {
        'id': 'azure-openai',
        'name': 'Azure OpenAI',
        'protocol': PROTOCOL_AZURE_OPENAI,
        'group': GROUP_OTHER,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': 'https://learn.microsoft.com/azure/ai-services/openai/',
        'supported': False,
        'services': {
            'chat': _unsupported_service(UNSUPPORTED_REASON),
            'transcription': _unsupported_service(UNSUPPORTED_REASON),
            'speech': _unsupported_service(UNSUPPORTED_REASON),
            'image': _unsupported_service(UNSUPPORTED_REASON),
        },
    },
    {
        'id': 'bedrock',
        'name': 'AWS Bedrock',
        'protocol': PROTOCOL_BEDROCK,
        'group': GROUP_OTHER,
        'auth_types': ['api_key'],
        'default_base_url': '',
        'docs_url': 'https://docs.aws.amazon.com/bedrock/',
        'supported': False,
        'services': {
            'chat': _unsupported_service(UNSUPPORTED_REASON),
            'transcription': _unsupported_service(UNSUPPORTED_REASON),
            'speech': _unsupported_service(UNSUPPORTED_REASON),
            'image': _unsupported_service(UNSUPPORTED_REASON),
        },
    },
    {
        'id': 'ollama',
        'name': '本地推理',
        'protocol': PROTOCOL_LOCAL,
        'group': GROUP_OTHER,
        'auth_types': ['api_key'],
        'default_base_url': 'http://localhost:11434/v1',
        'docs_url': 'https://ollama.com/',
        'supported': False,
        'services': {
            'chat': _unsupported_service(UNSUPPORTED_REASON),
            'transcription': _unsupported_service(UNSUPPORTED_REASON),
            'speech': _unsupported_service(UNSUPPORTED_REASON),
            'image': _unsupported_service(UNSUPPORTED_REASON),
        },
    },
]

TEMPLATES_BY_ID = {template['id']: template for template in TEMPLATES}

GROUP_LABELS = {group['id']: group['label'] for group in GROUPS}


def public_templates() -> list[dict]:
    """Return the template catalog for the interface."""
    return [
        {
            **template,
            'group_label': GROUP_LABELS.get(template['group'], template['group']),
            'protocol_label': PROTOCOLS.get(template['protocol'], {}).get('label', template['protocol']),
            'unsupported_reason': None if template['supported'] else UNSUPPORTED_REASON,
            'service_contract_version': SERVICE_CONTRACT_VERSION,
        }
        for template in TEMPLATES
    ]


def get_template(template_id: str) -> dict:
    template = TEMPLATES_BY_ID.get(template_id)
    if template is None:
        raise ProviderRequestError('upstream', 400, detail=f'Unknown provider template: {template_id}')
    return template


def resolve_template(provider_template: str | None = None, provider_id: str | None = None) -> dict:
    """Resolve the template of a create request.

    `provider_template` is authoritative. `provider_id` is only consulted when
    no template was named, so a client that still sends the legacy field does
    not silently fall back to `custom` and lose the selected provider. The
    ephemeral verification and the real save must both call this, so both
    resolve to the same provider id, protocol, default address and services.
    """
    template_id = (provider_template or '').strip() or (provider_id or '').strip() or 'custom'
    return get_template(template_id)


def template_services(template_id: str) -> dict:
    """Return the service declarations of one template."""
    return get_template(template_id).get('services') or {}


def require_supported_template(template_id: str) -> dict:
    """Return a template, refusing the ones this release cannot talk to."""
    template = get_template(template_id)
    if not template['supported']:
        raise ProviderRequestError(
            'upstream', 400, detail=f'{template["name"]} 尚未实装，请改用 OpenAI 兼容端点接入'
        )
    return template


def default_base_urls() -> dict[str, str]:
    return {template['id']: template['default_base_url'] for template in TEMPLATES}
