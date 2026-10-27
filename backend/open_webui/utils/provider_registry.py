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
        }
        for template in TEMPLATES
    ]


def get_template(template_id: str) -> dict:
    template = TEMPLATES_BY_ID.get(template_id)
    if template is None:
        raise ProviderRequestError('upstream', 400, detail=f'Unknown provider template: {template_id}')
    return template


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
