"""Frame provider SSE before handing it to Open WebUI's event consumer."""

import json
import logging

import aiohttp

log = logging.getLogger(__name__)
MAX_EVENT_BYTES = 2_000_000


async def sse_data(content):
    """Assemble complete SSE data fields without decoding partial UTF-8 chunks.

    The chat middleware consumes one JSON event per iterator item, whereas an
    aiohttp chunk can contain several events or just part of one. Bound both
    unfinished lines and multiline events, including ignored SSE fields.
    """
    buffer = bytearray()
    fields = []
    event_size = 0
    async for chunk in content.iter_chunked(16384):
        buffer.extend(chunk)
        while b'\n' in buffer:
            end = buffer.index(b'\n')
            line = bytes(buffer[:end]).removesuffix(b'\r')
            del buffer[: end + 1]
            event_size += end + 1
            if event_size > MAX_EVENT_BYTES:
                raise ValueError('oversized_event')
            if not line:
                if fields:
                    yield b'\n'.join(fields)
                fields = []
                event_size = 0
            elif line.startswith(b'data:'):
                value = line[5:]
                fields.append(value[1:] if value.startswith(b' ') else value)
        if event_size + len(buffer) > MAX_EVENT_BYTES:
            raise ValueError('oversized_event')
    if buffer or fields:
        # An unfinished event is not proof of completion, even if it contains
        # a syntactically valid JSON prefix.
        raise ValueError('incomplete_stream')


def error_frame(code, message):
    return encode_event({'error': {'code': code, 'message': message}})


def encode_event(event):
    return b'data: ' + json.dumps(event, ensure_ascii=False).encode() + b'\n\n'


async def provider_stream(response, session, *, require_completed=False):
    """Yield independent events and surface failures after HTTP headers arrive."""
    completed = False
    try:
        async for value in sse_data(response.content):
            if value.strip() == b'[DONE]':
                if require_completed and not completed:
                    break
                yield b'data: [DONE]\n\n'
                return
            event = json.loads(value)
            if not isinstance(event, dict):
                raise ValueError('invalid_event')
            event_type = event.get('type')
            if event_type == 'response.incomplete':
                log.warning('Provider stream failed: incomplete_response')
                yield error_frame('incomplete_response', 'Provider returned an incomplete response. Please retry.')
                return
            if event_type == 'response.completed':
                if not isinstance(event.get('response'), dict):
                    raise ValueError('invalid_event')
                completed = True
            failed = bool(event.get('error')) or event_type in ('response.failed', 'error')
            if failed:
                # Do not log upstream messages, prompts or credentials.
                log.warning('Provider stream returned a failure event')
                if event_type == 'response.failed' and not (event.get('response') or {}).get('error'):
                    yield error_frame('provider_error', 'Provider could not complete the request.')
                    return
            yield encode_event(event)
            if failed or completed:
                return
        if require_completed and not completed:
            log.warning('Provider stream failed: incomplete_stream')
            yield error_frame('incomplete_stream', 'Provider stream ended before completion. Please retry.')
    except (aiohttp.ClientError, TimeoutError):
        log.warning('Provider stream failed: connection_interrupted')
        yield error_frame('connection_interrupted', 'Provider connection was interrupted. Please retry.')
    except (ValueError, UnicodeDecodeError):
        log.warning('Provider stream failed: invalid_stream')
        yield error_frame('invalid_stream', 'Provider returned an invalid or unfinished stream. Please retry.')
    finally:
        response.release()
        await session.close()
