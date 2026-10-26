"""Observe actual upstream dispatches without retaining request/response content."""

import asyncio
import json
import logging
import time
from functools import wraps

from open_webui.models.provider_calls import reported_usage, save_call
from starlette.responses import StreamingResponse

log = logging.getLogger(__name__)


def record_invocation(function):
    @wraps(function)
    async def observed(request, form_data, user, *args, **kwargs):
        # Arena wrappers select a concrete model recursively; only that dispatch
        # is a receipt. Browser-direct requests cannot be measured by this server.
        from open_webui.utils.models import get_request_models, load_provider_models

        model_id = form_data.get('model', '')
        model = get_request_models(request).get(model_id, {})
        if not model:
            await load_provider_models(request, user)
            model = get_request_models(request).get(model_id, {})
        if model.get('owned_by') == 'arena' or getattr(request.state, 'direct', False):
            return await function(request, form_data, user, *args, **kwargs)
        metadata = {**getattr(request.state, 'metadata', {}), **form_data.get('metadata', {})}
        task = metadata.get('task')
        purpose = (
            str(task)
            if task
            else ('tool_cycle' if (form_data.get('messages') or [{}])[-1].get('role') == 'tool' else 'chat')
        )
        started = time.monotonic()
        record = dict(
            user_id=user.id,
            connection_id=model.get('connection_id'),
            model_id=model_id,
            chat_id=metadata.get('chat_id'),
            message_id=metadata.get('message_id'),
            purpose=purpose,
            created_at=int(time.time()),
            usage=None,
        )

        async def finish(status):
            try:
                await save_call(**record, status=status, elapsed_ms=int((time.monotonic() - started) * 1000))
            except Exception:
                log.warning('Could not persist invocation receipt')

        try:
            response = await function(request, form_data, user, *args, **kwargs)
        except asyncio.CancelledError:
            await asyncio.shield(finish('cancelled'))
            raise
        except Exception:
            await finish('failed')
            raise
        if isinstance(response, StreamingResponse):
            iterator = response.body_iterator

            async def stream():
                pending = b''
                status = 'incomplete'
                try:
                    async for chunk in iterator:
                        raw = chunk.encode() if isinstance(chunk, str) else chunk
                        pending += raw
                        while b'\n' in pending:
                            line, pending = pending.split(b'\n', 1)
                            if not line.startswith(b'data:'):
                                continue
                            value = line[5:].strip()
                            if value == b'[DONE]':
                                if status != 'failed':
                                    status = 'completed'
                                continue
                            try:
                                event = json.loads(value)
                                usage = reported_usage(event.get('usage') or (event.get('response') or {}).get('usage'))
                                if usage:
                                    record['usage'] = usage
                                if event.get('error') or event.get('type') == 'response.failed':
                                    status = 'failed'
                                elif event.get('type') == 'response.completed' or any(
                                    c.get('finish_reason') for c in event.get('choices', [])
                                ):
                                    status = 'completed'
                            except (ValueError, AttributeError, TypeError):
                                pass
                        if len(pending) > 2_000_000:
                            pending = b''
                        yield chunk
                except (asyncio.CancelledError, GeneratorExit):
                    status = 'cancelled'
                    raise
                except Exception:
                    status = 'failed'
                    raise
                finally:
                    if hasattr(iterator, 'aclose'):
                        await iterator.aclose()
                    await asyncio.shield(finish(status))

            response.body_iterator = stream()
        else:
            body = response if isinstance(response, dict) else None
            if body is None and hasattr(response, 'body'):
                try:
                    body = json.loads(response.body)
                except (ValueError, TypeError):
                    pass
            if isinstance(body, dict):
                record['usage'] = reported_usage(body.get('usage'))
            await finish(
                'failed'
                if getattr(response, 'status_code', 200) >= 400 or isinstance(body, dict) and body.get('error')
                else 'completed'
            )
        return response

    return observed
