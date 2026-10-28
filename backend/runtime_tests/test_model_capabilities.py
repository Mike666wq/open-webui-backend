"""Unified catalog: real DB authorization, manual classification and migration."""

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
from open_webui.routers import providers as api
from open_webui.utils import providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_capabilities import fingerprint, normalize_model
from sqlalchemy import delete


class ModelCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = SimpleNamespace(id='catalog-owner', role='admin')
        self.other = SimpleNamespace(id='catalog-other', role='admin')
        self.connection = new_provider_connection(
            provider_id='custom',
            name='Catalog',
            auth_type='api_key',
            visibility='private',
            owner_id=self.owner.id,
            config={'base_url': 'https://example.com/v1', '_catalog_version': 1},
            credential=providers.encrypt_credential('synthetic-key'),
            status='connected',
            models=[],
            is_active=True,
        )
        async with get_async_db_context() as db:
            await db.execute(
                delete(ProviderConnection).where(ProviderConnection.owner_id.in_([self.owner.id, self.other.id]))
            )
            db.add(self.connection)
            await db.commit()
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/providers')
        self.app.dependency_overrides[get_verified_user] = lambda: self.owner
        self.app.dependency_overrides[get_admin_user] = lambda: self.owner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.remote = patch.object(providers, '_request_json', new=AsyncMock(return_value={'data': []}))
        self.remote_mock = self.remote.start()
        self.addCleanup(self.remote.stop)

    def model(self, name='test-model', **extra):
        return {'id': providers.stable_model_id(self.connection.id, name), 'upstream_id': name, 'name': name, **extra}

    async def save_uses(self, name, uses):
        return await self.client.put(
            f'/providers/connections/{self.connection.id}/model-uses', json={'upstream_id': name, 'uses': uses}
        )

    async def test_unknown_new_models_are_not_chat_and_manual_multimodal_is_filtered(self):
        self.remote_mock.return_value = {'data': [{'id': 'unknown'}]}
        result = await self.client.get('/providers/models?purpose=chat')
        self.assertEqual(result.json()['data'], [])
        result = await self.client.get('/providers/models?purpose=all')
        self.assertEqual(result.json()['data'][0]['uses'], {})
        self.assertEqual(
            (
                await self.save_uses('unknown', {'transcription': 'openai-transcription', 'speech': 'openai-speech'})
            ).status_code,
            200,
        )
        self.assertEqual((await self.client.get('/providers/models?purpose=chat')).json()['data'], [])
        await self.client.put(
            f'/providers/connections/{self.connection.id}/model-enabled',
            json={'upstream_id': 'unknown', 'enabled': True},
        )
        self.assertEqual(len((await self.client.get('/providers/models?purpose=transcription')).json()['data']), 1)
        denied = await self.client.get('/providers/models?purpose=bad')
        self.assertEqual(denied.status_code, 400)
        async with get_async_db_context() as db:
            with self.assertRaises(Exception) as error:
                await providers.resolve_model(providers.stable_model_id(self.connection.id, 'unknown'), self.owner, db)
            self.assertEqual(error.exception.status_code, 404)

    async def test_saved_model_switch_filters_chat_and_blocks_stale_calls(self):
        from open_webui.models.models import Model
        from sqlalchemy import update

        await self.save_uses('switch-model', {'chat': 'openai-chat'})
        model_id = self.model('switch-model')['id']
        async with get_async_db_context() as db:
            db.add(
                Model(
                    id=model_id,
                    user_id=self.owner.id,
                    name='Switch',
                    base_model_id=None,
                    params={},
                    meta={},
                    is_active=False,
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()
            self.assertEqual(await providers.list_visible_models(self.owner, db), [])
            with self.assertRaises(HTTPException) as error:
                await providers.resolve_model(model_id, self.owner, db)
            self.assertEqual(error.exception.status_code, 404)
            await db.execute(update(Model).where(Model.id == model_id).values(is_active=True))
            await db.commit()
            self.assertIn(model_id, [m['id'] for m in await providers.list_visible_models(self.owner, db)])
            self.assertEqual(
                (await providers.resolve_model(model_id, self.owner, db)).upstream_model_id, 'switch-model'
            )
            await db.execute(delete(Model).where(Model.id == model_id))
            await db.commit()

    async def test_refresh_preserves_overrides_and_no_inference(self):
        await self.save_uses('manual-asr', {'transcription': 'openai-transcription'})
        response = await self.client.post(f'/providers/connections/{self.connection.id}/models/refresh')
        self.assertEqual(response.status_code, 200)
        model = response.json()['models'][0]
        self.assertEqual(model['uses'], {'transcription': 'openai-transcription'})
        self.assertEqual(model['verification']['transcription']['status'], 'untested')
        self.assertTrue(all(call.args[2].endswith('/models') for call in self.remote_mock.call_args_list))

    async def test_failed_catalog_does_not_hide_other_models_or_leak_private_connections(self):
        broken = new_provider_connection(
            provider_id='custom',
            name='Broken catalog',
            auth_type='api_key',
            visibility='private',
            owner_id=self.owner.id,
            config={'base_url': 'https://example.com/v1'},
            credential=providers.encrypt_credential('synthetic-key'),
            status='connected',
            models=[],
            is_active=True,
        )
        async with get_async_db_context() as db:
            db.add(broken)
            await db.commit()

        async def discover(connection, user, db):
            if connection.id == broken.id:
                raise HTTPException(502, 'Provider request failed with HTTP 404')
            return [self.model(uses={'transcription': 'openai-transcription'})]

        with patch.object(providers, 'visible_connection_models', side_effect=discover):
            result = await self.client.get('/providers/models?purpose=transcription')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(result.json()['data']), 1)
            self.assertEqual(
                result.json()['errors'],
                [
                    {
                        'connection_id': broken.id,
                        'connection_name': 'Broken catalog',
                        'status': 502,
                    }
                ],
            )
            self.app.dependency_overrides[get_verified_user] = lambda: self.other
            private = await self.client.get('/providers/models?purpose=transcription')
            self.assertEqual(private.json()['data'], [])
            self.assertEqual(private.json()['errors'], [])

    async def test_private_models_and_management_are_not_accessible_to_other_admin(self):
        await self.save_uses('private-asr', {'transcription': 'openai-transcription'})
        self.app.dependency_overrides[get_verified_user] = lambda: self.other
        self.assertEqual((await self.client.get('/providers/models?purpose=transcription')).json()['data'], [])
        self.assertEqual((await self.save_uses('private-asr', {'chat': 'openai-chat'})).status_code, 404)
        async with get_async_db_context() as db:
            with self.assertRaises(Exception) as error:
                await providers.resolve_media_model(
                    providers.stable_model_id(self.connection.id, 'private-asr'), self.other, 'transcription', db
                )
            self.assertEqual(error.exception.status_code, 404)

    async def test_invalid_adapter_cannot_be_saved(self):
        for uses in ({'transcription': 'openai-chat'}, {'image': 'siliconflow-image'}, {'video': 'custom'}):
            self.assertEqual((await self.save_uses('test', uses)).status_code, 400)

    async def test_account_capabilities_cannot_be_broadened(self):
        self.connection.provider_id = 'chatgpt'
        self.connection.auth_type = 'oauth'
        self.connection.config = {'_model_overrides': {'account': {'image': 'openai-image'}}}
        model = normalize_model(self.connection, self.model('account'))
        self.assertEqual(model['uses'], {'chat': 'responses'})

    async def test_old_persisted_chat_is_retained_but_not_new_unknown(self):
        self.connection.config = {}
        self.connection.models = [self.model('old')]
        self.assertEqual(normalize_model(self.connection, self.model('old'))['capability_source'], 'legacy-unconfirmed')
        self.assertEqual(normalize_model(self.connection, self.model('new'))['uses'], {})

    async def test_siliconflow_uses_filtered_catalog_not_name_guess(self):
        self.connection.provider_id = 'siliconflow'

        async def request(connection, user, url, **kwargs):
            purpose = kwargs.get('params', {}).get('sub_type')
            if purpose is None:
                return {
                    'data': [
                        {'id': 'FunAudioLLM/CosyVoice2-0.5B'},
                        {'id': 'unknown'},
                    ]
                }
            return {
                'data': [
                    {
                        'id': {'chat': 'text', 'speech-to-text': 'asr', 'text-to-image': 'picture'}.get(
                            purpose, 'unknown'
                        )
                    }
                ]
            }

        self.remote_mock.side_effect = request
        models = await providers.discover_connection_models(self.connection, self.owner)
        result = {m['upstream_id']: m['uses'] for m in models}
        self.assertEqual(result['asr'], {'transcription': 'openai-transcription'})
        self.assertEqual(result['picture'], {'image': 'siliconflow-image'})
        self.assertEqual(result['FunAudioLLM/CosyVoice2-0.5B'], {'speech': 'openai-speech'})
        self.assertEqual(result['unknown'], {})
        speech = next(m for m in models if m['upstream_id'] == 'FunAudioLLM/CosyVoice2-0.5B')
        self.assertEqual(len(speech['voice_options']), 8)
        self.assertIn('FunAudioLLM/CosyVoice2-0.5B:alex', [voice['id'] for voice in speech['voice_options']])

    async def test_test_results_invalidate_when_endpoint_or_key_or_uses_change(self):
        self.connection.config = {'_model_overrides': {'test': {'speech': 'openai-speech'}}}
        model = normalize_model(self.connection, self.model('test'))
        marker = fingerprint(self.connection, model)
        self.connection.config = {
            **self.connection.config,
            '_model_tests': {'test': {'speech': {'status': 'passed', 'fingerprint': marker}}},
        }
        self.assertEqual(
            normalize_model(self.connection, self.model('test'))['verification']['speech']['status'], 'passed'
        )
        self.connection.credential = providers.encrypt_credential('rotated')
        self.assertEqual(
            normalize_model(self.connection, self.model('test'))['verification']['speech']['status'], 'untested'
        )
        self.assertNotIn('fingerprint', str(normalize_model(self.connection, self.model('test'))['verification']))

    async def test_explicit_test_once_and_sanitized_failure(self):
        await self.save_uses('test', {'speech': 'openai-speech'})
        response = SimpleNamespace(status=401)

        @asynccontextmanager
        async def result():
            yield response

        session = SimpleNamespace(post=Mock(return_value=result()))

        @asynccontextmanager
        async def session_scope(connection, **_options):
            yield session

        with patch.object(providers, 'media_http_session', side_effect=session_scope):
            result = await self.client.post(
                f'/providers/connections/{self.connection.id}/model-test',
                json={'upstream_id': 'test', 'purpose': 'speech', 'voice': 'test-voice'},
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'failed')
        self.assertEqual(session.post.call_count, 1)
        self.assertNotIn('synthetic-key', result.text)
        models = (await self.client.get('/providers/models?purpose=speech')).json()['data']
        self.assertEqual(models[0]['verification']['speech']['status'], 'failed')

    async def test_migration_is_private_idempotent_and_keeps_legacy_secret(self):
        await Config.upsert(
            {
                'audio.stt.engine': 'openai',
                'audio.stt.model': 'old-asr',
                'audio.stt.openai.api_base_url': 'https://example.com/v1',
                'audio.stt.openai.api_key': 'legacy-synthetic',
            }
        )
        preview = (await self.client.get('/providers/media-migration')).json()
        row = next(r for r in preview if r['purpose'] == 'transcription')
        self.assertNotIn('legacy-synthetic', str(preview))
        payload = {'purpose': 'transcription', 'fingerprint': row['fingerprint']}
        first = await self.client.post('/providers/media-migration', json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        second = await self.client.post('/providers/media-migration', json=payload)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(first.json()['visibility'], 'private')
        self.assertEqual(await Config.get('audio.stt.openai.api_key'), 'legacy-synthetic')
        self.assertTrue((await Config.get('audio.stt.model')).startswith('provider:'))
        self.assertEqual(await Config.get('audio.stt.engine'), 'provider')

    async def test_equal_legacy_media_values_migrate_by_purpose(self):
        shared = {
            'engine': 'openai',
            'model': 'shared-model',
            'openai.api_base_url': 'https://example.com/v1',
            'openai.api_key': 'shared-synthetic-key',
        }
        await Config.upsert(
            {
                **{f'audio.stt.{key}': value for key, value in shared.items()},
                **{f'audio.tts.{key}': value for key, value in shared.items()},
            }
        )
        preview = {row['purpose']: row for row in (await self.client.get('/providers/media-migration')).json()}
        migrated = {}
        for purpose in ('transcription', 'speech'):
            row = preview[purpose]
            first = await self.client.post(
                '/providers/media-migration',
                json={'purpose': purpose, 'fingerprint': row['fingerprint']},
            )
            self.assertEqual(first.status_code, 200, first.text)
            second = await self.client.post(
                '/providers/media-migration',
                json={'purpose': purpose, 'fingerprint': row['fingerprint']},
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()['id'], first.json()['id'])
            migrated[purpose] = first.json()['id']
        self.assertNotEqual(migrated['transcription'], migrated['speech'])

    async def test_changed_legacy_configuration_rejects_stale_migration(self):
        await Config.upsert(
            {
                'audio.tts.engine': 'openai',
                'audio.tts.model': 'old-tts',
                'audio.tts.openai.api_base_url': 'https://example.com/v1',
                'audio.tts.openai.api_key': 'legacy-synthetic',
            }
        )
        row = next(r for r in (await self.client.get('/providers/media-migration')).json() if r['purpose'] == 'speech')
        await Config.upsert({'audio.tts.model': 'changed'})
        result = await self.client.post(
            '/providers/media-migration', json={'purpose': 'speech', 'fingerprint': row['fingerprint']}
        )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(await Config.get('audio.tts.engine'), 'openai')
