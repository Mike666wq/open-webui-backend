"""Import reservation must precede every application storage write."""
import unittest
import asyncio
import io
import os
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import delete, select
from open_webui.internal.db import get_async_db_context
from open_webui.models.knowledge import Knowledge, KnowledgeForm, Knowledges
from open_webui.models.files import File, FileForm, Files
from open_webui.models.resources import ResourceEntry, ResourceTask
from open_webui.models.users import User, Users
from open_webui.storage.provider import Storage
from open_webui.routers import knowledge as api
from open_webui.utils.auth import get_verified_user


class ImportReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user = await Users.insert_new_user(str(uuid.uuid4()), 'Owner', f'{uuid.uuid4()}@example.test', role='user')
        self.kb = await Knowledges.insert_new_knowledge(self.user.id, KnowledgeForm(name='Reservation', description=''))
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/api/v1/knowledge')
        self.app.dependency_overrides[get_verified_user] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        async with get_async_db_context() as db:
            await db.execute(delete(ResourceTask).where(ResourceTask.user_id == self.user.id))
            await db.execute(delete(ResourceEntry).where(ResourceEntry.user_id == self.user.id))
            await db.execute(delete(File).where(File.user_id == self.user.id))
            await db.execute(delete(Knowledge).where(Knowledge.id == self.kb.id))
            await db.execute(delete(User).where(User.id == self.user.id))
            await db.commit()

    async def test_expired_reservation_recovers_after_database_session_reopen(self):
        # Simulate a process disappearing after a committed reservation and
        # before upload. Recovery sees only persisted data in a fresh session.
        from open_webui.utils.knowledge_import_lifecycle import file_id_for, recover_expired
        from open_webui.utils import resource_service
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(self.user.id, self.kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        file = await Files.insert_new_file(self.user.id, FileForm(
            id=file_id, filename='picture.png', path=target['primary_path'], data={'status': 'uploading'},
            meta={'knowledge_import_lifecycle': {'version': 1, 'owner_id': self.user.id,
                  'knowledge_id': self.kb.id, 'operation_id': operation_id, 'target': target,
                  'state': 'reserved', 'lease_until': 1, 'lease_token': 'old', 'generation': 1}}
        ))
        self.assertIsNotNone(file)
        await recover_expired()
        await recover_expired()
        async with get_async_db_context() as db:
            tasks = (await db.scalars(select(ResourceTask).where(ResourceTask.user_id == self.user.id,
                       ResourceTask.action == 'recycle', ResourceTask.state != 'preview'))).all()
            self.assertEqual(len(tasks), 1)
            task_id = tasks[0].id
        await resource_service.run_task(task_id)
        async with get_async_db_context() as db:
            task = await db.get(ResourceTask, task_id)
            self.assertEqual(task.state, 'completed')
            entry = await db.scalar(select(ResourceEntry).where(ResourceEntry.resource_id == file_id))
            self.assertEqual(entry.state, 'recycled')

    async def test_image_success_is_placed_and_replay_does_not_upload_again(self):
        operation_id = str(uuid.uuid4())
        path = f'/api/v1/knowledge/{self.kb.id}/import/file'
        data = {'source_path': '测试图片.png', 'source_hash': 'a' * 64,
                'operation_id': operation_id, 'kind': 'image'}
        files = {'file': ('测试图片.png', b'png bytes', 'image/png')}
        with patch.object(Storage, 'upload_prepared', wraps=Storage.upload_prepared) as upload:
            first = await self.client.post(path, data=data, files=files)
            second = await self.client.post(path, data=data, files=files)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()['status'], 'completed')
        self.assertFalse(first.json()['indexed'])
        self.assertEqual(upload.call_count, 1)
        self.assertTrue(await Knowledges.has_file(self.kb.id, first.json()['file_id']))
        self.assertEqual(
            len(await Knowledges.get_files_by_id(self.kb.id)),
            1,
        )
        stored = await Files.get_file_by_id(first.json()['file_id'])
        self.assertEqual(stored.filename, '测试图片.png')
        self.assertTrue(stored.path.endswith(f'{stored.id}.png'))
        self.assertNotIn('测试图片', stored.path)

    async def test_read_only_account_never_reserves_or_uploads(self):
        other = SimpleNamespace(id='unrelated-user', role='user', email='x@example.test', name='Other')
        self.app.dependency_overrides[get_verified_user] = lambda: other
        with patch.object(api.Files, 'insert_new_file', new=AsyncMock()) as insert, patch.object(Storage, 'upload_prepared') as upload:
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={'source_path': 'picture.png', 'source_hash': 'a' * 64, 'operation_id': str(uuid.uuid4()), 'kind': 'image'},
                files={'file': ('picture.png', b'png bytes', 'image/png')},
            )
        self.assertEqual(response.status_code, 403, response.text)
        insert.assert_not_awaited()
        upload.assert_not_called()

    async def test_markdown_indexes_only_supplied_text_then_attaches(self):
        operation_id = str(uuid.uuid4())
        with patch.object(api, 'process_file', new=AsyncMock(return_value={'status': True, 'collection_name': self.kb.id})) as process:
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={'source_path': 'note.md', 'source_hash': 'a' * 64, 'operation_id': operation_id,
                      'kind': 'markdown', 'index_text': 'chapter [image]'},
                files={'file': ('note.md', b'chapter ![alt](/api/v1/files/one/content)', 'text/markdown')},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['indexed'])
        form = process.await_args.args[1]
        self.assertEqual(form.content, 'chapter [image]')
        self.assertEqual(form.collection_name, self.kb.id)
        self.assertTrue(await Knowledges.has_file(self.kb.id, response.json()['file_id']))

    async def test_empty_index_text_is_rejected_before_reservation(self):
        with patch.object(api.Files, 'insert_new_file', new=AsyncMock()) as insert, patch.object(Storage, 'upload_prepared') as upload:
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={'source_path': 'note.md', 'source_hash': 'a' * 64, 'operation_id': str(uuid.uuid4()), 'kind': 'markdown', 'index_text': '  '},
                files={'file': ('note.md', b'body', 'text/markdown')},
            )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['detail']['code'], 'empty_index_text')
        insert.assert_not_awaited()
        upload.assert_not_called()

    async def test_concurrent_recovery_fences_old_lease_and_creates_one_task(self):
        from open_webui.utils.knowledge_import_lifecycle import file_id_for, recover_expired, renew
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(self.user.id, self.kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        await Files.insert_new_file(self.user.id, FileForm(
            id=file_id, filename='picture.png', path=target['primary_path'], data={'status': 'uploading'},
            meta={'knowledge_import_lifecycle': {'version': 1, 'owner_id': self.user.id,
                'knowledge_id': self.kb.id, 'operation_id': operation_id, 'target': target,
                'state': 'uploading', 'lease_until': int(time.time()) - 1, 'lease_token': 'old', 'generation': 1}}
        ))
        await asyncio.gather(recover_expired(), recover_expired())
        self.assertFalse(await renew(file_id, 'old', 1, state='processing'))
        async with get_async_db_context() as db:
            tasks = (await db.scalars(select(ResourceTask).where(ResourceTask.user_id == self.user.id,
                       ResourceTask.action == 'recycle', ResourceTask.state != 'preview'))).all()
            self.assertEqual(len(tasks), 1)

    async def test_active_lease_is_not_recovered(self):
        from open_webui.utils.knowledge_import_lifecycle import file_id_for, recover_expired
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(self.user.id, self.kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        await Files.insert_new_file(self.user.id, FileForm(
            id=file_id, filename='picture.png', path=target['primary_path'], data={'status': 'uploading'},
            meta={'knowledge_import_lifecycle': {'version': 1, 'owner_id': self.user.id,
                'knowledge_id': self.kb.id, 'operation_id': operation_id, 'target': target,
                'state': 'uploading', 'lease_until': int(time.time()) + 120, 'lease_token': 'active', 'generation': 1}}
        ))
        self.assertEqual(await recover_expired(), [])
        async with get_async_db_context() as db:
            self.assertEqual((await db.get(File, file_id)).data['status'], 'uploading')
            self.assertIsNone(await db.scalar(select(ResourceTask).where(ResourceTask.user_id == self.user.id)))

    async def test_upload_failure_is_persisted_and_recycled(self):
        operation_id = str(uuid.uuid4())
        def interrupted(file, target, tags):
            with open(target['local_path'], 'wb') as local:
                local.write(file.read())
            raise OSError('simulated upload interruption')
        with patch.object(Storage, 'upload_prepared', side_effect=interrupted):
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={'source_path': 'picture.png', 'source_hash': 'a' * 64, 'operation_id': operation_id, 'kind': 'image'},
                files={'file': ('picture.png', b'png bytes', 'image/png')},
            )
        self.assertEqual(response.status_code, 502, response.text)
        file_id = response.json()['detail']['file_id']
        async with get_async_db_context() as db:
            entry = await db.scalar(select(ResourceEntry).where(ResourceEntry.resource_id == file_id))
            self.assertIsNotNone(entry)
            self.assertEqual(entry.state, 'recycled')
            self.assertGreater(entry.expires_at - entry.deleted_at, 6 * 86400)

    async def test_index_failure_after_upload_is_persisted_and_recycled(self):
        operation_id = str(uuid.uuid4())
        with patch.object(
            api,
            'process_file',
            new=AsyncMock(side_effect=RuntimeError('simulated indexing interruption')),
        ) as process:
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={
                    'source_path': 'note.md',
                    'source_hash': 'b' * 64,
                    'operation_id': operation_id,
                    'kind': 'markdown',
                    'index_text': 'indexed body',
                },
                files={
                    'file': (
                        'note.md',
                        b'readable markdown body',
                        'text/markdown',
                    )
                },
            )
        self.assertEqual(response.status_code, 502, response.text)
        process.assert_awaited_once()
        file_id = response.json()['detail']['file_id']
        self.assertFalse(await Knowledges.has_file(self.kb.id, file_id))
        async with get_async_db_context() as db:
            row = await db.get(File, file_id)
            self.assertEqual(row.data['status'], 'failed')
            entry = await db.scalar(
                select(ResourceEntry).where(ResourceEntry.resource_id == file_id)
            )
            self.assertIsNotNone(entry)
            self.assertEqual(entry.state, 'recycled')

    async def test_prepared_purge_is_idempotent_before_any_object_write(self):
        from open_webui.utils import resource_service
        from open_webui.utils.knowledge_import_lifecycle import file_id_for
        from open_webui.utils.resource_visibility import including_recycled

        operation_id = str(uuid.uuid4())
        file_id = file_id_for(self.user.id, self.kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        await Files.insert_new_file(
            self.user.id,
            FileForm(
                id=file_id,
                filename='never-written.png',
                path=target['primary_path'],
                data={'status': 'failed'},
                meta={
                    'knowledge_import_lifecycle': {
                        'version': 1,
                        'owner_id': self.user.id,
                        'knowledge_id': self.kb.id,
                        'operation_id': operation_id,
                        'target': target,
                        'state': 'failed',
                        'lease_until': 0,
                        'lease_token': 'old',
                        'generation': 1,
                    }
                },
            ),
        )
        async with get_async_db_context() as db:
            db.add(
                ResourceEntry(
                    id=str(uuid.uuid4()),
                    user_id=self.user.id,
                    type='file',
                    resource_id=file_id,
                    state='recycled',
                    generation=1,
                    title='never-written.png',
                    cutoff_at=0,
                    deleted_at=0,
                    expires_at=0,
                    size_bytes=0,
                    data={},
                )
            )
            await db.commit()

        with including_recycled():
            async with get_async_db_context() as db:
                preview = await resource_service.make_preview(
                    db,
                    self.user,
                    'purge',
                    [{'type': 'file', 'id': file_id}],
                    {},
                )
                self.assertEqual(preview['allowed'], 1, preview)
                task = await resource_service.submit(
                    db,
                    self.user,
                    preview['id'],
                    str(uuid.uuid4()),
                )
                task_id = task.id
            await resource_service.run_task(task_id)

        async with get_async_db_context() as db:
            task = await db.get(ResourceTask, task_id)
            self.assertEqual(task.state, 'completed', task.results)
            self.assertIsNone(await db.get(File, file_id))
            entry = await db.scalar(
                select(ResourceEntry).where(ResourceEntry.resource_id == file_id)
            )
            self.assertEqual(entry.state, 'purged')
        Storage.delete_prepared(target)
        Storage.delete_prepared(target)

    async def test_prepared_purge_storage_error_retains_record_and_retries(self):
        from open_webui.utils import resource_service
        from open_webui.utils.knowledge_import_lifecycle import file_id_for
        from open_webui.utils.resource_visibility import including_recycled

        operation_id = str(uuid.uuid4())
        file_id = file_id_for(self.user.id, self.kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        Storage.upload_prepared(
            io.BytesIO(b'prepared bytes'),
            target,
            {},
        )
        await Files.insert_new_file(
            self.user.id,
            FileForm(
                id=file_id,
                filename='retry.png',
                path=target['primary_path'],
                data={'status': 'failed'},
                meta={
                    'knowledge_import_lifecycle': {
                        'version': 1,
                        'owner_id': self.user.id,
                        'knowledge_id': self.kb.id,
                        'operation_id': operation_id,
                        'target': target,
                        'state': 'failed',
                        'lease_until': 0,
                        'lease_token': 'old',
                        'generation': 1,
                    }
                },
            ),
        )
        async with get_async_db_context() as db:
            db.add(
                ResourceEntry(
                    id=str(uuid.uuid4()),
                    user_id=self.user.id,
                    type='file',
                    resource_id=file_id,
                    state='recycled',
                    generation=1,
                    title='retry.png',
                    cutoff_at=0,
                    deleted_at=0,
                    expires_at=0,
                    size_bytes=len(b'prepared bytes'),
                    data={},
                )
            )
            await db.commit()

        with including_recycled():
            async with get_async_db_context() as db:
                preview = await resource_service.make_preview(
                    db,
                    self.user,
                    'purge',
                    [{'type': 'file', 'id': file_id}],
                    {},
                )
                self.assertEqual(preview['allowed'], 1, preview)
                task = await resource_service.submit(
                    db,
                    self.user,
                    preview['id'],
                    str(uuid.uuid4()),
                )
                task_id = task.id

            with patch.object(
                Storage,
                'delete_prepared',
                side_effect=OSError('synthetic storage outage'),
            ):
                await resource_service.run_task(task_id)

            async with get_async_db_context() as db:
                failed = await db.get(ResourceTask, task_id)
                self.assertEqual(failed.state, 'failed', failed.results)
                self.assertIsNotNone(await db.get(File, file_id))
                self.assertTrue(target['local_path'])

            self.assertTrue(os.path.exists(target['local_path']))

            async with get_async_db_context() as db:
                failed = await db.get(ResourceTask, task_id)
                failed.state = 'pending'
                failed.lease_until = 0
                await db.commit()
            await resource_service.run_task(task_id)

        async with get_async_db_context() as db:
            retried = await db.get(ResourceTask, task_id)
            self.assertEqual(retried.state, 'completed', retried.results)
            self.assertIsNone(await db.get(File, file_id))
            entry = await db.scalar(
                select(ResourceEntry).where(ResourceEntry.resource_id == file_id)
            )
            self.assertEqual(entry.state, 'purged')
        self.assertFalse(os.path.exists(target['local_path']))

    async def test_reservation_failure_does_not_write_storage(self):
        with patch.object(api.Files, 'insert_new_file', new=AsyncMock(return_value=None)) as insert, patch.object(Storage, 'upload_prepared', new=MagicMock()) as upload:
            response = await self.client.post(
                f'/api/v1/knowledge/{self.kb.id}/import/file',
                data={'source_path': 'picture.png', 'source_hash': 'a' * 64, 'operation_id': str(uuid.uuid4()), 'kind': 'image'},
                files={'file': ('picture.png', b'png bytes', 'image/png')},
            )
        self.assertGreaterEqual(response.status_code, 500, response.text)
        insert.assert_awaited_once()
        upload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
