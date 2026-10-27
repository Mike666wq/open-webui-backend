"""Provider template catalog and curated whitelist endpoints."""

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
from open_webui.utils import providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from sqlalchemy import delete


class ProviderApiV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = SimpleNamespace(id='api-owner', role='admin')
        self.connection = new_provider_connection(
            provider_id='deepseek',
            name='Synthetic',
            auth_type='api_key',
            owner_id=self.user.id,
            visibility='private',
            config={'_registry_version': 2},
            credential=providers.encrypt_credential('synthetic'),
            status='connected',
            models=[],
            is_active=True,
            protocol='openai-compatible',
            provider_template='deepseek',
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
        self.remote = patch.object(
            providers,
            '_request_json',
            new=AsyncMock(return_value={'data': [{'id': 'deepseek-chat'}, {'id': 'deepseek-reasoner'}]}),
        )
        self.mock = self.remote.start()
        self.addCleanup(self.remote.stop)

    async def test_catalog_lists_the_templates_by_group(self):
        response = await self.client.get('/providers')
        payload = response.json()
        self.assertEqual(payload['object'], 'list')
        ids = {entry['id'] for entry in payload['data']}
        for expected in ('openai', 'chatgpt', 'minimax', 'deepseek', 'siliconflow', 'bailian', 'custom'):
            self.assertIn(expected, ids)
        self.assertEqual([group['id'] for group in payload['groups']], ['official', 'cn', 'gateway', 'other'])
        minimax = next(entry for entry in payload['data'] if entry['id'] == 'minimax')
        self.assertEqual(minimax['protocol'], 'openai-compatible')
        self.assertEqual(minimax['group'], 'cn')
        self.assertTrue(minimax['supported'])

    async def test_unsupported_templates_are_listed_but_refused(self):
        payload = (await self.client.get('/providers')).json()
        anthropic = next(entry for entry in payload['data'] if entry['id'] == 'anthropic')
        self.assertFalse(anthropic['supported'])
        self.assertTrue(anthropic['unsupported_reason'])
        response = await self.client.post(
            '/providers/connections',
            json={'provider_template': 'anthropic', 'name': 'Anthropic', 'api_key': 'sk-test'},
        )
        self.assertEqual(response.status_code, 400)

    async def test_unknown_template_is_refused(self):
        response = await self.client.post(
            '/providers/connections',
            json={'provider_template': 'not-a-provider', 'name': 'Nope', 'api_key': 'sk-test'},
        )
        self.assertEqual(response.status_code, 400)

    async def test_fetch_returns_candidates_without_touching_the_whitelist(self):
        response = await self.client.post(f'/providers/connections/{self.connection.id}/models/fetch')
        payload = response.json()
        self.assertEqual(payload['error'], None)
        self.assertEqual(
            sorted(candidate['upstream_id'] for candidate in payload['candidates']),
            ['deepseek-chat', 'deepseek-reasoner'],
        )
        listed = (await self.client.get(f'/providers/connections/{self.connection.id}/models')).json()
        self.assertEqual(listed['data'], [])

    async def test_fetch_reports_the_failure_with_its_category(self):
        self.mock.side_effect = providers.provider_http_error(401)
        response = await self.client.post(f'/providers/connections/{self.connection.id}/models/fetch')
        payload = response.json()
        self.assertEqual(payload['candidates'], [])
        self.assertEqual(payload['error']['category'], 'authentication')
        self.assertEqual(payload['error']['status'], 401)

    async def test_added_models_become_the_whitelist(self):
        await self.client.post(f'/providers/connections/{self.connection.id}/models/fetch')
        response = await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={'upstream_ids': ['deepseek-chat']},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added'], ['deepseek-chat'])
        listed = (await self.client.get(f'/providers/connections/{self.connection.id}/models')).json()
        self.assertEqual([model['upstream_id'] for model in listed['data']], ['deepseek-chat'])
        self.assertEqual(listed['data'][0]['uses'], {'chat': 'openai-chat'})

    async def test_a_model_can_be_renamed_and_switched_off(self):
        await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={'upstream_ids': ['deepseek-chat']},
        )
        response = await self.client.put(
            f'/providers/connections/{self.connection.id}/models/deepseek-chat',
            json={'display_name': 'DeepSeek Chat', 'enabled': False},
        )
        self.assertEqual(response.status_code, 200)
        listed = (await self.client.get(f'/providers/connections/{self.connection.id}/models')).json()
        self.assertEqual(listed['data'][0]['name'], 'DeepSeek Chat')
        self.assertFalse(listed['data'][0]['enabled'])

    async def test_a_model_can_be_removed_from_the_whitelist(self):
        await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={'upstream_ids': ['deepseek-chat']},
        )
        response = await self.client.delete(f'/providers/connections/{self.connection.id}/models/deepseek-chat')
        self.assertEqual(response.status_code, 200)
        listed = (await self.client.get(f'/providers/connections/{self.connection.id}/models')).json()
        self.assertEqual(listed['data'], [])

    async def test_updating_an_unknown_model_is_rejected(self):
        response = await self.client.put(
            f'/providers/connections/{self.connection.id}/models/not-added',
            json={'enabled': False},
        )
        self.assertEqual(response.status_code, 404)

    async def test_the_provider_switch_hides_and_restores_the_models(self):
        await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={'upstream_ids': ['deepseek-chat']},
        )
        response = await self.client.put(
            f'/providers/connections/{self.connection.id}/enabled', json={'enabled': False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_enabled'])
        self.assertEqual((await self.client.get('/providers/models')).json()['data'], [])

        response = await self.client.put(f'/providers/connections/{self.connection.id}/enabled', json={'enabled': True})
        self.assertTrue(response.json()['is_enabled'])
        listed = (await self.client.get('/providers/models')).json()['data']
        self.assertEqual([model['upstream_id'] for model in listed], ['deepseek-chat'])

    async def test_editing_the_credential_keeps_the_models_of_an_unmigrated_connection(self):
        """A legacy connection must still be able to move its models into the whitelist."""
        legacy = new_provider_connection(
            provider_id='deepseek',
            name='Legacy',
            auth_type='api_key',
            owner_id=self.user.id,
            visibility='private',
            config={},
            credential=providers.encrypt_credential('synthetic'),
            status='configured',
            models=[{'id': 'legacy-model', 'upstream_id': 'legacy-model', 'name': 'Legacy model'}],
            is_active=True,
            protocol='openai-compatible',
            provider_template='deepseek',
        )
        async with get_async_db_context() as db:
            db.add(legacy)
            await db.commit()
        response = await self.client.patch(
            f'/providers/connections/{legacy.id}',
            json={'api_key': 'rotated'},
        )
        self.assertEqual(response.status_code, 200)
        async with get_async_db_context() as db:
            stored = await db.get(ProviderConnection, legacy.id)
            self.assertEqual([model['upstream_id'] for model in stored.models], ['legacy-model'])
        listed = (await self.client.get(f'/providers/connections/{legacy.id}/models')).json()
        self.assertEqual([model['upstream_id'] for model in listed['data']], ['legacy-model'])

    async def test_editing_the_credential_keeps_the_curated_whitelist(self):
        await self.client.post(
            f'/providers/connections/{self.connection.id}/models',
            json={'upstream_ids': ['deepseek-chat']},
        )
        response = await self.client.patch(
            f'/providers/connections/{self.connection.id}',
            json={'api_key': 'rotated'},
        )
        self.assertEqual(response.status_code, 200)
        listed = (await self.client.get(f'/providers/connections/{self.connection.id}/models')).json()
        self.assertEqual([model['upstream_id'] for model in listed['data']], ['deepseek-chat'])
