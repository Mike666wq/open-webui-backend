"""Curated whitelist invariants for the provider registry.

The whitelist is the source of truth for what a provider offers: a catalog
refresh only fills the candidate pool and never adds a model on its own.
Connections that could not be migrated keep working through the legacy path.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import (
    ProviderConnection,
    ProviderModel,
    new_provider_connection,
)
from open_webui.routers import providers as api
from open_webui.utils import providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_capabilities import fingerprint
from sqlalchemy import delete, select


class ProviderRegistryV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = SimpleNamespace(id='registry-owner', role='admin')
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
        )
        async with get_async_db_context() as db:
            await db.execute(
                delete(ProviderConnection).where(ProviderConnection.owner_id == self.user.id)
            )
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
            new=AsyncMock(return_value={'data': [{'id': 'new-model'}, {'id': 'picked-model'}]}),
        )
        self.mock = self.remote.start()
        self.addCleanup(self.remote.stop)

    async def seed(self, *upstream_ids, enabled=True, uses=None):
        async with get_async_db_context() as db:
            await providers.seed_connection_models(
                self.connection,
                list(upstream_ids),
                db,
                uses=uses or {'chat': 'openai-chat'},
            )
            await db.commit()

    async def whitelist(self):
        async with get_async_db_context() as db:
            return await providers.connection_models(self.connection, db)

    async def test_seeded_models_form_the_whitelist(self):
        await self.seed('picked-model')
        models = await self.whitelist()
        self.assertEqual([m['upstream_id'] for m in models], ['picked-model'])
        self.assertEqual(models[0]['uses'], {'chat': 'openai-chat'})
        self.assertTrue(models[0]['enabled'])

    async def test_catalog_refresh_does_not_add_models_to_the_whitelist(self):
        await self.seed('picked-model')
        response = await self.client.post(f'/providers/connections/{self.connection.id}/models/refresh')
        self.assertEqual(response.status_code, 200)
        models = await self.whitelist()
        self.assertEqual([m['upstream_id'] for m in models], ['picked-model'])

    async def test_new_upstream_models_stay_out_of_the_whitelist(self):
        await self.seed('picked-model')
        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, self.connection.id)
            await providers.refresh_connection_catalog(connection, self.user, db)
        models = await self.whitelist()
        self.assertNotIn('new-model', [m['upstream_id'] for m in models])

    async def test_legacy_connection_without_the_marker_keeps_its_models(self):
        legacy = new_provider_connection(
            provider_id='deepseek',
            name='Legacy',
            auth_type='api_key',
            owner_id=self.user.id,
            visibility='private',
            config={'_catalog_version': 1, '_model_enabled': {'old': True}},
            credential=providers.encrypt_credential('synthetic'),
            status='connected',
            models=[
                {
                    'id': 'provider:legacy:old',
                    'upstream_id': 'old',
                    'name': 'Old',
                    'uses': {'chat': 'openai-chat'},
                }
            ],
            is_active=True,
        )
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.id == legacy.id))
            db.add(legacy)
            await db.commit()
        async with get_async_db_context() as db:
            models = await providers.connection_models(legacy, db)
        self.assertEqual([m['upstream_id'] for m in models], ['old'])
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.id == legacy.id))
            await db.commit()

    async def test_provider_switch_hides_every_model(self):
        await self.seed('picked-model')
        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, self.connection.id)
            connection.is_enabled = False
            await db.commit()
        response = await self.client.get('/providers/models')
        self.assertEqual(response.json()['data'], [])
        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, self.connection.id)
            connection.is_enabled = True
            await db.commit()
        response = await self.client.get('/providers/models')
        self.assertEqual([m['upstream_id'] for m in response.json()['data']], ['picked-model'])

    async def test_disabled_model_is_hidden_but_stays_in_the_whitelist(self):
        await self.seed('picked-model', 'second-model')
        async with get_async_db_context() as db:
            await providers.set_connection_model_enabled(self.connection, 'picked-model', False, db)
            await db.commit()
        response = await self.client.get('/providers/models')
        self.assertEqual([m['upstream_id'] for m in response.json()['data']], ['second-model'])
        models = await self.whitelist()
        self.assertEqual(len(models), 2)
        self.assertFalse(next(m for m in models if m['upstream_id'] == 'picked-model')['enabled'])

    async def test_purposes_and_verification_are_stored_on_the_whitelist_row(self):
        await self.seed('picked-model')
        async with get_async_db_context() as db:
            await providers.set_connection_model_uses(self.connection, 'picked-model', {'image': 'openai-image'}, db)
            connection = await db.get(ProviderConnection, self.connection.id)
            expected = fingerprint(connection, {'upstream_id': 'picked-model', 'uses': {'image': 'openai-image'}})
            await providers.set_connection_model_verification(
                self.connection,
                'picked-model',
                'image',
                {'status': 'passed', 'tested_at': 1, 'fingerprint': expected},
                db,
            )
            await db.commit()
        models = await self.whitelist()
        self.assertEqual(models[0]['uses'], {'image': 'openai-image'})
        self.assertEqual(models[0]['verification']['image']['status'], 'passed')

    async def test_verification_is_not_trusted_after_the_configuration_changes(self):
        await self.seed('picked-model')
        async with get_async_db_context() as db:
            row = (
                await db.scalars(
                    select(ProviderModel).where(
                        ProviderModel.connection_id == self.connection.id,
                        ProviderModel.upstream_id == 'picked-model',
                    )
                )
            ).first()
            row.verification = {'chat': {'status': 'passed', 'fingerprint': 'stale'}}
            await db.commit()
        models = await self.whitelist()
        self.assertEqual(models[0]['verification']['chat']['status'], 'untested')

    async def test_whitelist_rows_never_leak_the_credential_fingerprint(self):
        await self.seed('picked-model')
        async with get_async_db_context() as db:
            row = (
                await db.scalars(
                    select(ProviderModel).where(
                        ProviderModel.connection_id == self.connection.id,
                        ProviderModel.upstream_id == 'picked-model',
                    )
                )
            ).first()
            row.verification = {'chat': {'status': 'passed', 'fingerprint': 'secret-hash'}}
            await db.commit()
        models = await self.whitelist()
        self.assertNotIn('fingerprint', models[0]['verification']['chat'])

    async def test_public_connection_reports_the_registry_state(self):
        await self.seed('picked-model')
        response = await self.client.get(f'/providers/connections/{self.connection.id}')
        payload = response.json()
        self.assertEqual(payload['protocol'], 'openai-compatible')
        self.assertTrue(payload['is_enabled'])
        self.assertEqual(payload['provider_template'], 'deepseek')
        self.assertEqual([m['upstream_id'] for m in payload['models']], ['picked-model'])

    async def test_unknown_model_is_rejected_without_discovery(self):
        await self.seed('picked-model')
        with self.assertRaises(HTTPException) as caught:
            await providers.resolve_model(
                providers.stable_model_id(self.connection.id, 'not-added'), self.user, None
            )
        self.assertEqual(caught.exception.status_code, 404)
