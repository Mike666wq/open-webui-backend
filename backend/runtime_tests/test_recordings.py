"""Exercise durable jobs with real SQLite and synthetic ASR, without billing."""

import asyncio
import io
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.notes import Note
from open_webui.models.recordings import Recording
from open_webui.routers import recordings as api
from open_webui.utils import recordings as service
from open_webui.utils.auth import get_admin_user, get_verified_user
from sqlalchemy import delete


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_patch = patch.object(service, 'ROOT', Path(self.temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.owner = SimpleNamespace(id='recording-owner', role='admin')
        self.other = SimpleNamespace(id='recording-other', role='admin')
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/api/v1/recordings')
        self.app.dependency_overrides[get_verified_user] = lambda: self.owner
        self.app.dependency_overrides[get_admin_user] = lambda: self.owner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        async with get_async_db_context() as db:
            await db.execute(delete(Recording))
            await db.execute(delete(Note).where(Note.user_id.in_([self.owner.id, self.other.id])))
            await db.commit()
        await Config.upsert(
            {
                'notes.enable': True,
                'audio.stt.engine': 'openai',
                'audio.stt.model': 'synthetic-asr',
                'audio.stt.openai.api_key': 'synthetic-key',
                'audio.stt.openai.api_base_url': 'https://asr.invalid/v1',
                'recordings.max_size_mb': 500,
                'recordings.max_duration_seconds': 7200,
                'recordings.response_format': 'json',
                'rag.file.max_size': None,
            }
        )
        self.user_patch = patch.object(service.Users, 'get_user_by_id', new=AsyncMock(return_value=self.owner))
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)

    def wav(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000)
        return buffer.getvalue()

    async def upload(self, key=None, body=None):
        return await self.client.post(
            '/api/v1/recordings',
            headers={'Idempotency-Key': key or str(uuid.uuid4())},
            files={'file': ('class.wav', self.wav() if body is None else body, 'audio/wav')},
        )

    async def create_job(self, duration=3000):
        job_id = str(uuid.uuid4())
        folder = service.directory(job_id)
        folder.mkdir(parents=True)
        (folder / 'source').write_bytes(self.wav())
        async with get_async_db_context() as db:
            job = Recording(
                id=job_id,
                user_id=self.owner.id,
                title='课堂录音',
                state='queued',
                request_key=job_id,
                duration=duration,
                size_bytes=32044,
                source_present=1,
                lease_until=0,
                note_id=str(uuid.uuid4()),
                created_at=service.now(),
                updated_at=service.now(),
                data={
                    'language': 'zh',
                    'config': await service.references(),
                    'response_format': (await service.settings())['response_format'],
                    'chunks': [],
                },
            )
            db.add(job)
            await db.commit()
        return job_id

    async def row(self, id):
        async with get_async_db_context() as db:
            return await db.get(Recording, id)

    async def test_upload_probe_idempotency_and_private_audio(self):
        key = str(uuid.uuid4())
        first = await self.upload(key)
        self.assertEqual(first.status_code, 202, first.text)
        id = first.json()['id']
        second = await self.upload(key)
        self.assertEqual(second.json()['id'], id)
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 1)
        result = await self.client.get(f'/api/v1/recordings/{id}/audio')
        self.assertEqual(result.content, self.wav())
        ranged = await self.client.get(f'/api/v1/recordings/{id}/audio', headers={'Range': 'bytes=0-43'})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, self.wav()[:44])
        self.assertIn('private', ranged.headers['cache-control'])
        self.app.dependency_overrides[get_verified_user] = lambda: self.other
        for suffix in ('', '/audio'):
            self.assertEqual((await self.client.get(f'/api/v1/recordings/{id}{suffix}')).status_code, 404)
        self.assertEqual((await self.client.post(f'/api/v1/recordings/{id}/cancel')).status_code, 404)
        self.assertEqual((await self.client.get('/api/v1/recordings')).json(), [])

    async def test_invalid_upload_duration_and_effective_size_limit(self):
        self.assertEqual((await self.upload(body=b'not audio')).status_code, 400)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        with patch.object(service, 'probe', new=AsyncMock(return_value=7201)):
            self.assertEqual((await self.upload()).status_code, 413)
        await Config.upsert({'rag.file.max_size': 0.5})
        self.assertEqual((await self.upload(body=b'x' * (1024 * 1024))).status_code, 413)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_unconfigured_asr_does_not_start_local_models(self):
        await Config.upsert({'audio.stt.engine': ''})
        config = await self.client.get('/api/v1/recordings/config')
        self.assertFalse(config.json()['configured'])
        self.assertEqual((await self.upload()).status_code, 400)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_permissions_are_checked_before_upload_and_each_operation(self):
        self.owner.role = 'user'
        with patch.object(service, 'has_permission', new=AsyncMock(return_value=False)):
            self.assertEqual((await self.upload()).status_code, 403)
        self.owner.role = 'admin'
        id = await self.create_job()
        await Config.upsert({'notes.enable': False})
        self.assertEqual((await self.client.get(f'/api/v1/recordings/{id}')).status_code, 403)

    async def test_completed_task_saves_one_note_and_preserves_raw_snapshot(self):
        id = await self.create_job(1)
        claim = await service.claim()
        self.assertEqual(claim[0], id)
        adapter = SimpleNamespace(
            transcribe=AsyncMock(
                return_value={
                    'text': '老师讲课。',
                    'segments': [{'text': '老师讲课。', 'start': 0, 'end': 1, 'speaker': '0'}],
                }
            )
        )
        await service.run_job(self.app, *claim, adapter)
        row = await self.row(id)
        self.assertEqual(row.state, 'completed')
        result = service.public(row)
        self.assertTrue(result['capabilities']['timestamps'])
        self.assertEqual(result['segments'][0]['speaker'], 'part-1:0')
        async with get_async_db_context() as db:
            note = await db.get(Note, row.note_id)
            self.assertEqual(note.data['content']['md'], '老师讲课。')
        names = await self.client.patch(f'/api/v1/recordings/{id}/speakers', json={'names': {'part-1:0': '老师'}})
        self.assertEqual(names.json()['speaker_names']['part-1:0'], '老师')
        summary_request = {'model': 'text-model', 'template': 'class', 'request_key': str(uuid.uuid4())}
        with patch.object(api, 'complete_text', new=AsyncMock(return_value='课堂笔记')) as complete:
            summary = await self.client.post(f'/api/v1/recordings/{id}/summary', json=summary_request)
            self.assertEqual(summary.status_code, 200, summary.text)
            second = await self.client.post(f'/api/v1/recordings/{id}/summary', json=summary_request)
            self.assertEqual(summary.json(), second.json())
            self.assertEqual(complete.await_count, 1)
        async with get_async_db_context() as db:
            note = await db.get(Note, row.note_id)
            self.assertEqual(note.data['content']['md'], '老师讲课。')
        self.assertEqual((await self.client.delete(f'/api/v1/recordings/{id}/audio')).status_code, 200)
        self.assertEqual((await self.client.get(f'/api/v1/recordings/{id}/audio')).status_code, 404)
        self.assertEqual(service.public(await self.row(id))['text'], '老师讲课。')
        self.assertEqual((await self.client.post(f'/api/v1/recordings/{id}/retry')).status_code, 409)

    async def test_long_audio_failure_checkpoint_and_manual_resume(self):
        id = await self.create_job(3000)  # A 50-minute class, five bounded chunks.
        calls = []

        async def response(request, path, job, user):
            calls.append(path)
            if len(calls) == 2:
                raise TimeoutError()
            return {
                'text': f'第{len(calls)}段',
                'segments': [{'text': f'第{len(calls)}段', 'start': 0, 'end': 1, 'speaker': '0'}],
            }

        adapter = SimpleNamespace(transcribe=response)
        with patch.object(service, 'command', new=AsyncMock(return_value=b'')):
            await service.run_job(self.app, *(await service.claim()), adapter)
            row = await self.row(id)
            self.assertEqual(row.state, 'failed')
            self.assertIn('provider transcription (segment 2)', row.data['error'])
            self.assertIn('request timed out', row.data['error'])
            self.assertEqual(len(row.data['chunks']), 1)
            self.assertEqual(len(calls), 2)  # No hidden retry of an uncertain response.
            result = await self.client.post(f'/api/v1/recordings/{id}/retry')
            self.assertEqual(result.status_code, 202)
            await service.run_job(self.app, *(await service.claim()), adapter)
        row = await self.row(id)
        self.assertEqual(row.state, 'completed')
        self.assertEqual(len(row.data['chunks']), 5)
        self.assertEqual(row.data['chunks'][1]['segments'][0]['start'], 600)
        self.assertEqual(row.data['chunks'][1]['segments'][0]['speaker'], 'part-2:0')
        self.assertEqual(len(calls), 6)
        self.assertEqual(list(service.directory(id).glob('*.mp3')), [])

    async def test_claim_serializes_users_and_restart_does_not_replay_unknown_request(self):
        await self.create_job()
        await self.create_job()
        claim = await service.claim()
        self.assertIsNotNone(claim)
        self.assertIsNone(await service.claim())
        async with get_async_db_context() as db:
            job = await db.get(Recording, claim[0])
            job.lease_until = service.now() - 1
            await db.commit()
        next_claim = await service.claim()
        self.assertNotEqual(next_claim[0], claim[0])
        old = await self.row(claim[0])
        self.assertEqual(old.state, 'interrupted')
        self.assertIsNone(old.lease_token)
        self.assertEqual(len(old.data['chunks']), 0)

    async def test_cancel_prevents_late_response_from_creating_note(self):
        id = await self.create_job(1)
        claim = await service.claim()

        async def response(*args):
            await self.client.post(f'/api/v1/recordings/{id}/cancel')
            return {'text': 'late response'}

        with patch.object(service, 'command', new=AsyncMock(return_value=b'')):
            task = asyncio.create_task(service.run_job(self.app, *claim, SimpleNamespace(transcribe=response)))
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = await self.row(id)
        self.assertEqual(row.state, 'cancelled')
        self.assertIsNone(row.lease_token)
        async with get_async_db_context() as db:
            self.assertIsNone(await db.get(Note, row.note_id))

    async def test_summary_failure_leaves_transcript_intact(self):
        id = await self.create_job(1)
        with patch.object(service, 'command', new=AsyncMock(return_value=b'')):
            await service.run_job(
                self.app, *(await service.claim()), SimpleNamespace(transcribe=AsyncMock(return_value={'text': '原文'}))
            )
        with patch.object(api, 'complete_text', new=AsyncMock(side_effect=HTTPException(502, 'unavailable'))):
            result = await self.client.post(
                f'/api/v1/recordings/{id}/summary', json={'model': 'bad-model', 'request_key': str(uuid.uuid4())}
            )
        self.assertEqual(result.status_code, 502)
        row = await self.row(id)
        self.assertEqual(row.state, 'completed')
        self.assertEqual(service.public(row)['text'], '原文')

    def test_plain_text_and_invalid_timestamps_are_not_fabricated(self):
        self.assertEqual(service.normalize({'text': '你好'}, 600, 1, 60), {'text': '你好', 'segments': []})
        result = service.normalize(
            {'text': '你好', 'segments': [{'text': '你好', 'start': float('nan'), 'end': 1}]}, 0, 0, 60
        )
        self.assertNotIn('start', result['segments'][0])

    async def test_real_http_asr_format_and_no_authentication_retry(self):
        from aiohttp import web
        from open_webui.utils.session_pool import close_session

        self.addAsyncCleanup(close_session)
        received = []
        upstream_status = 200

        async def transcribe(request):
            form = await request.post()
            received.append((request.headers.get('Authorization'), dict(form)))
            if upstream_status != 200:
                return web.json_response({'error': {'message': 'invalid synthetic credential'}}, status=401)
            return web.json_response(
                {
                    'text': '真实 HTTP 模拟转写',
                    'segments': [{'text': '真实 HTTP 模拟转写', 'start': 0, 'end': 1, 'speaker': 'A'}],
                }
            )

        app = web.Application()
        app.router.add_post('/v1/audio/transcriptions', transcribe)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        await Config.upsert(
            {
                'audio.stt.openai.api_base_url': f'http://127.0.0.1:{port}/v1',
                'recordings.response_format': 'verbose_json',
            }
        )
        first = await self.create_job(1)
        await service.run_job(self.app, *(await service.claim()))
        result = service.public(await self.row(first))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['text'], '真实 HTTP 模拟转写')
        self.assertTrue(result['capabilities']['timestamps'])
        self.assertEqual(received[0][0], 'Bearer synthetic-key')
        self.assertEqual(received[0][1]['response_format'], 'verbose_json')
        self.assertEqual(received[0][1]['language'], 'zh')
        upstream_status = 401
        second = await self.create_job(1)
        await service.run_job(self.app, *(await service.claim()))
        self.assertEqual((await self.row(second)).state, 'failed')
        self.assertEqual(len(received), 2)  # One call per job; no retry without language.

    async def test_reference_snapshot_redacts_endpoint_credentials(self):
        await Config.upsert({'audio.stt.openai.api_base_url': 'https://user:secret@asr.invalid/v1?key=secret'})
        reference = await service.references()
        self.assertNotIn('secret', str(reference))
        self.assertEqual(len(reference['endpoint_fingerprint']), 64)

    async def test_configuration_change_discards_incompatible_checkpoints_on_retry(self):
        id = await self.create_job(1200)
        with patch.object(service, 'command', new=AsyncMock(return_value=b'')):
            adapter = SimpleNamespace(transcribe=AsyncMock(side_effect=[{'text': '旧模型原文'}, TimeoutError()]))
            await service.run_job(self.app, *(await service.claim()), adapter)
        self.assertEqual(len((await self.row(id)).data['chunks']), 1)
        await Config.upsert({'audio.stt.model': 'replacement-asr', 'recordings.response_format': 'verbose_json'})
        result = await self.client.post(f'/api/v1/recordings/{id}/retry')
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()['completed_chunks'], 0)
        self.assertEqual((await self.row(id)).data['response_format'], 'verbose_json')

    async def test_knowledge_snapshot_requires_destination_write_access(self):
        from open_webui.routers import knowledge

        self.owner.role = 'user'
        target = SimpleNamespace(id='private-kb', user_id='someone-else', meta={})
        with (
            patch.object(knowledge.Knowledges, 'get_knowledge_by_id', new=AsyncMock(return_value=target)),
            patch.object(knowledge.AccessGrants, 'has_access', new=AsyncMock(return_value=False)),
            patch.object(knowledge.Files, 'get_file_by_id', new=AsyncMock()) as read_file,
        ):
            with self.assertRaises(HTTPException):
                await knowledge.add_file_to_knowledge_by_id(
                    None,
                    'private-kb',
                    knowledge.KnowledgeFileIdForm(file_id='transcript-file'),
                    user=self.owner,
                    db=SimpleNamespace(),
                )
            read_file.assert_not_awaited()

    async def test_browser_webm_without_duration_header_is_bounded_and_supported(self):
        import json

        metadata = json.dumps({'format': {}, 'streams': [{'codec_type': 'audio'}]}).encode()
        with patch.object(
            service, 'command', new=AsyncMock(side_effect=[metadata, b'out_time_us=3000000000\nprogress=end\n'])
        ) as command:
            duration = await service.probe(Path('browser.webm'), 7200)
        self.assertEqual(duration, 3000)
        arguments = command.await_args_list[1].args
        self.assertIn('7201', arguments)
        self.assertIn('file,pipe', arguments)

    async def test_playlist_inputs_are_rejected(self):
        result = await self.upload(body=b'#EXTM3U\n#EXTINF:50,\nhttps://example.invalid/private.wav\n')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_audio_probes_and_conversion_have_bounded_concurrency(self):
        running = 0
        maximum = 0

        class Process:
            returncode = 0

            async def communicate(self):
                nonlocal running, maximum
                running += 1
                maximum = max(maximum, running)
                await asyncio.sleep(0.02)
                running -= 1
                return b'output', b''

        with (
            patch.object(service, 'COMMAND_SLOTS', asyncio.Semaphore(2)),
            patch.object(
                service.asyncio, 'create_subprocess_exec', new=AsyncMock(side_effect=lambda *a, **kw: Process())
            ),
        ):
            result = await asyncio.gather(*(service.command('synthetic-decoder') for _ in range(5)))
        self.assertEqual(maximum, 2)
        self.assertEqual(result, [b'output'] * 5)

    async def test_selected_provider_model_is_fixed_despite_global_default_change(self):
        from open_webui.models.provider_connections import ProviderConnection, new_provider_connection
        from open_webui.utils import providers

        connection = new_provider_connection(
            provider_id='custom',
            name='Selected ASR',
            auth_type='api_key',
            visibility='private',
            owner_id=self.owner.id,
            config={
                'base_url': 'https://example.com/v1',
                '_catalog_version': 1,
                'model_ids': ['selected-asr'],
                '_model_overrides': {'selected-asr': {'transcription': 'openai-transcription'}},
            },
            credential=providers.encrypt_credential('synthetic-key'),
            status='connected',
            models=[],
            is_active=True,
        )
        async with get_async_db_context() as db:
            db.add(connection)
            await db.commit()
        model_ref = providers.stable_model_id(connection.id, 'selected-asr')
        with patch.object(providers, '_request_json', new=AsyncMock(return_value={'data': []})):
            response = await self.client.post(
                '/api/v1/recordings',
                headers={'Idempotency-Key': str(uuid.uuid4())},
                files={'file': ('class.wav', self.wav(), 'audio/wav')},
                data={'model_ref': model_ref},
            )
            self.assertEqual(response.status_code, 202, response.text)
            id = response.json()['id']
            self.assertEqual(response.json()['config']['model_ref'], model_ref)
            await Config.upsert({'audio.stt.model': 'a-different-global-default', 'audio.stt.engine': ''})
            adapter = SimpleNamespace(transcribe=AsyncMock(return_value={'text': '固定模型的原文'}))
            claim = await service.claim()
            await service.run_job(self.app, *claim, adapter)
            self.assertEqual((await self.row(id)).state, 'completed')
            self.assertEqual(adapter.transcribe.await_args.args[2].data['config']['model_ref'], model_ref)
            async with get_async_db_context() as db:
                stored = await db.get(Recording, id)
                stored.state = 'failed'
                await db.commit()
            # Removing access stops retry; there is no fallback to the server default.
            async with get_async_db_context() as db:
                c = await db.get(ProviderConnection, connection.id)
                c.is_active = False
                await db.commit()
            denied = await self.client.post(f'/api/v1/recordings/{id}/retry')
            self.assertEqual(denied.status_code, 404)

    async def test_switching_recording_model_requires_explicit_full_restart(self):
        id = await self.create_job(1)
        async with get_async_db_context() as db:
            job = await db.get(Recording, id)
            job.state = 'failed'
            await db.commit()
        with (
            patch.object(service, 'authorize', new=AsyncMock()),
            patch.object(
                service,
                'references',
                new=AsyncMock(return_value={'model_ref': 'provider:new:asr', 'response_format': 'json'}),
            ),
        ):
            result = await self.client.post(f'/api/v1/recordings/{id}/retry', json={'model_ref': 'provider:new:asr'})
            self.assertEqual(result.status_code, 409)
            result = await self.client.post(
                f'/api/v1/recordings/{id}/retry', json={'model_ref': 'provider:new:asr', 'restart': True}
            )
            self.assertEqual(result.status_code, 202, result.text)
            self.assertEqual(result.json()['config']['model_ref'], 'provider:new:asr')
