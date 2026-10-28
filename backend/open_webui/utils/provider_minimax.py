"""MiniMax native protocol adapters.

MiniMax does not expose an OpenAI-compatible media surface. Transcription,
speech synthesis and image generation each have their own path, their own
request body and their own response envelope. Keeping that protocol in one
module means the daily features and the explicit model test cannot drift
apart, and that a new caller cannot invent a fourth shape.

Only the synchronous, non-streaming subset is implemented. Native speech
streaming, real-time microphone input, voice cloning, asynchronous long text
synthesis and image editing are deliberately out of scope.
"""

TRANSCRIPTION_PATH = '/speech_to_text'
SPEECH_PATH = '/t2a_v2'
IMAGE_PATH = '/image_generation'
VOICE_PATH = '/get_voice'

# The transcription model is fixed by the vendor today. It still travels with
# every request: a defaulted parameter is not an absent parameter.
DEFAULT_ASR_MODEL = 'asr-1.0'

MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_AUDIO_SECONDS = 500

SUPPORTED_AUDIO_FORMATS = ('wav', 'aiff', 'flac', 'm4a', 'mp3', 'aac', 'opus', 'ogg')

# The header that carries the recognition language. It is a header, not a form
# field: a form field of the same name is ignored by the vendor.
LANGUAGE_HEADER = 'Language'

# Business codes documented for the MiniMax media endpoints. The vendor answers
# HTTP 200 with a non-zero `base_resp.status_code` when it refuses a request,
# so the code is the only honest signal about what went wrong.
ERROR_CODES = {
    '1004': ('invalid_credentials', '厂商拒绝了当前凭据，请重新填写 API Key。'),
    '2049': ('invalid_credentials', '厂商拒绝了当前凭据，请重新填写 API Key。'),
    '1008': ('quota_exhausted', '厂商额度或余额不足，请检查账户套餐与余额。'),
    '1002': ('rate_limited', '厂商限流，请等待片刻后重试。'),
    '2013': ('invalid_parameters', '厂商拒绝了这次请求的参数，请检查模型、尺寸与内容。'),
}


def business_failure(payload):
    """Return the business failure of a MiniMax response, if there is one.

    A non-zero `base_resp.status_code` means the request was refused even
    though the HTTP status was 200. Treating it as a success would write a
    provider error into an audio file.
    """
    if not isinstance(payload, dict):
        return None
    base = payload.get('base_resp')
    if not isinstance(base, dict):
        return None
    code = base.get('status_code')
    if code in (0, '0', None):
        return None
    return {
        'code': str(code),
        'message': base.get('status_msg') or '厂商拒绝了这次请求。',
    }


def failure_reason(failure) -> tuple[str, str]:
    """Map a MiniMax business code onto the provider error taxonomy."""
    code = str((failure or {}).get('code'))
    return ERROR_CODES.get(
        code,
        ('unknown', '厂商拒绝了这次请求，请检查请求参数、账户状态与服务区域。'),
    )


def transcription_fields(*, model: str | None = None, response_format: str | None = None) -> dict:
    """Build the multipart fields of a transcription request."""
    fields = {'model': model or DEFAULT_ASR_MODEL}
    if response_format in ('json', 'verbose_json'):
        fields['response_format'] = response_format
    return fields


def transcription_headers(*, language: str | None = None) -> dict:
    """Build the request headers of a transcription request.

    The language travels in a header. Omitting it means the vendor decides,
    which is the mixed-language behaviour the operator asked for; the adapter
    never probes several languages in turn.
    """
    headers = {}
    if language:
        headers[LANGUAGE_HEADER] = language
    return headers


def transcription_response(payload) -> dict:
    """Normalize a MiniMax transcription response."""
    if not isinstance(payload, dict):
        raise ValueError('厂商返回的转写结果不是合法对象')
    result = dict(payload)
    text = payload.get('text')
    result['text'] = text if isinstance(text, str) else ''
    duration = payload.get('duration')
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        result['duration'] = float(duration)
    segments = payload.get('segments')
    if isinstance(segments, list):
        result['segments'] = [dict(item) for item in segments if isinstance(item, dict)]
    return result


def speech_payload(
    *,
    model: str,
    text: str,
    voice: str | None = None,
    speed: float | None = None,
    audio_format: str = 'mp3',
) -> dict:
    """Build the body of a synchronous MiniMax speech request."""
    payload = {
        'model': model,
        'text': text,
        'output_format': 'hex',
        'stream': False,
        'audio_setting': {
            'format': audio_format,
        },
    }
    voice_setting = {}
    if voice:
        voice_setting['voice_id'] = voice
    if speed is not None:
        voice_setting['speed'] = speed
    if voice_setting:
        payload['voice_setting'] = voice_setting
    return payload


def speech_response(payload) -> bytes:
    """Return the decoded MP3 bytes of a synchronous MiniMax synthesis.

    The vendor returns the audio as a hexadecimal string. A JSON error, an
    empty body or a payload that is not valid hexadecimal must never end up
    in an audio file.
    """
    if not isinstance(payload, dict):
        raise ValueError('厂商返回的合成结果不是合法对象')
    data = payload.get('data')
    audio = data.get('audio') if isinstance(data, dict) else None
    if not isinstance(audio, str) or not audio.strip():
        raise ValueError('厂商没有返回音频内容')
    try:
        raw = bytes.fromhex(audio.strip())
    except ValueError:
        raise ValueError('厂商返回的音频内容不是合法的十六进制数据') from None
    if not raw:
        raise ValueError('厂商返回的音频内容为空')
    return raw


def image_payload(
    *,
    model: str,
    prompt: str,
    width: int | None = None,
    height: int | None = None,
    aspect_ratio: str | None = None,
    count: int = 1,
    response_format: str = 'base64',
) -> dict:
    """Build the body of a MiniMax text-to-image request.

    The OpenAI `size` string is not a MiniMax field. A dimension is sent as
    `width`/`height`; when an aspect ratio is given it wins, because the
    vendor documents it as taking precedence.
    """
    payload = {
        'model': model,
        'prompt': prompt,
        'n': count,
        'response_format': response_format,
    }
    if aspect_ratio:
        payload['aspect_ratio'] = aspect_ratio
    elif width and height:
        payload['width'] = width
        payload['height'] = height
    return payload


def image_response(payload) -> dict:
    """Return the images of a MiniMax generation and whether it was partial."""
    if not isinstance(payload, dict):
        raise ValueError('厂商返回的生图结果不是合法对象')
    data = payload.get('data')
    images = []
    urls = data.get('image_urls') if isinstance(data, dict) else None
    for url in urls or []:
        if isinstance(url, str) and url:
            images.append({'url': url, 'b64': None})
    encoded = data.get('image_base64') if isinstance(data, dict) else None
    for item in encoded or []:
        if isinstance(item, str) and item:
            images.append({'url': None, 'b64': item})
    metadata = payload.get('metadata') if isinstance(payload, dict) else None
    failed = metadata.get('failed_count') if isinstance(metadata, dict) else None
    success = metadata.get('success_count') if isinstance(metadata, dict) else None
    return {
        'images': images,
        'failed_count': failed if isinstance(failed, int) else None,
        'success_count': success if isinstance(success, int) else None,
    }


def voice_payload(*, voice_type: str = 'system') -> dict:
    """Build the body of a MiniMax voice listing request."""
    return {'voice_type': voice_type}


def voice_response(payload) -> list[dict]:
    """Normalize a MiniMax voice listing response."""
    data = payload.get('data') if isinstance(payload, dict) else None
    voices = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        voice_id = item.get('voice_id') or item.get('id')
        if not voice_id:
            continue
        voices.append(
            {
                'id': str(voice_id),
                'name': item.get('name') or item.get('description') or str(voice_id),
                'source': item.get('type') or 'system',
            }
        )
    return voices
