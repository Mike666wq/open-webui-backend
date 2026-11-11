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

    async def test_automatic_media_migration_is_retired(self):
        """The inferred migration is gone, and says why.

        It matched a full endpoint against a base URL and a decrypted key
        byte-for-byte, so it could never match a shared connection; when it did
        fire it wrote a private one that every other user then got a 403 from.
        Repairing that inference was the wrong move — the replacement is an
        explicit assignment, and this endpoint now refuses with a reason
        instead of returning a plausible-looking wrong answer.
        """
        preview = await self.client.get('/providers/media-migration')
        self.assertEqual(preview.status_code, 410, preview.text)
        self.assertIn('MEDIA_MIGRATION_RETIRED', preview.text)
        self.assertIn('media-assignment', preview.text)

        legacy_secret = 'legacy-synthetic'
        await Config.upsert(
            {
                'audio.stt.engine': 'openai',
                'audio.stt.model': 'old-asr',
                'audio.stt.openai.api_base_url': 'https://example.com/v1',
                'audio.stt.openai.api_key': legacy_secret,
            }
        )
        posted = await self.client.post('/providers/media-migration', json={'purpose': 'transcription'})
        self.assertEqual(posted.status_code, 410, posted.text)
        self.assertNotIn(legacy_secret, posted.text)
        # A refusal must not touch the legacy block it was asked to migrate.
        self.assertEqual(await Config.get('audio.stt.engine'), 'openai')
        self.assertEqual(await Config.get('audio.stt.model'), 'old-asr')

    async def test_assignment_view_reports_legacy_without_leaking_the_secret(self):
        legacy_secret = 'legacy-synthetic-transcription'
        await Config.upsert(
            {
                'audio.stt.engine': 'openai',
                'audio.stt.model': 'old-asr',
                'audio.stt.openai.api_base_url': 'https://example.com/v1/audio/transcriptions',
                'audio.stt.openai.api_key': legacy_secret,
                'audio.stt.provider_connection_id': '',
            }
        )
        payload = await self.client.get('/providers/media-assignment')
        self.assertEqual(payload.status_code, 200, payload.text)
        transcription = next(r for r in payload.json()['purposes'] if r['purpose'] == 'transcription')
        self.assertEqual(transcription['mode'], 'legacy')
        self.assertEqual(transcription['legacy']['normalized_base_url'], 'https://example.com/v1')
        self.assertEqual(transcription['legacy']['has_api_key'], True)
        self.assertNotIn(legacy_secret, payload.text)

    async def test_image_config_never_returns_the_api_key(self):
        """A settings GET used to hand the vendor key back as plain text."""
        from fastapi import FastAPI

        from open_webui.routers import images as images_router
        from open_webui.utils.media_assignment import CLEAR_SENTINEL

        secret = 'sk-image-legacy-synthetic'
        await Config.upsert(
            {
                'image_generation.engine': 'openai',
                'image_generation.openai.api_key': secret,
            }
        )

        # The shared app only mounts the providers router, so mount images here.
        app = FastAPI()
        app.include_router(images_router.router, prefix='/images')
        app.dependency_overrides[get_admin_user] = lambda: self.owner
        app.dependency_overrides[get_verified_user] = lambda: self.owner

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test'
        ) as client:
            config = await client.get('/images/config')
            self.assertEqual(config.status_code, 200, config.text)
            self.assertNotIn(secret, config.text)
            body = config.json()
            self.assertEqual(body['IMAGES_OPENAI_API_KEY'], '')
            self.assertEqual(body['has_api_key']['IMAGES_OPENAI_API_KEY'], True)

            # A blank key box means "keep the stored credential".
            body['IMAGE_GENERATION_MODEL'] = body.get('IMAGE_GENERATION_MODEL') or 'dall-e-3'
            body['IMAGES_OPENAI_API_KEY'] = ''
            saved = await client.post('/images/config/update', json=body)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(await Config.get('image_generation.openai.api_key'), secret)

            # The sentinel is the only way to remove it.
            body['IMAGES_OPENAI_API_KEY'] = CLEAR_SENTINEL
            cleared = await client.post('/images/config/update', json=body)
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertEqual(await Config.get('image_generation.openai.api_key'), '')
