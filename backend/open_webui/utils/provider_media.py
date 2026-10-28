"""One media call contract shared by the daily features and the explicit test.

The chat microphone, the recordings page, read-aloud and the image settings
must reach a provider through the same adapter, the same address resolution,
the same input limits and the same response normalization as the explicit
model test. If the test owns a second copy of the request code, a service can
pass its test and still fail in daily use.

This module owns that contract:

- `resolve_service` picks the adapter, resolves the final outbound URL and
  validates the options against the adapter's own declaration.
- `call_media_service` performs **exactly one** upstream call and returns a
  normalized result.
- `fetch_voices` lists the voices of a speech service, for adapters that can.

The MiniMax native protocol lives in `provider_minimax.py`; the
OpenAI-compatible shapes live here. Nothing else may build a media request.
"""

import json
import mimetypes
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException
from open_webui.utils import provider_minimax
from open_webui.utils.apps import validate_app_url
from open_webui.utils.provider_capabilities import (
    ADAPTERS,
    ADAPTER_PATHS,
    service_declaration,
)
from open_webui.utils.provider_errors import (
    STAGE_INFERENCE,
)

PURPOSE_TRANSCRIPTION = 'transcription'
PURPOSE_SPEECH = 'speech'
PURPOSE_IMAGE = 'image'

MEDIA_PURPOSES = (PURPOSE_TRANSCRIPTION, PURPOSE_SPEECH, PURPOSE_IMAGE)

# Media answers are larger than a model list: a base64 image is a few
# megabytes. The bound still exists so a runaway upstream cannot exhaust the
# server memory.
MAX_MEDIA_RESPONSE_BYTES = 32 * 1024 * 1024

MEDIA_TIMEOUT = 300

SERVICE_CONFIG_FIELDS = {'enabled', 'adapter', 'endpoint', 'options'}
ENDPOINT_KINDS = ('base_url', 'endpoint_url')


@dataclass
class MediaService:
    """One resolved media service of one connection."""

    purpose: str
    adapter: str
    endpoint: str
    options: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    discovery: str = 'remote'
    model_requirement: str = 'required'
    base_url: str = ''
    endpoint_kind: str = 'base_url'


def validate_final_url(url: str) -> str:
    """Validate a URL the server is about to call.

    Credentials in the URL, query strings and fragments are refused: the
    address of a provider service is not a place to carry a secret, and a
    fragment never reaches the server anyway.
    """
    value = (url or '').strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ('http', 'https')
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(400, '服务地址必须是有效的 HTTP(S) 地址，且不能包含凭据、查询参数或片段')
    try:
        validate_app_url(value)
    except Exception:
        raise HTTPException(400, '服务地址必须是有效的 HTTP(S) 地址') from None
    return value


def service_config(connection, purpose: str) -> dict:
    """Return the stored configuration of one service, or an empty object."""
    services = (connection.config or {}).get('services')
    if not isinstance(services, dict):
        return {}
    entry = services.get(purpose)
    return entry if isinstance(entry, dict) else {}


def service_endpoint_config(connection, purpose: str) -> dict:
    """Return the stored endpoint block of one service, or an empty object."""
    endpoint = service_config(connection, purpose).get('endpoint')
    return endpoint if isinstance(endpoint, dict) else {}


def validate_options(connection, purpose: str, adapter: str | None, options) -> dict:
    """Validate the service options against the adapter's own declaration.

    Unknown fields are refused before the request goes out, so a typo cannot
    silently reach the vendor as an unsupported parameter.
    """
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise HTTPException(400, '服务选项必须是对象')
    declaration = service_declaration(connection, purpose)
    allowed = declaration.get('options') or {}
    result = {}
    for key, value in options.items():
        if key not in allowed:
            raise HTTPException(400, f'不支持的服务选项：{key}')
        values = allowed.get(key)
        if isinstance(values, list) and values and 'manual' not in values and value not in values:
            raise HTTPException(400, f'服务选项 {key} 的取值不被支持')
        if isinstance(values, list) and 'manual' in values and not isinstance(value, str):
            raise HTTPException(400, f'服务选项 {key} 必须是文本')
        result[key] = value
    return result


def validate_service_config(connection, purpose: str, config) -> dict:
    """Validate one service configuration block and return the cleaned copy."""
    if not isinstance(config, dict):
        raise HTTPException(400, '服务配置必须是对象')
    unknown = set(config) - SERVICE_CONFIG_FIELDS
    if unknown:
        raise HTTPException(400, 'Unsupported provider service field')
    declaration = service_declaration(connection, purpose)
    result = {}
    if 'enabled' in config:
        if not isinstance(config['enabled'], bool):
            raise HTTPException(400, '服务开关必须是布尔值')
        result['enabled'] = config['enabled']
    adapter = config.get('adapter')
    if adapter is not None:
        if adapter not in ADAPTERS.get(purpose, set()):
            raise HTTPException(400, 'Unsupported purpose or adapter')
        if adapter not in (declaration.get('adapters') or []):
            raise HTTPException(400, f'该服务的模板不支持 {adapter} 适配器。')
        result['adapter'] = adapter
    endpoint = config.get('endpoint')
    if endpoint is not None:
        if not isinstance(endpoint, dict) or set(endpoint) - {'kind', 'url'}:
            raise HTTPException(400, '服务地址格式无效')
        kind = endpoint.get('kind')
        if kind not in ENDPOINT_KINDS:
            raise HTTPException(400, '服务地址类型只能是 base_url 或 endpoint_url')
        url = validate_final_url((endpoint.get('url') or '').strip())
        result['endpoint'] = {'kind': kind, 'url': url}
    options = config.get('options')
    if options is not None:
        chosen = adapter or (declaration.get('adapters') or [None])[0]
        result['options'] = validate_options(connection, purpose, chosen, options)
    return result


def resolve_endpoint(connection, purpose: str, adapter: str | None) -> tuple[str, str, str]:
    """Resolve the final outbound URL of one service.

    Returns the URL, the base URL it was derived from and the endpoint kind.
    A stored `endpoint_url` is used verbatim, exactly once. A stored
    `base_url` gets the adapter's registered relative path appended exactly
    once. A connection without any service configuration keeps the legacy
    OpenAI-compatible behaviour.
    """
    path = ADAPTER_PATHS.get(adapter or '')
    endpoint = service_endpoint_config(connection, purpose)
    if endpoint:
        kind = endpoint.get('kind')
        url = (endpoint.get('url') or '').strip()
        if kind == 'endpoint_url':
            return validate_final_url(url), '', 'endpoint_url'
        if kind == 'base_url':
            if not path:
                raise HTTPException(400, '该服务没有可追加的相对路径，请填写完整的请求地址。')
            base = validate_final_url(url.rstrip('/'))
            return validate_final_url(base + path), base, 'base_url'
        raise HTTPException(400, '服务地址类型只能是 base_url 或 endpoint_url')
    from open_webui.utils.providers import provider_base_url

    base = provider_base_url(connection)
    if not base:
        raise HTTPException(400, '该连接没有可用的服务地址')
    if not path:
        raise HTTPException(400, '该服务没有可追加的相对路径，请填写完整的请求地址。')
    return validate_final_url(base + path), base, 'base_url'


def resolve_service(connection, purpose: str, adapter: str | None = None) -> MediaService:
    """Pick the adapter of a service and resolve everything it needs.

    The adapter is normally taken from the model's own purposes. The template
    declaration is enforced when a binding is *written*, not when it is
    called: an existing connection keeps working even if a later release
    narrows what its template declares.
    """
    if purpose not in MEDIA_PURPOSES:
        raise HTTPException(400, 'Unsupported media purpose')
    declaration = service_declaration(connection, purpose)
    config = service_config(connection, purpose)
    chosen = adapter or config.get('adapter') or (declaration.get('adapters') or [None])[0]
    if not chosen or chosen not in ADAPTERS.get(purpose, set()):
        raise HTTPException(400, 'Unsupported purpose or adapter')
    endpoint, base, kind = resolve_endpoint(connection, purpose, chosen)
    options = validate_options(connection, purpose, chosen, config.get('options'))
    return MediaService(
        purpose=purpose,
        adapter=chosen,
        endpoint=endpoint,
        options=options,
        limits=declaration.get('limits') or {},
        discovery=declaration.get('discovery') or 'remote',
        model_requirement=declaration.get('model_requirement') or 'required',
        base_url=base,
        endpoint_kind=kind,
    )


def enforce_audio_limits(service: MediaService, audio: bytes, duration=None, filename: str | None = None) -> None:
    """Refuse an audio input the adapter cannot accept."""
    limits = service.limits or {}
    max_bytes = limits.get('max_bytes')
    if isinstance(max_bytes, int) and len(audio) > max_bytes:
        raise HTTPException(400, f'音频超过 {max_bytes // (1024 * 1024)} MiB 的服务上限')
    max_seconds = limits.get('max_seconds')
    if isinstance(max_seconds, int) and isinstance(duration, (int, float)) and duration > max_seconds:
        raise HTTPException(400, f'音频超过 {max_seconds} 秒的服务上限，请改用录音任务分段转写')
    formats = limits.get('input_formats')
    if isinstance(formats, list) and formats and filename:
        suffix = str(filename).rsplit('.', 1)[-1].lower()
        if suffix not in formats:
            raise HTTPException(400, f'该服务不支持 {suffix} 格式的音频')


def enforce_text_limits(service: MediaService, text: str) -> None:
    limits = service.limits or {}
    max_chars = limits.get('max_text_chars')
    if isinstance(max_chars, int) and len(text) > max_chars:
        raise HTTPException(400, f'文本超过 {max_chars} 字的服务上限')


def enforce_image_limits(service: MediaService, payload: dict) -> None:
    limits = service.limits or {}
    prompt = payload.get('prompt') or ''
    max_prompt = limits.get('max_prompt_chars')
    if isinstance(max_prompt, int) and len(prompt) > max_prompt:
        raise HTTPException(400, f'提示词超过 {max_prompt} 字的服务上限')
    count = payload.get('n') or 1
    max_images = limits.get('max_images')
    if isinstance(max_images, int) and count > max_images:
        raise HTTPException(400, f'一次最多生成 {max_images} 张图片')
    width, height = payload.get('width'), payload.get('height')
    minimum = limits.get('min_dimension')
    maximum = limits.get('max_dimension')
    multiple = limits.get('dimension_multiple')
    for value in (width, height):
        if not isinstance(value, int):
            continue
        if isinstance(minimum, int) and value < minimum:
            raise HTTPException(400, f'图片尺寸不能小于 {minimum}')
        if isinstance(maximum, int) and value > maximum:
            raise HTTPException(400, f'图片尺寸不能大于 {maximum}')
        if isinstance(multiple, int) and value % multiple:
            raise HTTPException(400, f'图片尺寸必须是 {multiple} 的倍数')


def _media_error(detail: str, service: MediaService, *, reason: str = 'upstream_failure'):
    """Build an upstream media failure that keeps the error taxonomy."""
    error = HTTPException(502, detail)
    error.provider_category = 'upstream'
    error.provider_hint = detail
    error.provider_reason = reason
    error.provider_suggested_action = 'retry_later'
    error.provider_stage = STAGE_INFERENCE
    error.provider_purpose = service.purpose
    return error


def _upstream_error(status: int, raw: bytes, service: MediaService):
    """Turn a failed media call into an error that keeps the taxonomy.

    The media routers already know how to render an `HTTPException` with the
    provider attributes, so the failure travels in that shape rather than in a
    bare exception that would be flattened into a generic network error.
    """
    from open_webui.utils.providers import provider_http_error

    error = provider_http_error(status, stage=STAGE_INFERENCE)
    error.provider_purpose = service.purpose
    return error


def _business_error(failure: dict, service: MediaService):
    """Turn a MiniMax business failure into an error that keeps the taxonomy.

    The upstream answered HTTP 200, so the real upstream status is 200 and the
    business code is the honest signal. A failed key must not be reported as
    an upstream 401, and a quota problem must not be reported as a broken key.
    """
    reason, hint = provider_minimax.failure_reason(failure)
    if reason == 'invalid_credentials':
        category, action = 'authentication', 'replace_key'
    elif reason in ('quota_exhausted', 'rate_limited'):
        category, action = 'rate_limit', 'retry_later'
    elif reason == 'invalid_parameters':
        category, action = 'upstream', 'inspect_connection'
    else:
        category, action = 'upstream', 'retry_later'
    error = HTTPException(502, hint)
    error.provider_category = category
    error.provider_status = None
    error.provider_upstream_http_status = 200
    error.provider_upstream_code = str(failure.get('code') or '')
    error.provider_hint = hint
    error.provider_reason = reason
    error.provider_suggested_action = action
    error.provider_stage = STAGE_INFERENCE
    error.provider_purpose = service.purpose
    return error


async def _read_body(response, limit: int = MAX_MEDIA_RESPONSE_BYTES) -> bytes:
    from open_webui.utils.providers import read_bounded_response

    return await read_bounded_response(response, limit)


def _headers(credential: str, extra: dict | None = None) -> dict:
    headers = {'Authorization': f'Bearer {credential}'}
    headers.update(extra or {})
    return headers


async def _post(connection, service, *, json_body=None, form=None, headers=None):
    """Perform exactly one upstream media request."""
    from open_webui.utils.providers import media_http_session

    async with media_http_session(connection, target_url=service.endpoint) as session:
        async with session.post(
            service.endpoint,
            json=json_body,
            data=form,
            headers=headers or {},
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=MEDIA_TIMEOUT),
        ) as response:
            raw = await _read_body(response)
            return response.status, raw, dict(response.headers)


async def _transcription(connection, service, model, credential, payload) -> dict:
    audio = payload.get('audio')
    if not isinstance(audio, (bytes, bytearray)) or not audio:
        raise HTTPException(400, '缺少待转写的音频')
    filename = payload.get('filename') or 'audio.mp3'
    response_format = payload.get('response_format')
    if response_format not in (None, 'json', 'verbose_json'):
        raise HTTPException(400, '该适配器只支持 json 或 verbose_json 转写结果')
    enforce_audio_limits(service, bytes(audio), payload.get('duration'), filename)
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    form = aiohttp.FormData()
    headers = {}
    if service.adapter == 'minimax-transcription':
        for key, value in provider_minimax.transcription_fields(
            model=model, response_format=response_format
        ).items():
            form.add_field(key, value)
        headers = provider_minimax.transcription_headers(language=payload.get('language'))
    else:
        form.add_field('model', model)
        if response_format:
            form.add_field('response_format', response_format)
    form.add_field('file', bytes(audio), filename=filename, content_type=content_type)
    status, raw, _ = await _post(
        connection, service, form=form, headers=_headers(credential, headers)
    )
    if not 200 <= status < 300:
        raise _upstream_error(status, raw, service)
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise _media_error('Provider returned an invalid transcription', service) from None
    if service.adapter == 'minimax-transcription':
        failure = provider_minimax.business_failure(body)
        if failure:
            raise _business_error(failure, service)
        return provider_minimax.transcription_response(body)
    result = dict(body) if isinstance(body, dict) else {}
    if not isinstance(result.get('text'), str):
        raise _media_error('Provider did not return transcription text', service)
    return result


async def _speech(connection, service, model, credential, payload) -> dict:
    text = (payload.get('text') or '').strip()
    if not text:
        raise HTTPException(400, '缺少待合成的文本')
    enforce_text_limits(service, text)
    voice = payload.get('voice')
    if service.adapter == 'minimax-speech':
        if not voice:
            raise HTTPException(400, '该服务需要选择一个音色')
        body = provider_minimax.speech_payload(
            model=model,
            text=text,
            voice=voice,
            speed=payload.get('speed'),
            audio_format=service.options.get('format') or 'mp3',
        )
        status, raw, _ = await _post(
            connection, service, json_body=body, headers=_headers(credential)
        )
        if not 200 <= status < 300:
            raise _upstream_error(status, raw, service)
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise _media_error('Provider returned an invalid speech result', service) from None
        failure = provider_minimax.business_failure(parsed)
        if failure:
            raise _business_error(failure, service)
        try:
            audio = provider_minimax.speech_response(parsed)
        except ValueError as exc:
            raise _media_error(str(exc), service) from None
        return {'audio': audio, 'content_type': 'audio/mpeg'}
    body = {'model': model, 'input': text, 'response_format': 'mp3'}
    if voice:
        body['voice'] = voice
    status, raw, headers = await _post(
        connection, service, json_body=body, headers=_headers(credential)
    )
    if not 200 <= status < 300:
        raise _upstream_error(status, raw, service)
    content_type = headers.get('Content-Type', 'audio/mpeg')
    if not raw:
        raise _media_error('Provider did not return audio', service)
    if content_type and not content_type.startswith('audio/'):
        raise _media_error('Provider did not return audio', service)
    return {'audio': raw, 'content_type': content_type}


def _parse_openai_images(body: dict) -> list[dict]:
    images = []
    for item in body.get('data') or []:
        if not isinstance(item, dict):
            continue
        if item.get('b64_json'):
            images.append({'b64': item['b64_json'], 'url': None})
        elif item.get('url'):
            images.append({'b64': None, 'url': item['url']})
    return images


def _parse_siliconflow_images(body: dict) -> list[dict]:
    images = []
    for item in body.get('images') or []:
        if not isinstance(item, dict):
            continue
        if item.get('url'):
            images.append({'b64': None, 'url': item['url']})
    return images


async def _image(connection, service, model, credential, payload) -> dict:
    prompt = (payload.get('prompt') or '').strip()
    if not prompt:
        raise HTTPException(400, '缺少生图提示词')
    enforce_image_limits(service, payload)
    if service.adapter == 'minimax-image':
        body = provider_minimax.image_payload(
            model=model,
            prompt=prompt,
            width=payload.get('width'),
            height=payload.get('height'),
            aspect_ratio=payload.get('aspect_ratio'),
            count=payload.get('n') or 1,
            response_format=service.options.get('response_format') or 'base64',
        )
        status, raw, _ = await _post(
            connection, service, json_body=body, headers=_headers(credential)
        )
        if not 200 <= status < 300:
            raise _upstream_error(status, raw, service)
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise _media_error('Provider returned an invalid image result', service) from None
        failure = provider_minimax.business_failure(parsed)
        if failure:
            raise _business_error(failure, service)
        result = provider_minimax.image_response(parsed)
        result['partial'] = bool(result.get('failed_count'))
        return result
    body = {'model': model, 'prompt': prompt, 'n': payload.get('n') or 1}
    if service.adapter == 'siliconflow-image':
        # The SiliconFlow image endpoint takes its own fields; it has no `n`.
        body.pop('n')
        size = payload.get('size')
        if size:
            body['image_size'] = size
    else:
        size = payload.get('size')
        if size:
            body['size'] = size
        response_format = service.options.get('response_format')
        if response_format:
            body['response_format'] = response_format
    status, raw, _ = await _post(connection, service, json_body=body, headers=_headers(credential))
    if not 200 <= status < 300:
        raise _upstream_error(status, raw, service)
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise _media_error('Provider returned an invalid image result', service) from None
    images = (
        _parse_siliconflow_images(parsed)
        if service.adapter == 'siliconflow-image'
        else _parse_openai_images(parsed)
    )
    if not images:
        raise _media_error('Provider did not return images', service)
    return {'images': images, 'partial': False, 'failed_count': None, 'success_count': None}


async def call_media_service(connection, service: MediaService, *, model, credential, payload) -> dict:
    """Perform exactly one media call and return the normalized result.

    No caller may retry here: an interrupted or timed-out call may already
    have been charged.
    """
    if service.purpose == PURPOSE_TRANSCRIPTION:
        return await _transcription(connection, service, model, credential, payload)
    if service.purpose == PURPOSE_SPEECH:
        return await _speech(connection, service, model, credential, payload)
    if service.purpose == PURPOSE_IMAGE:
        return await _image(connection, service, model, credential, payload)
    raise HTTPException(400, 'Unsupported media purpose')


async def fetch_voices(connection, service: MediaService, credential: str, voice_type: str = 'system'):
    """List the voices of a speech service.

    Only adapters that can answer the question are allowed to. The call is a
    read-only metadata request: it never activates a voice and never runs an
    inference.
    """
    if service.adapter != 'minimax-speech':
        raise HTTPException(400, '该适配器不支持音色查询，请手工填写音色 ID。')
    if service.endpoint_kind != 'base_url' or not service.base_url:
        raise HTTPException(
            400,
            '该服务使用了完整请求地址，无法推导音色查询地址；请改用基础地址模式或手工填写音色 ID。',
        )
    body = provider_minimax.voice_payload(voice_type=voice_type)
    voice_service = MediaService(
        purpose=service.purpose,
        adapter=service.adapter,
        endpoint=service.base_url.rstrip('/') + provider_minimax.VOICE_PATH,
        options=service.options,
        limits=service.limits,
        discovery=service.discovery,
        model_requirement=service.model_requirement,
        base_url=service.base_url,
        endpoint_kind=service.base_url,
    )
    status, raw, _ = await _post(connection, voice_service, json_body=body, headers=_headers(credential))
    if not 200 <= status < 300:
        raise _upstream_error(status, raw, voice_service)
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise _media_error('Provider returned an invalid voice list', service) from None
    failure = provider_minimax.business_failure(parsed)
    if failure:
        raise _business_error(failure, voice_service)
    return provider_minimax.voice_response(parsed)
