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


class ChunkedContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_total_response_limit_applies_across_chunks(self):
        response = SimpleNamespace(content=ChunkedContent([b'ab', b'cd']))
        with self.assertRaises(HTTPException):
            await providers.read_bounded_response(response, 3)
