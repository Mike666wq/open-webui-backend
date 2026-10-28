"""Per-service provider configuration: declarations, addresses and purposes."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
from open_webui.routers import providers as api
from open_webui.utils import provider_media, providers
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.provider_capabilities import service_declaration, validate_model_uses
from open_webui.utils.provider_registry import public_templates, resolve_template
from sqlalchemy import delete


class ProviderServicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = SimpleNamespace(id='services-owner', role='admin')
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/providers')
        self.app.dependency_overrides[get_verified_user] = lambda: self.owner
        self.app.dependency_overrides[get_admin_user] = lambda: self.owner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        async with get_async_db_context() as db:
            await db.execute(delete(ProviderConnection).where(ProviderConnection.owner_id == self.owner.id))
            await db.commit()
        self.remote = patch.object(providers, '_request_json', new=AsyncMock(return_value={'data': []}))
        self.remote_mock = self.remote.start()
        self.addCleanup(self.remote.stop)

    def minimax_connection(self, **overrides):
        values = {
            'provider_id': 'minimax',
            'provider_template': 'minimax',
            'name': 'MiniMax',
            'auth_type': 'api_key',
            'visibility': 'private',
            'owner_id': self.owner.id,
            'config': {
                'base_url': 'https://api.minimax.chat/v1',
                'services': {
                    'transcription': {
                        'enabled': True,
                        'adapter': 'minimax-transcription',
                        'endpoint': {
                            'kind': 'endpoint_url',
                            'url': 'https://api.minimax.cn/v1/speech_to_text',
                        },
                    },
                    'speech': {
                        'enabled': True,
                        'adapter': 'minimax-speech',
                        'endpoint': {'kind': 'base_url', 'url': 'https://api.minimax.cn/v1'},
                    },
                },
            },
            'credential': providers.encrypt_credential('synthetic-key'),
            'status': 'configured',
            'models': [],
            'is_active': True,
            'protocol': 'openai-compatible',
        }
        values.update(overrides)
        return new_provider_connection(**values)

    async def test_templates_declare_their_services_and_oauth_media_is_refused(self):
        catalog = (await self.client.get('/providers')).json()
        self.assertEqual(catalog['service_contract_version'], 1)
        templates = {item['id']: item for item in catalog['data']}
        minimax = templates['minimax']
        self.assertEqual(minimax['services']['transcription']['adapters'], ['minimax-transcription'])
        self.assertEqual(minimax['services']['transcription']['model_requirement'], 'defaulted')
        self.assertEqual(minimax['services']['transcription']['discovery'], 'preset')
        self.assertEqual(minimax['services']['transcription']['limits']['max_seconds'], 500)
        self.assertEqual(minimax['services']['image']['adapters'], ['minimax-image'])
        self.assertFalse(minimax['services']['image']['options'].get('edit'))
        self.assertFalse(templates['chatgpt']['services']['speech']['supported'])
        self.assertFalse(templates['kimi']['services']['image']['supported'])
        self.assertFalse(templates['deepseek']['services']['transcription']['supported'])
        self.assertTrue(templates['openai']['services']['transcription']['supported'])
        # The interface never keeps a second opinion about media support.
        self.assertEqual(
            templates['minimax']['services']['transcription']['discovery'],
            'preset',
        )

    async def test_template_identity_survives_a_missing_provider_id(self):
        template = resolve_template('minimax', 'custom')
        self.assertEqual(template['id'], 'minimax')
        self.assertEqual(template['protocol'], 'openai-compatible')
        # The legacy field is only consulted when no template was named.
        self.assertEqual(resolve_template(None, 'deepseek')['id'], 'deepseek')
        self.assertEqual(resolve_template(None, None)['id'], 'custom')

        created = await self.client.post(
            '/providers/connections',
            json={
                'provider_id': 'custom',
                'provider_template': 'minimax',
                'name': 'Media only',
                'api_key': 'synthetic-key',
                'config': {
                    'services': {
                        'transcription': {
                            'enabled': True,
                            'adapter': 'minimax-transcription',
                            'endpoint': {
                                'kind': 'endpoint_url',
                                'url': 'https://api.minimax.cn/v1/speech_to_text',
                            },
                        }
                    }
                },
                'model_selections': [
                    {
                        'upstream_id': 'asr-1.0',
                        'display_name': 'asr-1.0',
                        'uses': {'transcription': 'minimax-transcription'},
                        'source': 'preset',
                    }
                ],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body['provider_id'], 'minimax')
        self.assertEqual(body['provider_template'], 'minimax')
        self.assertEqual(body['models'][0]['uses'], {'transcription': 'minimax-transcription'})
        self.assertEqual(body['models'][0]['source'], 'preset')
        self.assertEqual(body['models'][0]['verification']['transcription']['status'], 'untested')
        # The transcription model never shows up as a chat model.
        chat = (await self.client.get('/providers/models?purpose=chat')).json()['data']
        self.assertEqual([m for m in chat if m['upstream_id'] == 'asr-1.0'], [])
        transcription = (await self.client.get('/providers/models?purpose=transcription')).json()['data']
        self.assertEqual([m['upstream_id'] for m in transcription], ['asr-1.0'])

    async def test_creating_a_media_connection_never_calls_the_model_list(self):
        self.remote_mock.reset_mock()
        created = await self.client.post(
            '/providers/connections',
            json={
                'provider_template': 'minimax',
                'name': 'No list',
                'api_key': 'synthetic-key',
                'config': {
                    'services': {
                        'transcription': {
                            'enabled': True,
                            'adapter': 'minimax-transcription',
                            'endpoint': {
                                'kind': 'endpoint_url',
                                'url': 'https://api.minimax.cn/v1/speech_to_text',
                            },
                        }
                    }
                },
                'model_selections': [
                    {
                        'upstream_id': 'asr-1.0',
                        'uses': {'transcription': 'minimax-transcription'},
                        'source': 'preset',
                    }
                ],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.remote_mock.assert_not_awaited()

    async def test_model_selections_and_model_ids_must_not_disagree(self):
        response = await self.client.post(
            '/providers/connections',
            json={
                'provider_template': 'minimax',
                'name': 'Conflict',
                'api_key': 'synthetic-key',
                'config': {'model_ids': ['asr-1.0']},
                'model_selections': [
                    {'upstream_id': 'other-model', 'uses': {'transcription': 'minimax-transcription'}}
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('model_ids', response.json()['detail'])

    async def test_purposes_are_validated_against_the_template(self):
        connection = self.minimax_connection()
        with self.assertRaises(HTTPException):
            validate_model_uses(connection, {'chat': 'openai-chat', 'image': 'openai-image'})
        with self.assertRaises(HTTPException):
            validate_model_uses(connection, {'transcription': 'openai-speech'})
        self.assertEqual(
            validate_model_uses(connection, {'transcription': 'minimax-transcription'}),
            {'transcription': 'minimax-transcription'},
        )
        oauth = self.minimax_connection(provider_id='chatgpt', provider_template='chatgpt', auth_type='oauth')
        with self.assertRaises(HTTPException):
            validate_model_uses(oauth, {'transcription': 'minimax-transcription'})

    async def test_endpoint_modes_are_resolved_once(self):
        connection = self.minimax_connection()
        service = provider_media.resolve_service(connection, 'transcription', 'minimax-transcription')
        self.assertEqual(service.endpoint, 'https://api.minimax.cn/v1/speech_to_text')
        speech = provider_media.resolve_service(connection, 'speech', 'minimax-speech')
        self.assertEqual(speech.endpoint, 'https://api.minimax.cn/v1/t2a_v2')
        self.assertEqual(speech.endpoint_kind, 'base_url')
        # A legacy connection keeps the OpenAI-compatible path.
        legacy = self.minimax_connection(config={'base_url': 'https://api.openai.com/v1'})
        legacy_service = provider_media.resolve_service(legacy, 'transcription', 'openai-transcription')
        self.assertEqual(legacy_service.endpoint, 'https://api.openai.com/v1/audio/transcriptions')

    async def test_an_invalid_endpoint_is_refused_before_the_request(self):
        connection = self.minimax_connection(
            config={
                'base_url': 'https://api.minimax.chat/v1',
                'services': {
                    'transcription': {
                        'enabled': True,
                        'adapter': 'minimax-transcription',
                        'endpoint': {'kind': 'base_url', 'url': 'https://user:secret@api.minimax.cn/v1'},
                    }
                },
            }
        )
        with self.assertRaises(HTTPException) as error:
            provider_media.resolve_service(connection, 'transcription', 'minimax-transcription')
        self.assertEqual(error.exception.status_code, 400)

    async def test_service_statuses_keep_the_layers_apart(self):
        connection = self.minimax_connection()
        statuses = providers.service_statuses(connection)
        self.assertEqual(statuses['transcription']['catalog'], 'not_applicable')
        self.assertEqual(statuses['transcription']['probe'], 'not_available')
        self.assertEqual(statuses['transcription']['configuration'], 'valid')
        self.assertEqual(statuses['chat']['catalog'], 'unknown')

    async def test_a_disabled_service_hides_its_models(self):
        connection = self.minimax_connection(
            config={
                'base_url': 'https://api.minimax.chat/v1',
                'services': {
                    'transcription': {
                        'enabled': False,
                        'adapter': 'minimax-transcription',
                        'endpoint': {
                            'kind': 'endpoint_url',
                            'url': 'https://api.minimax.cn/v1/speech_to_text',
                        },
                    }
                },
            }
        )
        self.assertFalse(providers.service_enabled(connection, 'transcription'))
        self.assertFalse(providers.service_enabled(connection, 'speech'))

    async def test_preset_services_do_not_fetch_a_catalog(self):
        connection = self.minimax_connection()
        async with get_async_db_context() as db:
            db.add(connection)
            await db.commit()
        response = await self.client.post(
            f'/providers/connections/{connection.id}/models/fetch?purpose=transcription'
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'not_applicable')
        self.assertIsNone(body['error'])
        self.assertEqual([c['upstream_id'] for c in body['candidates']], ['asr-1.0'])
        self.remote_mock.assert_not_awaited()

    async def test_the_image_service_declares_the_operations_it_supports(self):
        templates = {template['id']: template for template in public_templates()}
        # A generate-only adapter must never look like an editing adapter.
        self.assertEqual(templates['minimax']['services']['image']['operations'], ['generate'])
        self.assertEqual(
            sorted(templates['openai']['services']['image']['operations']),
            ['edit', 'generate'],
        )
        # A service with a single operation does not need the field.
        self.assertNotIn('operations', templates['minimax']['services']['transcription'])

        connection = self.minimax_connection()
        resolved = providers.resolved_services(connection)
        self.assertEqual(resolved['image']['operations'], ['generate'])
        self.assertEqual(resolved['transcription']['operations'], [])

    async def test_templates_are_shared_with_the_catalog(self):
        self.assertEqual(
            [template['id'] for template in public_templates()],
            [template['id'] for template in public_templates()],
        )
        self.assertTrue(
            all('services' in template for template in public_templates()),
        )
        declaration = service_declaration(
            SimpleNamespace(
                id='x',
                provider_id='minimax',
                provider_template='minimax',
                auth_type='api_key',
                config={},
                models=[],
            ),
            'transcription',
        )
        self.assertTrue(declaration['supported'])
        self.assertEqual(declaration['preset_models'][0]['uses'], {'transcription': 'minimax-transcription'})
