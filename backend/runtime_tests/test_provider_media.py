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
from PIL import Image
from open_webui.routers import audio, images
from open_webui.utils import providers


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
            patch.object(
                audio, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))
            ),
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
