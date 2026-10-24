"""Media routes must use saved keys without bypassing connection permissions."""

import base64
import io
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from open_webui.routers import audio, images
from open_webui.utils import providers
from PIL import Image


@asynccontextmanager
async def fake_session(session):
    yield session


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(id='media-owner', role='admin')
        self.connection = SimpleNamespace(
            id='media-connection',
            provider_id='openai',
            auth_type='api_key',
            owner_id=self.owner.id,
            visibility='private',
            is_active=True,
            credential=providers.encrypt_credential('saved-key'),
            config={},
        )

        async def resolve(connection_id, model_id, user, purpose):
            return SimpleNamespace(
                connection=self.connection,
                upstream_model_id=model_id,
                model_metadata={'uses': {purpose: 'openai-image' if purpose == 'image' else 'openai-speech'}},
            )

        for module in (audio, images):
            scoped = patch.object(module, 'resolve_configured_media', side_effect=resolve)
            scoped.start()
            self.addCleanup(scoped.stop)

    async def test_media_resolver_rejects_private_oauth_and_missing_connections(self):
        db = SimpleNamespace(get=AsyncMock(return_value=self.connection))
        connection, key = await providers.get_media_api_connection(self.connection.id, self.owner, db)
        self.assertIs(connection, self.connection)
        self.assertEqual(key, 'saved-key')
        with self.assertRaises(HTTPException) as denied:
            await providers.get_media_api_connection(self.connection.id, SimpleNamespace(id='other', role='admin'), db)
        self.assertEqual(denied.exception.status_code, 404)
        self.connection.auth_type = 'oauth'
        with self.assertRaises(HTTPException) as oauth:
            await providers.get_media_api_connection(self.connection.id, self.owner, db)
        self.assertEqual(oauth.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            await providers.get_media_api_connection('', self.owner, db)

    async def test_model_listing_never_generates_an_image(self):
        config = SimpleNamespace(
            IMAGE_GENERATION_ENGINE='provider',
            IMAGES_PROVIDER_CONNECTION_ID='saved',
            IMAGE_GENERATION_MODEL='gpt-image-test',
        )
        with (
            patch.object(images, 'get_image_config', new=AsyncMock(return_value=config)),
            patch.object(
                images, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))
            ),
            patch.object(images, 'get_session', new=AsyncMock()) as session,
        ):
            result = await images.get_models(None, self.owner)
        self.assertEqual(result, [{'id': 'gpt-image-test', 'name': 'gpt-image-test'}])
        session.assert_not_awaited()

    async def test_image_request_cannot_override_the_admin_media_model(self):
        config = SimpleNamespace(
            IMAGE_GENERATION_ENGINE='provider',
            IMAGES_PROVIDER_CONNECTION_ID='saved',
            IMAGE_GENERATION_MODEL='allowed-image',
            IMAGE_SIZE='1024x1024',
        )
        with (
            patch.object(images, 'get_image_config', new=AsyncMock(return_value=config)),
            patch.object(
                images, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))
            ),
            patch.object(images, 'media_http_session') as session,
        ):
            with self.assertRaises(HTTPException) as denied:
                await images.image_generations(
                    None, images.CreateImageForm(prompt='test', model='other-model'), user=self.owner
                )
            self.assertEqual(denied.exception.status_code, 400)
            session.assert_not_called()

    async def test_generation_and_edit_forward_saved_key_and_store_image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (1, 1)).save(buffer, format='PNG')
        encoded = base64.b64encode(buffer.getvalue()).decode()
        for editing in (False, True):
            config = SimpleNamespace(
                IMAGE_GENERATION_ENGINE='provider',
                IMAGES_PROVIDER_CONNECTION_ID='saved',
                IMAGE_GENERATION_MODEL='gpt-image-test',
                IMAGE_SIZE='1024x1024',
                IMAGE_EDIT_ENGINE='provider',
                IMAGES_EDIT_PROVIDER_CONNECTION_ID='saved',
                IMAGE_EDIT_MODEL='gpt-image-test',
                IMAGE_EDIT_SIZE='1024x1024',
            )
            response = SimpleNamespace(
                status=200, raise_for_status=Mock(), json=AsyncMock(return_value={'data': [{'b64_json': encoded}]})
            )

            @asynccontextmanager
            async def response_context():
                yield response

            session = SimpleNamespace(post=Mock(return_value=response_context()))
            upload = AsyncMock(return_value=(None, {'id': 'generated', 'url': '/files/generated'}))
            with (
                patch.object(images, 'get_image_config', new=AsyncMock(return_value=config)),
                patch.object(
                    images, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))
                ),
                patch.object(images, 'media_http_session', side_effect=lambda c: fake_session(session)),
                patch.object(images, 'upload_image', new=upload),
                patch.object(images, 'ENABLE_FORWARD_USER_INFO_HEADERS', False),
            ):
                if editing:
                    result = await images.image_edits(
                        None,
                        images.EditImageForm(image='data:image/png;base64,' + encoded, prompt='edit'),
                        user=self.owner,
                    )
                else:
                    result = await images.image_generations(
                        None, images.CreateImageForm(prompt='generate'), user=self.owner
                    )
            self.assertEqual(result[0]['id'], 'generated')
            kwargs = session.post.call_args.kwargs
            self.assertEqual(kwargs['headers']['Authorization'], 'Bearer saved-key')
            self.assertFalse(kwargs['allow_redirects'])
            self.assertTrue(kwargs['url'].endswith('/images/edits' if editing else '/images/generations'))
            self.assertEqual(upload.await_args.args[1], buffer.getvalue())

    async def test_siliconflow_image_uses_native_payload_and_response(self):
        self.connection.provider_id = 'siliconflow'
        self.connection.config = {'base_url': 'https://api.siliconflow.cn/v1'}
        config = SimpleNamespace(
            IMAGE_GENERATION_ENGINE='provider',
            IMAGES_PROVIDER_CONNECTION_ID='saved',
            IMAGE_GENERATION_MODEL='image-model',
            IMAGE_SIZE='1024x1024',
        )
        resolved = SimpleNamespace(
            connection=self.connection,
            upstream_model_id='image-model',
            model_metadata={'uses': {'image': 'siliconflow-image'}},
        )
        response = SimpleNamespace(
            status=200,
            raise_for_status=Mock(),
            json=AsyncMock(
                return_value={'images': [{'url': 'https://api.siliconflow.cn/generated.png'}]}
            ),
        )

        @asynccontextmanager
        async def response_context():
            yield response

        session = SimpleNamespace(post=Mock(return_value=response_context()))
        upload = AsyncMock(return_value=(None, {'id': 'generated', 'url': '/files/generated'}))
        image_data = AsyncMock(return_value=(b'png-data', 'image/png'))
        with (
            patch.object(images, 'get_image_config', new=AsyncMock(return_value=config)),
            patch.object(images, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))),
            patch.object(
                images,
                'resolve_configured_media',
                new=AsyncMock(return_value=resolved),
            ),
            patch.object(images, 'media_http_session', side_effect=lambda c: fake_session(session)),
            patch.object(images, 'get_image_data', new=image_data),
            patch.object(images, 'upload_image', new=upload),
            patch.object(images, 'ENABLE_FORWARD_USER_INFO_HEADERS', False),
        ):
            result = await images.image_generations(
                None,
                images.CreateImageForm(prompt='generate', size='768x1024'),
                user=self.owner,
            )

        self.assertEqual(result[0]['id'], 'generated')
        request = session.post.call_args.kwargs
        self.assertEqual(
            request['json'],
            {
                'model': 'image-model',
                'prompt': 'generate',
                'image_size': '768x1024',
            },
        )
        image_data.assert_awaited_once()
        upload.assert_awaited_once()
        self.assertEqual(upload.await_args.args[1], b'png-data')

    async def test_siliconflow_compatible_asr_omits_unsupported_fields(self):
        self.connection.provider_id = 'custom'
        self.connection.config = {'base_url': 'https://api.siliconflow.cn/v1'}
        resolved = SimpleNamespace(
            connection=self.connection,
            upstream_model_id='asr-model',
            model_metadata={'uses': {'transcription': 'openai-transcription'}},
        )
        response = SimpleNamespace(
            status=200,
            raise_for_status=Mock(),
            read=AsyncMock(return_value=b''),
            json=AsyncMock(return_value={'text': 'hello'}),
        )
        session = SimpleNamespace(post=AsyncMock(return_value=response))

        class CapturingFormData:
            def __init__(self):
                self.fields = {}

            def add_field(self, name, value, **kwargs):
                self.fields[name] = value

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(audio, 'resolve_media_model', new=AsyncMock(return_value=resolved)),
            patch.object(
                audio,
                'get_media_api_connection',
                new=AsyncMock(return_value=(self.connection, 'saved-key')),
            ),
            patch.object(audio, 'media_http_session', side_effect=lambda c: fake_session(session)),
            patch.object(audio.aiohttp, 'FormData', CapturingFormData),
            patch.object(audio, 'ENABLE_FORWARD_USER_INFO_HEADERS', False),
        ):
            path = Path(directory)
            audio_path = path / 'speech.mp3'
            audio_path.write_bytes(b'audio')
            result = await audio._transcribe_openai(
                None,
                str(audio_path),
                'speech.mp3',
                ['zh-CN', 'en'],
                str(path),
                'siliconflow-transcript',
                self.owner,
                use_provider=True,
                response_format='verbose_json',
                model_ref='provider:media-connection:asr-model',
            )

        self.assertEqual(result, {'text': 'hello'})
        form_data = session.post.await_args.kwargs['data']
        self.assertEqual(form_data.fields['model'], 'asr-model')
        self.assertIn('file', form_data.fields)
        self.assertNotIn('language', form_data.fields)
        self.assertNotIn('response_format', form_data.fields)
        self.assertEqual(session.post.await_count, 1)

    async def test_siliconflow_speech_uses_model_scoped_voice(self):
        self.connection.provider_id = 'siliconflow'
        self.connection.config = {'base_url': 'https://api.siliconflow.cn/v1'}
        config = {
            'audio.tts.model': 'speech-model',
            'audio.tts.voice': 'speech-model:alex',
            'audio.tts.provider_connection_id': 'saved',
        }

        async def get_config(key, *args):
            return config.get(key)

        response = SimpleNamespace(
            status=200,
            headers={'Content-Type': 'audio/mpeg'},
            raise_for_status=Mock(),
            read=AsyncMock(return_value=b'audio-bytes'),
        )
        session = SimpleNamespace(post=AsyncMock(return_value=response))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(audio.Config, 'get', new=get_config),
            patch.object(audio, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))),
            patch.object(audio, 'media_http_session', side_effect=lambda c: fake_session(session)),
            patch.object(audio, 'ENABLE_FORWARD_USER_INFO_HEADERS', False),
            patch.object(audio, 'USE_SLIM', True),
        ):
            path = Path(directory)
            await audio._tts_provider(
                None,
                {'input': 'hello'},
                path / 'speech.mp3',
                path / 'speech.json',
                self.owner,
            )

        request = session.post.await_args.kwargs
        self.assertEqual(request['url'], 'https://api.siliconflow.cn/v1/audio/speech')
        self.assertEqual(request['json']['model'], 'speech-model')
        self.assertEqual(request['json']['voice'], 'speech-model:alex')

    async def test_tts_and_transcription_use_saved_key_and_do_not_fall_back(self):
        config = {
            'audio.tts.model': 'tts-test',
            'audio.tts.voice': 'alloy',
            'audio.tts.provider_connection_id': 'saved',
            'audio.stt.model': 'stt-test',
            'audio.stt.provider_connection_id': 'saved',
        }

        async def get_config(key, *args):
            return config.get(key)

        response = SimpleNamespace(
            status=200,
            headers={'Content-Type': 'audio/mpeg'},
            raise_for_status=Mock(),
            read=AsyncMock(return_value=b'audio-bytes'),
            json=AsyncMock(return_value={'text': 'hello'}),
        )
        session = SimpleNamespace(post=AsyncMock(return_value=response))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(audio.Config, 'get', new=get_config),
            patch.object(audio, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))),
            patch.object(audio, 'media_http_session', side_effect=lambda c: fake_session(session)),
            patch.object(audio, 'ENABLE_FORWARD_USER_INFO_HEADERS', False),
            patch.object(audio, 'USE_SLIM', True),
        ):
            path = Path(directory)
            await audio._tts_provider(None, {'input': 'hello'}, path / 'speech.mp3', path / 'speech.json', self.owner)
            self.assertEqual((path / 'speech.mp3').read_bytes(), b'audio-bytes')
            self.assertEqual(session.post.await_args.kwargs['json']['model'], 'tts-test')
            result = await audio._transcribe_openai(
                None,
                str(path / 'speech.mp3'),
                'speech.mp3',
                [None],
                str(path),
                'transcript',
                self.owner,
                use_provider=True,
            )
            self.assertEqual(result, {'text': 'hello'})
            self.assertTrue(session.post.await_args.kwargs['url'].endswith('/audio/transcriptions'))
            self.assertEqual(session.post.await_args.kwargs['headers']['Authorization'], 'Bearer saved-key')
            self.assertFalse(session.post.await_args.kwargs['allow_redirects'])
            self.assertNotIn('saved-key', (path / 'speech.json').read_text())
            with patch.object(
                audio, 'get_media_api_connection', new=AsyncMock(side_effect=HTTPException(404, 'revoked'))
            ):
                with self.assertRaises(HTTPException) as revoked:
                    await audio._transcribe_openai(
                        None, 'unused', 'unused', [None], str(path), 'denied', self.owner, use_provider=True
                    )
                self.assertEqual(revoked.exception.status_code, 404)

    async def test_cached_speech_still_requires_current_connection_access(self):
        async def config(key, *args):
            return {'audio.tts.engine': 'provider', 'audio.tts.provider_connection_id': 'saved'}.get(key)

        with (
            patch.object(audio.Config, 'get', new=config),
            patch.object(audio, 'get_media_api_connection', new=AsyncMock(side_effect=HTTPException(404, 'revoked'))),
            patch.object(Path, 'is_file', return_value=True),
        ):
            with self.assertRaises(HTTPException) as denied:
                await audio.speech(
                    SimpleNamespace(body=AsyncMock(return_value=json.dumps({'input': 'cached'}).encode())), self.owner
                )
        self.assertEqual(denied.exception.status_code, 404)
