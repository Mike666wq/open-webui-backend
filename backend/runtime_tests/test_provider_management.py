"""Provider management invariants using synthetic catalogs and disposable DBs."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.provider_calls import ProviderCall, reported_usage
from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
from open_webui.routers import providers as api
from open_webui.utils import providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_receipts import record_invocation
from sqlalchemy import delete, select
from starlette.responses import StreamingResponse


class ProviderManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        persistent = patch.object(Config, 'PERSISTENT_ENABLED', True)
        persistent.start()
        self.addCleanup(persistent.stop)
        self.user = SimpleNamespace(id='management-owner', role='admin')
        self.other = SimpleNamespace(id='management-other', role='admin')
        self.connection = new_provider_connection(
            provider_id='deepseek',
            name='Synthetic',
            auth_type='api_key',
            owner_id=self.user.id,
            visibility='private',
            config={},
            credential=providers.encrypt_credential('synthetic'),
            status='configured',
            models=[],
            is_active=True,
        )
        self.mid = providers.stable_model_id(self.connection.id, 'old')
        self.connection.models = [{'id': self.mid, 'upstream_id': 'old'}]
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.owner_id == self.user.id))
            await db.execute(delete(ProviderCall).where(ProviderCall.user_id.in_([self.user.id, self.other.id])))
            db.add(self.connection)
            await db.commit()
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/providers')
        self.app.dependency_overrides[get_verified_user] = lambda: self.user
        self.app.dependency_overrides[get_admin_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://fixture')
        self.addAsyncCleanup(self.client.aclose)
        self.remote = patch.object(
            providers, '_request_json', new=AsyncMock(return_value={'data': [{'id': 'old'}, {'id': 'new'}]})
        )
        self.mock = self.remote.start()
        self.addCleanup(self.remote.stop)

    async def refresh(self):
        return await self.client.post(f'/providers/connections/{self.connection.id}/models/refresh')

    async def test_refresh_preserves_old_and_disables_new_models(self):
        result = await self.refresh()
        self.assertEqual(result.status_code, 200)
        rows = {m['upstream_id']: m for m in result.json()['models']}
        self.assertTrue(rows['old']['enabled'])
        self.assertFalse(rows['new']['enabled'])
        self.assertEqual(
            [m['upstream_id'] for m in (await self.client.get('/providers/models')).json()['data']], ['old']
        )
        await self.client.put(
            f'/providers/connections/{self.connection.id}/model-enabled', json={'upstream_id': 'old', 'enabled': False}
        )
        self.assertEqual((await self.client.get('/providers/models')).json()['data'], [])
        async with get_async_db_context() as db:
            with self.assertRaises(HTTPException):
                await providers.resolve_model(self.mid, self.user, db)
        await self.refresh()
        rows = (await self.client.get('/providers/connections')).json()[0]['models']
        self.assertTrue(all(not m['enabled'] for m in rows))

    async def test_refresh_failure_keeps_directory_and_sanitizes_errors(self):
        await self.refresh()
        self.mock.side_effect = HTTPException(502, 'secret upstream body synthetic')
        self.assertEqual((await self.refresh()).status_code, 502)
        result = await self.client.get('/providers/models')
        self.assertEqual([m['upstream_id'] for m in result.json()['data']], ['old'])
        self.assertNotIn('secret upstream body', result.text)
        self.assertEqual(result.json()['catalogs'][0]['error']['category'], 'network')

    async def test_stale_catalog_returns_without_waiting_and_coalesces_background(self):
        await self.refresh()
        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, self.connection.id)
            config = dict(connection.config)
            cache = dict(config['_models_by_user'])
            cache[self.user.id] = {**cache[self.user.id], 'updated_at': int(time.time()) - 1000}
            connection.config = {**config, '_models_by_user': cache}
            await db.commit()
        gate = asyncio.Event()

        async def background(*args):
            await gate.wait()

        with patch.object(providers, '_refresh_catalog_background', side_effect=background) as refresh:
            try:
                first = await self.client.get('/providers/models')
                second = await self.client.get('/providers/models')
                self.assertTrue(first.json()['catalogs'][0]['stale'])
                self.assertEqual(first.json()['data'], second.json()['data'])
                await asyncio.sleep(0)
                self.assertEqual(refresh.call_count, 1)
            finally:
                gate.set()
                await asyncio.gather(*list(providers._catalog_refresh_tasks.values()))

    async def test_explicit_refresh_coalesces_simultaneous_requests(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def discover(*args):
            entered.set()
            await release.wait()
            return {'data': [{'id': 'old'}]}

        self.mock.side_effect = discover
        first = asyncio.create_task(self.refresh())
        await entered.wait()
        second = asyncio.create_task(self.refresh())
        await asyncio.sleep(0.03)
        release.set()
        results = await asyncio.gather(first, second)
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(self.mock.await_count, 1)

    async def test_revoked_access_cannot_read_stale_models(self):
        await self.refresh()
        self.app.dependency_overrides[get_verified_user] = lambda: self.other
        self.assertEqual((await self.client.get('/providers/models')).json()['data'], [])
        self.assertEqual(
            (await self.client.post(f'/providers/connections/{self.connection.id}/models/refresh')).status_code, 404
        )

    async def test_auxiliary_model_never_falls_back(self):
        from open_webui.routers.tasks import get_task_model_generation_config
        from open_webui.utils.task import get_task_model_id

        models = {'gpt': {'connection_type': 'external'}, 'cheap': {'connection_type': 'external'}}
        self.assertIsNone(get_task_model_id('gpt', '', '', models))
        self.assertIsNone(get_task_model_id('gpt', '', 'missing', models))
        self.assertEqual(get_task_model_id('gpt', '', 'cheap', models), 'cheap')
        with (
            patch.object(Config, 'get_many', new=AsyncMock(return_value={})),
            self.assertRaises(HTTPException) as error,
        ):
            await get_task_model_generation_config('gpt', models)
        self.assertEqual(error.exception.status_code, 409)

    async def test_receipts_stream_usage_failure_and_owner_isolation(self):
        async def frames():
            yield b'data: {"usage":{"prompt_tokens":9,"completion_tokens":3,"completion_tokens_details":{"reasoning_tokens":2}}}\n\n'
            yield b'data: [DONE]\n\n'

        @record_invocation
        async def call(request, form_data, user):
            return StreamingResponse(frames())

        request = SimpleNamespace(
            state=SimpleNamespace(metadata={'chat_id': 'chat', 'message_id': 'turn'}),
            app=SimpleNamespace(state=SimpleNamespace(MODELS={'gpt': {'connection_id': self.connection.id}})),
        )
        response = await call(request, {'model': 'gpt'}, self.user)
        self.assertEqual(len([chunk async for chunk in response.body_iterator]), 2)
        rows = (await self.client.get('/providers/calls/history?chat_id=chat&message_id=turn')).json()['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['usage'], {'input_tokens': 9, 'output_tokens': 3, 'reasoning_tokens': 2})
        self.assertEqual(rows[0]['status'], 'completed')
        self.app.dependency_overrides[get_verified_user] = lambda: self.other
        self.assertEqual((await self.client.get('/providers/calls/history')).json()['data'], [])
        self.assertEqual((await self.client.get('/providers/calls/history?admin_summary=true')).json()['data'], [])
        self.assertIsNone(reported_usage({}))

    async def test_legacy_migration_idempotent_and_keeps_alias(self):
        from open_webui.utils.provider_migration import apply_migration, preview_migration
        from starlette.requests import Request

        await Config.upsert(
            {
                'openai.api_base_urls': ['https://example.com/v1'],
                'openai.api_keys': ['synthetic'],
                'openai.api_configs': {'0': {'model_ids': ['legacy'], 'prefix_id': 'source'}},
            }
        )
        request = Request(
            {'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(BASE_MODELS=[])), 'headers': []}
        )
        async with get_async_db_context() as db:
            preview = await preview_migration(request, self.user, db)
            source = preview['sources'][0]
            self.assertNotIn('synthetic', str(preview))
            result = await apply_migration(request, self.user, db, 0, source['fingerprint'], {})
            again = await apply_migration(request, self.user, db, 0, source['fingerprint'], {})
            self.assertEqual(result['id'], again['id'])
            visible = await providers.list_visible_models(self.user, db)
            self.assertIn('source.legacy', [m['id'] for m in visible])
            self.assertFalse((await Config.get('openai.api_configs'))['0']['enable'])
            new_preview = await preview_migration(request, self.user, db)
            self.assertEqual(new_preview['sources'][0]['migrated_connection'], result['id'])

    async def test_legacy_directory_failure_retains_prior_models(self):
        from open_webui.utils.legacy_catalogs import cached_legacy_catalog

        request = SimpleNamespace(state=SimpleNamespace(), query_params={'refresh': 'true'})
        fetch = AsyncMock(return_value={'data': [{'id': 'legacy'}]})
        first = await cached_legacy_catalog(request, 'https://legacy.invalid/v1', 'synthetic', self.user, {}, fetch)
        fetch.return_value = {'catalog_error_status': 500}
        second = await cached_legacy_catalog(request, 'https://legacy.invalid/v1', 'synthetic', self.user, {}, fetch)
        self.assertEqual(first, second)
        self.assertEqual(next(iter(request.state.legacy_catalog_statuses.values()))['error']['status'], 500)
        fetch.assert_awaited()

    async def test_receipts_cancel_and_expire_without_retaining_content(self):
        from open_webui.models.provider_calls import cleanup_calls, save_call

        closed = []

        async def frames():
            try:
                yield b'data: {"choices":[{"delta":{"content":"never persist this"}}]}\n\n'
                yield b'data: [DONE]\n\n'
            finally:
                closed.append(True)

        @record_invocation
        async def call(request, form_data, user):
            return StreamingResponse(frames())

        request = SimpleNamespace(
            state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(MODELS={'gpt': {}}))
        )
        # A nonempty model metadata fixture avoids unnecessary catalog loading.
        request.app.state.MODELS['gpt'] = {'id': 'gpt'}
        response = await call(request, {'model': 'gpt'}, self.user)
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        self.assertTrue(closed)
        await save_call(
            user_id=self.user.id,
            connection_id=None,
            model_id='old',
            chat_id=None,
            message_id=None,
            purpose='chat',
            status='completed',
            elapsed_ms=1,
            usage=None,
            created_at=int(time.time()) - 31 * 86400,
        )
        await cleanup_calls()
        async with get_async_db_context() as db:
            records = (await db.scalars(select(ProviderCall).where(ProviderCall.user_id == self.user.id))).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, 'cancelled')
        result = await self.client.get('/providers/calls/history')
        self.assertNotIn('never persist this', result.text)
        self.assertIsNone(result.json()['data'][0]['usage'])

    async def test_migration_failure_preserves_source_and_blocks_special_protocols(self):
        from open_webui.utils.provider_migration import apply_migration, preview_migration
        from starlette.requests import Request

        await Config.upsert(
            {
                'openai.api_base_urls': ['https://migration.invalid/v1'],
                'openai.api_keys': ['synthetic'],
                'openai.api_configs': {'0': {'model_ids': ['legacy'], 'enable': True, 'headers': {'X-Custom': 'test'}}},
            }
        )
        request = Request(
            {'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(BASE_MODELS=[])), 'headers': []}
        )
        async with get_async_db_context() as db:
            source = (await preview_migration(request, self.user, db))['sources'][0]
            self.assertIn('headers', source['blocked_reason'])
            with self.assertRaises(HTTPException):
                await apply_migration(request, self.user, db, 0, source['fingerprint'], {})
            self.assertTrue((await Config.get('openai.api_configs'))['0']['enable'])
        await Config.upsert({'openai.api_configs': {'0': {'model_ids': ['legacy'], 'enable': True}}})
        async with get_async_db_context() as db:
            source = (await preview_migration(request, self.user, db))['sources'][0]
            with patch.object(Config, 'upsert', new=AsyncMock(side_effect=RuntimeError('synthetic save failure'))):
                with self.assertRaises(RuntimeError):
                    await apply_migration(request, self.user, db, 0, source['fingerprint'], {})
            await db.rollback()
        self.assertTrue((await Config.get('openai.api_configs'))['0']['enable'])

    async def test_upgrade_preserves_existing_per_user_model_selection(self):
        import hashlib

        async with get_async_db_context() as db:
            connection = await db.get(ProviderConnection, self.connection.id)
            connection.models = []
            connection.config = {
                '_catalog_version': 1,
                '_models_by_user': {
                    self.user.id: {
                        'credential_hash': hashlib.sha256(connection.credential.encode()).hexdigest(),
                        'models': [{'id': self.mid, 'upstream_id': 'old', 'uses': {'chat': 'openai-chat'}}],
                    }
                },
            }
            await db.commit()
            models = await providers.visible_connection_models(connection, self.user, db)
            self.assertTrue(models[0]['enabled'])
            self.assertEqual(models[0]['id'], self.mid)
            self.mock.assert_not_awaited()

    async def test_malformed_success_is_not_allowed_to_erase_catalog(self):
        await self.refresh()
        self.mock.return_value = {'message': 'not a catalog'}
        self.assertEqual((await self.refresh()).status_code, 502)
        result = await self.client.get('/providers/models')
        self.assertEqual([m['upstream_id'] for m in result.json()['data']], ['old'])

    async def test_disabled_legacy_source_cannot_be_called_from_cached_route(self):
        from open_webui.routers.openai import get_openai_connection

        await Config.upsert(
            {
                'openai.api_base_urls': ['https://disabled.invalid/v1'],
                'openai.api_keys': ['synthetic'],
                'openai.api_configs': {'0': {'enable': False}},
            }
        )
        with self.assertRaises(HTTPException) as error:
            await get_openai_connection(0)
        self.assertEqual(error.exception.status_code, 503)

    async def test_missing_auxiliary_model_is_visible_during_search(self):
        from open_webui.utils import middleware

        emitter = AsyncMock()
        with (
            patch.object(
                middleware,
                'generate_queries',
                new=AsyncMock(side_effect=HTTPException(409, 'Configure auxiliary model')),
            ),
            patch.object(middleware, 'process_web_search', new=AsyncMock(return_value=[])) as search,
        ):
            await middleware.chat_web_search_handler(
                SimpleNamespace(),
                {'model': 'gpt', 'messages': [{'role': 'user', 'content': 'synthetic query'}]},
                {'__event_emitter__': emitter},
                self.user,
            )
        self.assertTrue(
            any(c.args[0].get('data', {}).get('action') == 'auxiliary_model_missing' for c in emitter.call_args_list)
        )
        self.assertEqual(search.call_args.args[1].queries, ['synthetic query'])

    async def test_upstream_500_is_distinct_from_local_gateway_502(self):
        error = providers.catalog_error(providers.provider_http_error(500))
        self.assertEqual(error['category'], 'upstream')
        self.assertEqual(error['status'], 500)
        self.assertEqual(error['http_status'], 502)
        self.assertEqual(providers.catalog_error(HTTPException(502, 'Could not reach provider'))['category'], 'network')
