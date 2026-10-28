"""MiniMax native media adapters: request shape, limits and business failures."""

import base64
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from open_webui.routers import audio
from open_webui.utils import provider_media, provider_minimax, providers


class _Content:
    """The part of an aiohttp response the bounded reader touches."""

    def __init__(self, body: bytes):
        self._body = body

    def iter_chunked(self, _size):
        async def generator():
            if self._body:
                yield self._body

        return generator()


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'', headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.content = _Content(body)

    def json(self, **_kwargs):
        import json

        return json.loads(self.content._body or b'{}')


class FakeSession:
    """A session that records every call and answers with one response."""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        session = self

        class _Context:
            async def __aenter__(self):
                return session.response

            async def __aexit__(self, *_args):
                return False

        return _Context()


class CapturingFormData:
    """A multipart body that keeps the fields readable for the assertions."""

    def __init__(self, *args, **kwargs):
        self.fields = {}

    def add_field(self, name, value, **_kwargs):
        self.fields[name] = value


class MiniMaxAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = SimpleNamespace(id='minimax-owner', role='admin')
        self.connection = SimpleNamespace(
            id='minimax-connection',
            provider_id='minimax',
            provider_template='minimax',
            auth_type='api_key',
            owner_id=self.owner.id,
            visibility='private',
            is_active=True,
            credential=providers.encrypt_credential('saved-key'),
            config={
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
                    'image': {
                        'enabled': True,
                        'adapter': 'minimax-image',
                        'endpoint': {'kind': 'base_url', 'url': 'https://api.minimax.cn/v1'},
                    },
                },
            },
        )

    def service(self, purpose, adapter=None):
        return provider_media.resolve_service(self.connection, purpose, adapter)

    async def call(self, purpose, payload, response, adapter=None):
        session = FakeSession(response)

        @asynccontextmanager
        async def session_scope(connection, **_options):
            yield session

        with (
            patch.object(providers, 'media_http_session', side_effect=session_scope),
            patch.object(provider_media.aiohttp, 'FormData', CapturingFormData),
        ):
            result = await provider_media.call_media_service(
                self.connection,
                self.service(purpose, adapter),
                model='asr-1.0' if purpose == 'transcription' else 'model-under-test',
                credential='saved-key',
                payload=payload,
            )
        return result, session

    async def test_transcription_sends_the_model_the_file_and_the_language_header(self):
        response = FakeResponse(200, b'{"text": "hello", "duration": 1.5}')
        result, session = await self.call(
            'transcription',
            {
                'audio': b'audio-bytes',
                'filename': 'sample.mp3',
                'duration': 1.5,
                'language': 'zh',
                'response_format': 'verbose_json',
            },
            response,
        )
        self.assertEqual(result['text'], 'hello')
        self.assertEqual(result['duration'], 1.5)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        # The complete address is used verbatim, exactly once.
        self.assertEqual(call['url'], 'https://api.minimax.cn/v1/speech_to_text')
        self.assertNotIn('/audio/transcriptions', call['url'])
        self.assertNotIn('/models', call['url'])
        self.assertEqual(call['headers']['Authorization'], 'Bearer saved-key')
        self.assertEqual(call['headers']['Language'], 'zh')
        fields = call['data'].fields
        self.assertEqual(fields['model'], 'asr-1.0')
        self.assertEqual(fields['response_format'], 'verbose_json')
        self.assertIn('file', fields)
        # The language never travels as a form field.
        self.assertNotIn('language', fields)

    async def test_transcription_refuses_audio_the_service_cannot_take(self):
        service = self.service('transcription', 'minimax-transcription')
        with self.assertRaises(HTTPException) as too_long:
            provider_media.enforce_audio_limits(service, b'audio', 501, 'sample.mp3')
        self.assertEqual(too_long.exception.status_code, 400)
        with self.assertRaises(HTTPException) as too_big:
            provider_media.enforce_audio_limits(service, b'x' * (50 * 1024 * 1024 + 1), 1, 'sample.mp3')
        self.assertEqual(too_big.exception.status_code, 400)
        with self.assertRaises(HTTPException) as wrong_format:
            provider_media.enforce_audio_limits(service, b'audio', 1, 'sample.webm')
        self.assertEqual(wrong_format.exception.status_code, 400)
        # 500 seconds is still accepted.
        provider_media.enforce_audio_limits(service, b'audio', 500, 'sample.mp3')

    async def test_transcription_keeps_the_verbose_segments(self):
        body = (
            b'{"text": "hi", "segments": [{"text": "hi", "start": 0, "end": 1, "speaker": "S1"}]}'
        )
        result, _ = await self.call(
            'transcription',
            {'audio': b'audio', 'filename': 'sample.mp3', 'duration': 1},
            FakeResponse(200, body),
        )
        self.assertEqual(result['segments'][0]['speaker'], 'S1')
        self.assertEqual(result['segments'][0]['start'], 0)

    async def test_speech_decodes_the_hex_audio(self):
        audio_hex = base64.b16encode(b'mp3-bytes').decode().lower()
        body = ('{"base_resp": {"status_code": 0}, "data": {"audio": "%s"}}' % audio_hex).encode()
        result, session = await self.call(
            'speech', {'text': 'hello', 'voice': 'voice-id'}, FakeResponse(200, body)
        )
        self.assertEqual(result['audio'], b'mp3-bytes')
        self.assertEqual(result['content_type'], 'audio/mpeg')
        call = session.calls[0]
        self.assertEqual(call['url'], 'https://api.minimax.cn/v1/t2a_v2')
        self.assertEqual(call['json']['model'], 'model-under-test')
        self.assertEqual(call['json']['text'], 'hello')
        self.assertEqual(call['json']['voice_setting'], {'voice_id': 'voice-id'})
        self.assertEqual(call['json']['audio_setting']['format'], 'mp3')
        self.assertEqual(call['json']['output_format'], 'hex')
        self.assertFalse(call['json']['stream'])
        self.assertNotIn('output_format', call['json']['audio_setting'])
        self.assertNotIn('stream', call['json']['audio_setting'])

    async def test_speech_refuses_a_business_failure_inside_a_http_200(self):
        body = b'{"base_resp": {"status_code": 1004, "status_msg": "invalid api key"}}'
        with self.assertRaises(HTTPException) as failure:
            await self.call('speech', {'text': 'hello', 'voice': 'voice-id'}, FakeResponse(200, body))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(failure.exception.provider_reason, 'invalid_credentials')
        self.assertEqual(failure.exception.provider_upstream_http_status, 200)
        self.assertEqual(failure.exception.provider_upstream_code, '1004')
        self.assertEqual(failure.exception.provider_category, 'authentication')

    async def test_speech_refuses_audio_that_is_not_hexadecimal(self):
        body = b'{"base_resp": {"status_code": 0}, "data": {"audio": "not-hex-at-all"}}'
        with self.assertRaises(HTTPException) as failure:
            await self.call('speech', {'text': 'hello', 'voice': 'voice-id'}, FakeResponse(200, body))
        self.assertEqual(failure.exception.status_code, 502)
        self.assertEqual(failure.exception.provider_reason, 'upstream_failure')

    async def test_image_parses_base64_and_urls(self):
        body = (
            b'{"base_resp": {"status_code": 0}, "data": {"image_base64": ["aGVsbG8="]},'
            b' "metadata": {"success_count": 1, "failed_count": 0}}'
        )
        result, session = await self.call(
            'image',
            {'prompt': 'a circle', 'n': 1, 'width': 1024, 'height': 1024},
            FakeResponse(200, body),
        )
        self.assertEqual(result['images'], [{'url': None, 'b64': 'aGVsbG8='}])
        self.assertFalse(result['partial'])
        call = session.calls[0]
        self.assertEqual(call['url'], 'https://api.minimax.cn/v1/image_generation')
        self.assertEqual(call['json']['model'], 'model-under-test')
        self.assertEqual(call['json']['prompt'], 'a circle')
        self.assertEqual(call['json']['width'], 1024)
        self.assertEqual(call['json']['height'], 1024)
        self.assertNotIn('size', call['json'])

    async def test_image_reports_a_partial_result(self):
        body = (
            b'{"base_resp": {"status_code": 0}, "data": {"image_urls": ["https://cdn.example/a.png"]},'
            b' "metadata": {"success_count": 1, "failed_count": 1}}'
        )
        result, _ = await self.call(
            'image',
            {'prompt': 'a circle', 'n': 2, 'width': 1024, 'height': 1024},
            FakeResponse(200, body),
        )
        self.assertTrue(result['partial'])
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(result['images'][0]['url'], 'https://cdn.example/a.png')

    async def test_image_refuses_an_unsupported_size_before_the_request(self):
        service = self.service('image', 'minimax-image')
        with self.assertRaises(HTTPException) as error:
            provider_media.enforce_image_limits(service, {'prompt': 'x', 'n': 1, 'width': 100, 'height': 100})
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as error:
            provider_media.enforce_image_limits(service, {'prompt': 'x' * 2000, 'n': 1})
        self.assertEqual(error.exception.status_code, 400)

    async def test_image_editing_is_not_implied_by_generation(self):
        from open_webui.utils.provider_capabilities import adapter_supports_operation

        self.assertTrue(adapter_supports_operation('openai-image', 'edit'))
        self.assertFalse(adapter_supports_operation('minimax-image', 'edit'))
        self.assertFalse(adapter_supports_operation('siliconflow-image', 'edit'))

    async def test_the_daily_speech_path_uses_the_native_endpoint(self):
        import tempfile
        from pathlib import Path

        audio_hex = base64.b16encode(b'mp3-bytes').decode().lower()
        body = ('{"base_resp": {"status_code": 0}, "data": {"audio": "%s"}}' % audio_hex).encode()
        session = FakeSession(FakeResponse(200, body))
        resolved = SimpleNamespace(
            connection=self.connection,
            upstream_model_id='speech-2.8-hd',
            model_metadata={'uses': {'speech': 'minimax-speech'}},
        )
        config = {
            'audio.tts.model': 'provider:minimax-connection:speech-2.8-hd',
            'audio.tts.voice': 'voice-id',
            'audio.tts.provider_connection_id': 'minimax-connection',
        }

        async def get_config(key, *args):
            return config.get(key)

        @asynccontextmanager
        async def session_scope(connection, **_options):
            yield session

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (
                patch.object(audio.Config, 'get', new=get_config),
                patch.object(
                    audio, 'get_media_api_connection', new=AsyncMock(return_value=(self.connection, 'saved-key'))
                ),
                patch.object(audio, 'resolve_configured_media', new=AsyncMock(return_value=resolved)),
                patch.object(providers, 'media_http_session', side_effect=session_scope),
                patch.object(audio, 'USE_SLIM', True),
            ):
                await audio._tts_provider(
                    None, {'input': 'hello'}, path / 'speech.mp3', path / 'speech.json', self.owner
                )
            self.assertEqual((path / 'speech.mp3').read_bytes(), b'mp3-bytes')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]['url'], 'https://api.minimax.cn/v1/t2a_v2')
        self.assertEqual(session.calls[0]['json']['voice_setting'], {'voice_id': 'voice-id'})

    async def test_the_voice_list_comes_from_the_provider(self):
        session = FakeSession(
            FakeResponse(
                200,
                b'{"base_resp": {"status_code": 0}, "data": [{"voice_id": "v1", "name": "Voice One"}]}',
            )
        )

        @asynccontextmanager
        async def session_scope(connection, **_options):
            yield session

        with patch.object(providers, 'media_http_session', side_effect=session_scope):
            voices = await provider_media.fetch_voices(
                self.connection, self.service('speech', 'minimax-speech'), 'saved-key', 'system'
            )
        self.assertEqual(voices, [{'id': 'v1', 'name': 'Voice One', 'source': 'system'}])
        self.assertEqual(session.calls[0]['url'], 'https://api.minimax.cn/v1/get_voice')
        self.assertEqual(session.calls[0]['json'], {'voice_type': 'system'})

    async def test_a_service_without_a_voice_list_says_so_locally(self):
        connection = SimpleNamespace(
            **{
                **self.connection.__dict__,
                'config': {'base_url': 'https://api.openai.com/v1'},
            }
        )
        service = provider_media.resolve_service(connection, 'speech', 'openai-speech')
        with self.assertRaises(HTTPException) as error:
            await provider_media.fetch_voices(connection, service, 'saved-key')
        self.assertEqual(error.exception.status_code, 400)

    def test_the_business_codes_map_onto_the_error_taxonomy(self):
        self.assertEqual(provider_minimax.failure_reason({'code': '1004'})[0], 'invalid_credentials')
        self.assertEqual(provider_minimax.failure_reason({'code': '2049'})[0], 'invalid_credentials')
        self.assertEqual(provider_minimax.failure_reason({'code': '1008'})[0], 'quota_exhausted')
        self.assertEqual(provider_minimax.failure_reason({'code': '1002'})[0], 'rate_limited')
        self.assertEqual(provider_minimax.failure_reason({'code': '2013'})[0], 'invalid_parameters')
        self.assertEqual(provider_minimax.failure_reason({'code': '9999'})[0], 'unknown')

    def test_the_model_is_always_sent_even_when_it_has_a_default(self):
        self.assertEqual(provider_minimax.transcription_fields()['model'], 'asr-1.0')
        self.assertEqual(provider_minimax.transcription_fields(model='asr-1.0')['model'], 'asr-1.0')
        self.assertNotIn('response_format', provider_minimax.transcription_fields())

    def test_the_speech_body_never_streams(self):
        payload = provider_minimax.speech_payload(model='speech-2.8-hd', text='hi', voice='v')
        self.assertFalse(payload['stream'])
        self.assertEqual(payload['output_format'], 'hex')
        self.assertNotIn('stream', payload['audio_setting'])
        self.assertNotIn('output_format', payload['audio_setting'])
        self.assertEqual(payload['voice_setting'], {'voice_id': 'v'})
