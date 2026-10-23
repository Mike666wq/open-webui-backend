"""Chunked Responses transport must read the terminal event before reporting success."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
from open_webui.internal.db import get_async_db_context
from sqlalchemy import delete

from fastapi import HTTPException
from open_webui.utils import chatgpt_auth, providers
from open_webui.routers import provider_auth


class ChunkedContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def streaming(self, chunks, consume, *, provider_id='chatgpt', status=200):
        response = SimpleNamespace(status=status, headers={}, content=ChunkedContent(chunks), release=Mock())
        session = SimpleNamespace(close=AsyncMock(), post=AsyncMock(return_value=response))
        connection = SimpleNamespace(provider_id=provider_id, config={})
        resolved = SimpleNamespace(connection=connection, upstream_model_id='gpt-test')
        with (
            patch.object(providers, 'resolve_model', new=AsyncMock(return_value=resolved)),
            patch.object(chatgpt_auth, 'chatgpt_chat_completion', new=AsyncMock(return_value=(response, session))),
            patch.object(providers, '_authorization', new=AsyncMock(return_value='synthetic')),
            patch.object(providers, 'provider_base_url', return_value='https://example.invalid/v1'),
            patch.object(providers, 'provider_request_headers', return_value={}),
            patch.object(providers, 'validate_app_url', return_value=False),
            patch.object(providers, 'AppTCPConnector'),
            patch.object(providers.aiohttp, 'ClientSession', return_value=session),
        ):
            try:
                result = await providers.provider_chat_completion(
                    'provider:test:gpt-test',
                    {'messages': [{'role': 'user', 'content': 'Hello'}], 'stream': True},
                    SimpleNamespace(id='owner'),
                )
                return await consume(result)
            finally:
                response.release.assert_called_once()
                session.close.assert_awaited_once()

    @staticmethod
    def encode_events(events):
        return b''.join(
            ('event: ' + event['type'] + '\r\ndata: ' + json.dumps(event, ensure_ascii=False) + '\r\n\r\n').encode()
            for event in events
        )

    async def render_chat(self, response):
        # Exercise the actual UI/socket consumer, rather than just asserting that
        # the bytes reached an HTTP client (which misses framing regressions).
        from open_webui.main import app
        from open_webui.utils import middleware
        from starlette.requests import Request

        emitted = AsyncMock()
        ctx = {
            'request': Request({'type': 'http', 'app': app, 'headers': [], 'state': {}}),
            'user': SimpleNamespace(id='owner', role='admin'),
            'model': {'id': 'provider:test:gpt-test'},
            'form_data': {'model': 'provider:test:gpt-test', 'messages': [{'role': 'user', 'content': 'Hello'}]},
            'metadata': {'chat_id': 'local:framing', 'message_id': 'reply', 'params': {}},
            'events': [],
            'event_emitter': emitted,
            'event_caller': None,
        }
        with (
            patch.object(middleware, 'get_system_oauth_token', new=AsyncMock(return_value=None)),
            patch.object(middleware, 'get_filter_functions', new=AsyncMock(return_value=[])),
            patch.object(middleware, 'outlet_filter_handler', new=AsyncMock()),
            patch.object(middleware, 'background_tasks_handler', new=AsyncMock()),
        ):
            await middleware.process_chat_response(response, ctx)
        return [call.args[0] for call in emitted.await_args_list]

    async def test_chatgpt_coalesced_and_split_events_render_in_chat(self):
        events = [
            {
                'type': 'response.output_item.added',
                'output_index': 0,
                'item': {
                    'id': 'msg-test',
                    'type': 'message',
                    'role': 'assistant',
                    'content': [],
                },
            },
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '你好🌏'},
            {'type': 'response.completed', 'response': {'id': 'resp-test', 'output': []}},
        ]
        wire = self.encode_events(events)
        split = wire.index('好'.encode()) + 1
        for chunks in ([wire], [wire[:split], wire[split : split + 1], wire[split + 1 :]]):
            with self.subTest(chunks=len(chunks)):
                emitted = await self.streaming(chunks, self.render_chat)
                final = next(e['data'] for e in reversed(emitted) if e['data'].get('done'))
                self.assertEqual(final['output'][0]['content'][0]['text'], '你好🌏')
                self.assertTrue(any(e['type'] == 'response:completion' for e in emitted))

    async def test_stream_failure_and_truncation_reach_chat_as_errors(self):
        for events, code in (
            (
                [
                    {
                        'type': 'response.failed',
                        'response': {
                            'error': {
                                'code': 'subscription_sharing_usage_limit_exceeded',
                                'message': 'Plan limit reached',
                            }
                        },
                    }
                ],
                'subscription_sharing_usage_limit_exceeded',
            ),
            ([{'type': 'response.output_text.delta', 'delta': 'Partial'}], 'incomplete_stream'),
            (
                [{'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'max_output_tokens'}}}],
                'incomplete_response',
            ),
        ):
            with self.subTest(code=code):
                emitted = await self.streaming([self.encode_events(events)], self.render_chat)
                errors = [e['data']['error'] for e in emitted if e['data'].get('error')]
                self.assertTrue(errors, emitted)
                self.assertIn(code, json.dumps(errors))

    async def test_api_key_coalesced_events_remain_separate(self):
        async def consume(response):
            return [chunk async for chunk in response.body_iterator]

        wire = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
        frames = await self.streaming([wire], consume, provider_id='custom')
        self.assertEqual(len(frames), 2)
        self.assertEqual(json.loads(frames[0][5:])['choices'][0]['delta']['content'], 'hello')
        self.assertEqual(frames[1].strip(), b'data: [DONE]')

    async def test_non_2xx_releases_response_without_awaiting_sync_release(self):
        with self.assertRaises(HTTPException) as caught:
            await self.streaming([], AsyncMock(), provider_id='custom', status=429)
        self.assertEqual(caught.exception.status_code, 429)

    async def test_chatgpt_http_errors_close_session_and_preserve_status(self):
        for status in (401, 402, 429, 500):
            with self.subTest(status=status):
                response = SimpleNamespace(status=status, release=Mock())
                session = SimpleNamespace(post=AsyncMock(return_value=response), close=AsyncMock())
                with (
                    patch.object(chatgpt_auth, 'get_provider_access_token', new=AsyncMock(return_value='synthetic')),
                    patch.object(chatgpt_auth.aiohttp, 'ClientSession', return_value=session),
                ):
                    with self.assertRaises(HTTPException) as caught:
                        await chatgpt_auth.chatgpt_chat_completion(None, 'gpt-test', {'messages': []}, None)
                self.assertEqual(caught.exception.status_code, status if status != 500 else 502)
                response.release.assert_called_once()
                session.close.assert_awaited_once()

    async def test_invalid_oversized_and_interrupted_streams_are_visible_errors(self):
        from open_webui.utils.provider_stream import MAX_EVENT_BYTES

        for wire in (b'data: not-json\n\n', b'data: {"type":', b'data: ' + b'x' * MAX_EVENT_BYTES):
            with self.subTest(size=len(wire)):
                emitted = await self.streaming([wire], self.render_chat)
                self.assertTrue(any(e['data'].get('error') for e in emitted))

        async def broken_content(size):
            yield b': heartbeat\n\n'
            raise providers.aiohttp.ClientPayloadError('synthetic interrupted connection')

        with patch.object(ChunkedContent, 'iter_chunked', side_effect=broken_content):
            emitted = await self.streaming([], self.render_chat)
        self.assertTrue(any(e['data'].get('error', {}).get('code') == 'connection_interrupted' for e in emitted))

    async def test_multiline_data_heartbeats_and_tool_events_survive_single_byte_chunks(self):
        async def consume(response):
            return [json.loads(chunk[5:]) async for chunk in response.body_iterator]

        wire = (
            b': heartbeat\n\nevent: response.function_call_arguments.delta\n'
            b'data: {"type":"response.function_call_arguments.delta",\n'
            b'data: "output_index":0,"delta":"{\\"city\\":\\"Paris\\"}"}\n\n'
            b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
        )
        events = await self.streaming([wire[i : i + 1] for i in range(len(wire))], consume)
        self.assertEqual(events[0]['delta'], '{"city":"Paris"}')
        self.assertEqual(events[1]['type'], 'response.completed')

    async def test_closing_stream_releases_upstream_connection(self):
        async def consume(response):
            await anext(response.body_iterator)
            await response.body_iterator.aclose()

        await self.streaming(
            [self.encode_events([{'type': 'response.output_text.delta', 'delta': 'partial'}])], consume
        )

    async def completion(self, events):
        response = SimpleNamespace(
            status=200,
            headers={},
            content=ChunkedContent([('data: ' + json.dumps(event) + '\n\n').encode() for event in events]),
            release=Mock(),
        )
        session = SimpleNamespace(close=AsyncMock())
        connection = SimpleNamespace(provider_id='chatgpt', config={})
        resolved = SimpleNamespace(connection=connection, upstream_model_id='gpt-test')
        with (
            patch.object(providers, 'resolve_model', new=AsyncMock(return_value=resolved)),
            patch.object(chatgpt_auth, 'chatgpt_chat_completion', new=AsyncMock(return_value=(response, session))),
        ):
            try:
                return await providers.provider_chat_completion(
                    'provider:test:gpt-test', {'messages': [], 'stream': False}, SimpleNamespace(id='owner')
                )
            finally:
                response.release.assert_called_once()
                session.close.assert_awaited_once()

    async def test_nonstream_waits_for_completed_event_in_later_chunk(self):
        result = await self.completion(
            [
                {'type': 'response.output_text.delta', 'delta': 'Hello'},
                {
                    'type': 'response.completed',
                    'response': {
                        'id': 'resp-test',
                        'model': 'gpt-test',
                        'output': [
                            {
                                'type': 'message',
                                'role': 'assistant',
                                'content': [{'type': 'output_text', 'text': 'Hello'}],
                            }
                        ],
                    },
                },
            ]
        )
        self.assertEqual(result['choices'][0]['message']['content'], 'Hello')
        self.assertEqual(result['model'], 'provider:test:gpt-test')

    async def test_late_failure_after_text_delta_is_not_success(self):
        with self.assertRaises(HTTPException) as caught:
            await self.completion(
                [
                    {'type': 'response.output_text.delta', 'delta': 'Partial'},
                    {'type': 'response.failed', 'response': {'error': {'code': 'usage_limit_reached'}}},
                ]
            )
        self.assertEqual(caught.exception.status_code, 502)

    async def test_truncated_stream_is_not_success(self):
        with self.assertRaises(HTTPException):
            await self.completion([{'type': 'response.output_text.delta', 'delta': 'Partial'}])

    async def test_new_authorization_keeps_independent_registrations(self):
        owner = SimpleNamespace(id='registration-test-' + str(uuid4()))
        async with get_async_db_context() as db:
            first = await providers.create_provider_connection(
                owner, {'client_id': 'oaiapp_first'}, db=db, credentials={'access_token': 'first'}
            )
            second = await providers.create_provider_connection(
                owner, {'client_id': 'oaiapp_second'}, db=db, credentials={'access_token': 'second'}
            )
            self.assertNotEqual(first.id, second.id)
            self.assertEqual(first.config['client_id'], 'oaiapp_first')
            self.assertEqual(second.config['client_id'], 'oaiapp_second')
            await db.execute(
                delete(providers.ProviderConnection).where(providers.ProviderConnection.owner_id == owner.id)
            )
            await db.commit()

    async def test_disconnect_revokes_latest_rotated_token_not_stale_session(self):
        owner = SimpleNamespace(id='disconnect-test-' + str(uuid4()))
        async with get_async_db_context() as db:
            connection = await providers.create_provider_connection(
                owner,
                {'client_id': 'oaiapp_test'},
                db=db,
                credentials={'client_id': 'oaiapp_test', 'refresh_token': 'old-token'},
            )
            await db.commit()
            async with get_async_db_context() as other:
                latest = await other.get(providers.ProviderConnection, connection.id)
                latest.credential = providers.encrypt_credential(
                    json.dumps({'client_id': 'oaiapp_test', 'refresh_token': 'new-token'})
                )
                await other.commit()
            with patch.object(provider_auth, 'revoke_chatgpt_session', new=AsyncMock(return_value=True)) as revoke:
                result = await provider_auth.disconnect_chatgpt(connection.id, owner, db)
            self.assertTrue(result['revocation_confirmed'])
            self.assertEqual(revoke.await_args.args[0]['refresh_token'], 'new-token')
            self.assertIsNone(connection.credential)
            await db.execute(
                delete(providers.ProviderConnection).where(providers.ProviderConnection.id == connection.id)
            )
            await db.commit()

    async def test_total_response_limit_applies_across_chunks(self):
        response = SimpleNamespace(content=ChunkedContent([b'ab', b'cd']))
        with self.assertRaises(HTTPException):
            await providers.read_bounded_response(response, 3)
