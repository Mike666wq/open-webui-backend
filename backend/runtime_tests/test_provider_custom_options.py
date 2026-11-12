"""Tests for custom provider options, custom headers, and test timings persistence."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import (
    ProviderConnection,
    ProviderModel,
    new_provider_connection,
)
from open_webui.routers import providers as api
from open_webui.utils import provider_registry, providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from sqlalchemy import delete


class ProviderCustomOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = SimpleNamespace(id='custom-owner', role='admin')
        self.connection = new_provider_connection(
            provider_id='custom',
            name='My Custom Provider',
            auth_type='api_key',
            owner_id=self.user.id,
            visibility='private',
            config={
                '_registry_version': 2,
                'base_url': 'https://custom-gateway.example.com/v1',
                'custom_headers': {
                    'HTTP-Referer': 'https://myapp.example.com',
                    'X-Title': 'MyApp',
                },
            },
            credential=providers.encrypt_credential('custom-sk-key'),
            status='connected',
            models=[],
            is_active=True,
            protocol='openai-compatible',
            provider_template='custom',
        )
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.owner_id == self.user.id))
            await db.execute(delete(ProviderModel).where(ProviderModel.connection_id == self.connection.id))
            db.add(self.connection)
            await db.commit()
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/providers')
        self.app.dependency_overrides[get_verified_user] = lambda: self.user
        self.app.dependency_overrides[get_admin_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)

    def test_custom_headers_in_provider_request_headers(self):
        headers = providers.provider_request_headers(self.connection, 'custom-sk-key')
        self.assertEqual(headers.get('Authorization'), 'Bearer custom-sk-key')
        self.assertEqual(headers.get('HTTP-Referer'), 'https://myapp.example.com')
        self.assertEqual(headers.get('X-Title'), 'MyApp')

    def test_custom_provider_template_supports_all_services(self):
        tpl = provider_registry.get_template('custom')
        self.assertTrue(tpl.get('supported'))
        services = tpl.get('services', {})
        self.assertIn('chat', services)
        self.assertIn('transcription', services)
        self.assertIn('speech', services)
        self.assertIn('image', services)

    async def test_chat_test_with_custom_prompt_and_timings_persisted(self):
        # 1. Add a model to curated whitelist
        res = await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={
                'upstream_ids': ['custom-gpt-4o'],
                'display_names': {'custom-gpt-4o': 'Custom GPT-4o'},
                'uses': {'chat': 'openai-chat'},
                'source': 'manual',
            },
        )
        self.assertEqual(res.status_code, 200)

        # 2. Mock provider_chat_completion to assert prompt and return usage
        mock_response = {
            'choices': [{'message': {'role': 'assistant', 'content': 'Custom reply!'}}],
            'usage': {'prompt_tokens': 15, 'completion_tokens': 5, 'total_tokens': 20},
        }

        with patch('open_webui.utils.providers.provider_chat_completion', new=AsyncMock(return_value=mock_response)) as mock_chat:
            test_res = await self.client.post(
                f'/providers/connections/{self.connection.id}/model-test',
                json={
                    'upstream_id': 'custom-gpt-4o',
                    'purpose': 'chat',
                    'prompt': 'Tell me a joke',
                },
            )
            self.assertEqual(test_res.status_code, 200)
            data = test_res.json()
            self.assertEqual(data.get('status'), 'passed')
            self.assertIn('elapsed_ms', data)
            self.assertIn('timings', data)

            # Assert custom prompt was used
            mock_chat.assert_awaited_once()
            call_payload = mock_chat.await_args[0][1]
            self.assertEqual(call_payload['messages'][0]['content'], 'Tell me a joke')

        # 3. Read back models list and verify verification carries timings and usage
        models_res = await self.client.get(f'/providers/connections/{self.connection.id}/models')
        self.assertEqual(models_res.status_code, 200)
        models_data = models_res.json().get('data', [])
        self.assertEqual(len(models_data), 1)
        model_entry = models_data[0]
        chat_verification = model_entry.get('verification', {}).get('chat', {})
        self.assertEqual(chat_verification.get('status'), 'passed')
        self.assertIn('elapsed_ms', chat_verification)
        self.assertIn('timings', chat_verification)
        self.assertIn('upstream_total_ms', chat_verification['timings'])
        self.assertIn('usage', chat_verification)
        self.assertEqual(chat_verification['usage'].get('total_tokens'), 20)
