"""Model-test stream never fabricates output or retries an interrupted inference."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from open_webui.routers import providers


def frames(*values):
    async def generate():
        for value in values:
            await asyncio.sleep(0.001)
            yield b'data: ' + json.dumps(value).encode() + b'\n\n' if isinstance(value, dict) else value

    return SimpleNamespace(body_iterator=generate())


class ProviderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, upstream):
        connection = SimpleNamespace(auth_type='api_key')
        resolved = SimpleNamespace(model_metadata={'uses': {'chat': 'openai-chat'}})
        form = providers.ModelTest(upstream_id='model', purpose='chat')
        save = AsyncMock(
            side_effect=lambda c, m, b, p, o, e, st, u, db, usage: {'status': o, 'elapsed_ms': 10, 'error': e}
        )

        async def start(*args, **kwargs):
            kwargs['on_request_started']()
            return upstream

        with (
            patch.object(providers, '_connection_with_manage_access', new=AsyncMock(return_value=connection)),
            patch('open_webui.utils.providers.resolve_model', new=AsyncMock(return_value=resolved)),
            patch('open_webui.utils.provider_capabilities.fingerprint', return_value='fingerprint'),
            patch('open_webui.utils.providers.provider_chat_completion', new=AsyncMock(side_effect=start)) as call,
            patch.object(providers, '_save_model_test', new=save),
        ):
            response = await providers.test_model_stream('connection', form, object(), object())
            result = []
            async for frame in response.body_iterator:
                result.append(json.loads(frame.decode()[6:]))
        return result, call, save

    async def test_first_text_and_completion_are_observed_once(self):
        result, call, save = await self.run_case(
            frames(
                {'choices': [{'delta': {'role': 'assistant'}}]},
                {'choices': [{'delta': {'content': 'OK'}}]},
                b'data: [DONE]\n\n',
            )
        )
        self.assertEqual(
            [e['phase'] for e in result],
            ['accepted', 'request_started', 'response_headers', 'output_delta', 'completed'],
        )
        self.assertEqual(result[-1]['data']['status'], 'passed')
        self.assertIsNotNone(result[-1]['data']['timings']['first_output_ms'])
        self.assertGreaterEqual(
            result[-1]['data']['timings']['first_output_ms'], result[-1]['data']['timings']['upstream_headers_ms']
        )
        call.assert_awaited_once()
        save.assert_awaited_once()

    async def test_incomplete_stream_is_not_saved_as_passed(self):
        result, call, save = await self.run_case(frames({'choices': [{'delta': {'content': 'partial'}}]}))
        self.assertEqual(result[-1]['phase'], 'failed')
        self.assertEqual(result[-1]['data']['status'], 'failed')
        call.assert_awaited_once()
        save.assert_awaited_once()

    async def test_reasoning_is_not_answer_text(self):
        result, _, _ = await self.run_case(
            frames(
                {'type': 'response.reasoning_summary_text.delta', 'delta': 'secret'},
                {'type': 'response.completed', 'response': {}},
            )
        )
        self.assertEqual(result[-1]['phase'], 'failed')
        self.assertNotIn('secret', repr(result))
        self.assertIsNone(result[-1]['data']['timings']['first_output_ms'])

    async def test_closing_subscription_cancels_request_without_saving_success(self):
        connection = SimpleNamespace(auth_type='api_key')
        resolved = SimpleNamespace(model_metadata={'uses': {'chat': 'openai-chat'}})
        form = providers.ModelTest(upstream_id='model', purpose='chat')
        cancelled = asyncio.Event()

        async def pending(*args, **kwargs):
            kwargs['on_request_started']()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch.object(providers, '_connection_with_manage_access', new=AsyncMock(return_value=connection)),
            patch('open_webui.utils.providers.resolve_model', new=AsyncMock(return_value=resolved)),
            patch('open_webui.utils.provider_capabilities.fingerprint', return_value='fingerprint'),
            patch('open_webui.utils.providers.provider_chat_completion', new=AsyncMock(side_effect=pending)) as call,
            patch.object(providers, '_save_model_test', new=AsyncMock()) as save,
        ):
            response = await providers.test_model_stream('connection', form, object(), object())
            self.assertEqual(json.loads((await anext(response.body_iterator)).decode()[6:])['phase'], 'accepted')
            self.assertEqual(json.loads((await anext(response.body_iterator)).decode()[6:])['phase'], 'request_started')
            await response.body_iterator.aclose()
            self.assertTrue(cancelled.is_set())
            call.assert_awaited_once()
            save.assert_not_awaited()
