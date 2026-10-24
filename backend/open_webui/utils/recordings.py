"""Disk-bounded transcription with database leases and explicit uncertain failures.

Adapters may later implement vendor submit/poll/cancel. The initial adapter uses
existing server STT configuration; it never assumes a model name implies a protocol.
"""

import asyncio
import hashlib
import json
import logging
import math
import shutil
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from open_webui.config import DATA_DIR
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.notes import Note
from open_webui.models.recordings import Recording
from open_webui.models.users import Users
from open_webui.routers.notes import check_notes_access
from open_webui.utils.access_control import has_permission
from open_webui.utils.providers import get_media_api_connection
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)
ROOT = Path(DATA_DIR) / 'recordings'
CHUNK_SECONDS = 600
COMMAND_SLOTS = asyncio.Semaphore(2)
AUDIO_FORMATS = 'wav,mp3,mov,matroska,webm,ogg,flac,aac,amr,asf,avi,au,aiff,caf'
ACTIVE = ('preparing', 'transcribing', 'saving')
DEFAULTS = {'max_size_mb': 500, 'max_duration_seconds': 7200, 'response_format': 'json'}
REFERENCE_KEYS = ('engine', 'model', 'provider_connection_id', 'openai.api_request_format')


def now():
    return int(time.time())


def directory(job_id):
    # IDs are generated on the server, but keep this helper safe for every caller.
    return ROOT / str(uuid.UUID(job_id))


async def settings():
    return {key: await Config.get(f'recordings.{key}', value) for key, value in DEFAULTS.items()}


async def authorize(user, *, stt=False, model_ref=None):
    if not user or user.role not in ('user', 'admin'):
        raise HTTPException(403, 'User is no longer active')
    async with get_async_db_context() as db:
        await check_notes_access(user, db)
    if stt:
        if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
            raise HTTPException(503, 'Recording transcription requires ffmpeg and ffprobe in the backend image')
        if user.role != 'admin' and not await has_permission(user.id, 'chat.stt', await Config.get('user.permissions')):
            raise HTTPException(403, 'Speech-to-text permission is required')
        if model_ref:
            from open_webui.utils.providers import resolve_media_model

            await resolve_media_model(model_ref, user, 'transcription')
            return
        engine = await Config.get('audio.stt.engine')
        if engine not in ('provider', 'openai', 'azure', 'deepgram', 'mistral'):
            raise HTTPException(400, 'Configure a server-side cloud ASR in Admin Settings → Audio first')
        if engine == 'provider':
            await get_media_api_connection(await Config.get('audio.stt.provider_connection_id'), user)
        else:
            key = {
                'openai': 'openai.api_key',
                'azure': 'azure.api_key',
                'deepgram': 'deepgram.api_key',
                'mistral': 'mistral.api_key',
            }[engine]
            if not await Config.get(f'audio.stt.{key}'):
                raise HTTPException(400, 'The configured ASR credential is missing')
        if engine in ('provider', 'openai') and not await Config.get('audio.stt.model'):
            raise HTTPException(400, 'Configure a transcription model first')


async def references(model_ref=None, user=None):
    if model_ref:
        from open_webui.utils.providers import provider_base_url, resolve_media_model

        resolved = await resolve_media_model(model_ref, user, 'transcription')
        return {
            'engine': 'provider',
            'model_ref': model_ref,
            'model': resolved.upstream_model_id,
            'provider_connection_id': resolved.connection.id,
            'response_format': await Config.get('recordings.response_format', 'json'),
            'endpoint_fingerprint': hashlib.sha256(
                json.dumps(
                    [provider_base_url(resolved.connection), resolved.model_metadata['uses']], sort_keys=True
                ).encode()
            ).hexdigest(),
        }
    result = {key: await Config.get(f'audio.stt.{key}') for key in REFERENCE_KEYS}
    endpoints = [
        await Config.get(f'audio.stt.{key}')
        for key in ('openai.api_base_url', 'azure.base_url', 'azure.region', 'mistral.api_base_url')
    ]
    result['response_format'] = await Config.get('recordings.response_format', 'json')
    result['endpoint_fingerprint'] = hashlib.sha256(json.dumps(endpoints).encode()).hexdigest()
    return result


async def command(*args, timeout=120):
    # Bound both upload probes (including WebM scans) and worker conversion.
    async with COMMAND_SLOTS:
        return await run_command(*args, timeout=timeout)


async def run_command(*args, timeout=120):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise ValueError('Audio could not be decoded; check the recording format')
    return stdout


async def probe(path, max_duration=7200):
    if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
        raise HTTPException(503, 'Recording transcription requires ffmpeg and ffprobe in the backend image')
    try:
        info = json.loads(
            await command(
                'ffprobe',
                '-v',
                'error',
                '-protocol_whitelist',
                'file,pipe',
                '-format_whitelist',
                AUDIO_FORMATS,
                '-show_entries',
                'format=duration:stream=codec_type',
                '-of',
                'json',
                str(path),
            )
        )
        if not any(s.get('codec_type') == 'audio' for s in info['streams']):
            raise ValueError()
        duration = info.get('format', {}).get('duration')
        if duration is None or duration == 'N/A':
            # Browser MediaRecorder WebM often has no duration header. Decode on
            # disk with a hard duration cap instead of rejecting a valid recording.
            progress = await command(
                'ffmpeg',
                '-nostdin',
                '-v',
                'error',
                '-protocol_whitelist',
                'file,pipe',
                '-format_whitelist',
                AUDIO_FORMATS,
                '-i',
                str(path),
                '-t',
                str(max_duration + 1),
                '-map',
                '0:a:0',
                '-progress',
                'pipe:1',
                '-nostats',
                '-f',
                'null',
                '-',
            )
            times = [
                int(line.split(b'=', 1)[1])
                for line in progress.splitlines()
                if line.startswith(b'out_time_us=') and line.split(b'=', 1)[1].isdigit()
            ]
            duration = max(times, default=0) / 1_000_000
        duration = float(duration)
        if (
            not math.isfinite(duration)
            or duration <= 0
            or not any(s.get('codec_type') == 'audio' for s in info['streams'])
        ):
            raise ValueError()
        return math.ceil(duration)
    except (ValueError, KeyError):
        raise HTTPException(400, 'The file does not contain a valid, finite audio recording') from None


def normalize(result, offset, chunk_index, chunk_duration):
    """Never invent timestamps or equate speakers across independent requests."""
    text = result.get('text')
    if not isinstance(text, str):
        raise ValueError('ASR returned no transcript text')
    segments = []
    for part in result.get('segments') or []:
        if not isinstance(part, dict) or not isinstance(part.get('text'), str):
            continue
        item = {'text': part['text']}
        start, end = part.get('start'), part.get('end')
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and math.isfinite(start)
            and math.isfinite(end)
            and 0 <= start <= end <= chunk_duration + 1
        ):
            item.update(start=offset + start, end=offset + end)
        speaker = part.get('speaker', part.get('speaker_id'))
        if speaker is not None:
            item['speaker'] = f'part-{chunk_index + 1}:{str(speaker)[:80]}'
        segments.append(item)
    return {'text': text.strip(), 'segments': segments}


def public(job, detail=True):
    data = job.data or {}
    result = {
        name: getattr(job, name)
        for name in ('id', 'title', 'state', 'duration', 'size_bytes', 'created_at', 'updated_at', 'note_id')
    }
    result.update(
        source_present=bool(job.source_present),
        completed_chunks=len(data.get('chunks', [])),
        total_chunks=math.ceil(job.duration / CHUNK_SECONDS),
        error=data.get('error'),
        config=data.get('config'),
        speaker_names=data.get('speaker_names', {}),
        summary_note_ids=data.get('summary_note_ids', []),
    )
    if detail:
        chunks = data.get('chunks', [])
        result['text'] = '\n\n'.join(c['text'] for c in chunks)
        result['segments'] = [s for c in chunks for s in c['segments']]
        result['capabilities'] = {
            'timestamps': any('start' in s for s in result['segments']),
            'speakers': any('speaker' in s for s in result['segments']),
            'cross_chunk_speakers': False,
        }
    return result


async def get_owned(job_id, user):
    await authorize(user)
    async with get_async_db_context() as db:
        job = await db.get(Recording, job_id)
        if not job or job.user_id != user.id:
            raise HTTPException(404, 'Recording not found')
        return job


async def claim():
    """Atomic per-user lock: all pending rows must pass the same active-row guard."""
    token = str(uuid.uuid4())
    async with get_async_db_context() as db:
        # An interrupted HTTP request may already have been billed. Do not replay it.
        await db.execute(
            update(Recording)
            .where(Recording.state.in_(ACTIVE), Recording.lease_until < now())
            .values(state='interrupted', lease_token=None, lease_until=0, updated_at=now())
        )
        await db.execute(
            update(Recording)
            .where(Recording.lease_until < now(), Recording.state == 'cancelled')
            .values(lease_token=None, lease_until=0)
        )
        candidates = (
            await db.scalars(
                select(Recording).where(Recording.state == 'queued').order_by(Recording.created_at).limit(30)
            )
        ).all()
        for job in candidates:
            other = Recording.__table__.alias('other_recording')
            busy = exists(select(other.c.id).where(other.c.user_id == job.user_id, other.c.lease_token.is_not(None)))
            try:
                changed = await db.execute(
                    update(Recording)
                    .where(Recording.id == job.id, Recording.state == 'queued', ~busy)
                    .values(state='preparing', lease_token=token, lease_until=now() + 180, updated_at=now())
                )
            except IntegrityError:
                await db.rollback()
                return None
            if changed.rowcount:
                await db.commit()
                return job.id, token
        await db.commit()
    return None


async def change(job_id, token, **values):
    async with get_async_db_context() as db:
        result = await db.execute(
            update(Recording)
            .where(
                Recording.id == job_id,
                Recording.lease_token == token,
                Recording.state.in_(ACTIVE),
                Recording.lease_until >= now(),
            )
            .values(**values, updated_at=now())
        )
        await db.commit()
        if not result.rowcount:
            raise asyncio.CancelledError()


async def heartbeat(job_id, token, owner_task):
    while True:
        await asyncio.sleep(15)
        try:
            await change(job_id, token, lease_until=now() + 180)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            owner_task.cancel()
            return


class ExistingASRAdapter:
    async def transcribe(self, request, path, job, user):
        from open_webui.routers.audio import transcription_handler

        # Calls the existing single-file engine, not its all-chunks-in-memory pipeline.
        return await transcription_handler(
            request,
            str(path),
            {
                'language': job.data['language'],
                '_recording_single_attempt': True,
                '_provider_model_ref': job.data['config'].get('model_ref'),
                '_recording_response_format': job.data.get('response_format', 'json'),
            },
            user,
        )


async def run_job(app, job_id, token, adapter=None):
    adapter = adapter or ExistingASRAdapter()
    heartbeat_task = asyncio.create_task(heartbeat(job_id, token, asyncio.current_task()))
    chunk_path = directory(job_id) / f'{token}.mp3'
    try:
        async with get_async_db_context() as db:
            job = await db.get(Recording, job_id)
        user = await Users.get_user_by_id(job.user_id)
        if not user or user.role not in ('user', 'admin'):
            raise HTTPException(403, 'User is no longer active')
        request = Request(
            {
                'type': 'http',
                'app': app,
                'headers': [],
                'method': 'POST',
                'path': '/api/v1/recordings',
                'query_string': b'',
            }
        )
        data = dict(job.data)
        data.pop('error', None)
        for index in range(len(data.get('chunks', [])), math.ceil(job.duration / CHUNK_SECONDS)):
            user = await Users.get_user_by_id(job.user_id)
            await authorize(user, stt=True, model_ref=data['config'].get('model_ref'))
            if await references(data['config'].get('model_ref'), user) != data['config']:
                raise HTTPException(409, 'ASR configuration changed; retry to restart with the new model')
            await change(job_id, token, state='preparing')
            offset = index * CHUNK_SECONDS
            await command(
                'ffmpeg',
                '-nostdin',
                '-v',
                'error',
                '-y',
                '-ss',
                str(offset),
                '-protocol_whitelist',
                'file,pipe',
                '-format_whitelist',
                AUDIO_FORMATS,
                '-i',
                str(directory(job_id) / 'source'),
                '-t',
                str(min(CHUNK_SECONDS, job.duration - offset)),
                '-vn',
                '-ar',
                '16000',
                '-ac',
                '1',
                '-b:a',
                '64k',
                str(chunk_path),
            )
            await change(job_id, token, state='transcribing')
            # No automatic retry: a timeout or lost response could already be charged.
            raw = await asyncio.wait_for(adapter.transcribe(request, chunk_path, job, user), 900)
            data['chunks'] = [
                *data.get('chunks', []),
                normalize(raw, offset, index, min(CHUNK_SECONDS, job.duration - offset)),
            ]
            await change(job_id, token, data=data)
            chunk_path.unlink(missing_ok=True)
            chunk_path.with_suffix('.json').unlink(missing_ok=True)
        user = await Users.get_user_by_id(job.user_id)
        await authorize(user, stt=True, model_ref=data['config'].get('model_ref'))
        if await references(data['config'].get('model_ref'), user) != data['config']:
            raise HTTPException(409, 'ASR configuration changed during transcription')
        if not any(chunk['text'].strip() for chunk in data['chunks']):
            raise HTTPException(422, 'No speech was recognized in this recording')
        await change(job_id, token, state='saving')
        # Persist note and job completion together. A crash cannot create duplicate notes.
        async with get_async_db_context() as db:
            current = await db.get(Recording, job_id, with_for_update=True)
            if current.lease_token != token or current.state != 'saving' or current.lease_until < now():
                raise asyncio.CancelledError()
            if not await db.get(Note, current.note_id):
                db.add(
                    Note(
                        id=current.note_id,
                        user_id=user.id,
                        title=current.title,
                        data={'content': {'md': '\n\n'.join(c['text'] for c in data['chunks'])}},
                        meta={'recording_id': job_id, 'recording_kind': 'transcript'},
                        created_at=time.time_ns(),
                        updated_at=time.time_ns(),
                    )
                )
            current.state, current.data = 'completed', data
            current.lease_token, current.lease_until, current.updated_at = None, 0, now()
            await db.commit()
    except asyncio.CancelledError:
        # Cancellation/shutdown leaves saved chunks intact; lease expiry marks interrupted.
        raise
    except Exception as error:
        log.warning('Recording job %s stopped (%s)', job_id, type(error).__name__)
        async with get_async_db_context() as db:
            current = await db.get(Recording, job_id)
            if current and current.lease_token == token and current.state in ACTIVE:
                data = dict(current.data)
                data['error'] = (
                    str(error.detail)
                    if isinstance(error, HTTPException) and error.status_code < 500
                    else 'Transcription did not finish. The last request may have been charged; retry manually.'
                )
                current.data, current.state = data, 'failed'
                current.lease_token, current.lease_until, current.updated_at = None, 0, now()
                await db.commit()
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        chunk_path.unlink(missing_ok=True)
        chunk_path.with_suffix('.json').unlink(missing_ok=True)
        async with get_async_db_context() as db:
            await db.execute(
                update(Recording)
                .where(Recording.id == job_id, Recording.lease_token == token)
                .values(lease_token=None, lease_until=0)
            )
            await db.commit()


async def recording_worker(app):
    while True:
        try:
            claimed = await claim()
            if claimed:
                try:
                    await asyncio.create_task(run_job(app, *claimed))
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Recording worker failed')
            await asyncio.sleep(5)
