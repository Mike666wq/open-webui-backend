"""Knowledge-import images must not remain readable after KB access is revoked."""
import io
import json
import unittest
import uuid
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
from open_webui.models.access_grants import AccessGrants
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select
from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File, FileForm, Files
from open_webui.models.knowledge import Knowledge, KnowledgeFile, KnowledgeForm, Knowledges
from open_webui.models.groups import GroupForm, Groups
from open_webui.models.resources import ResourceEntry
from open_webui.models.users import User, Users
from open_webui.retrieval.utils import filter_accessible_collections, get_sources_from_items
from open_webui.routers import files as files_api
from open_webui.routers import knowledge as knowledge_api
from open_webui.routers import models as models_api
from open_webui.routers import retrieval as retrieval_api
from open_webui.tools import builtin as builtin_tools
from open_webui.utils import resource_service
from open_webui.utils import files as file_utils
from open_webui.storage.provider import Storage
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.knowledge_import_lifecycle import file_id_for


class ImportImagePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploader_without_knowledge_access_cannot_read_imported_image(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        uploader = await Users.insert_new_user(str(uuid.uuid4()), 'Former writer', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(uploader.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        _, path = Storage.upload_prepared(io.BytesIO(b'synthetic image'), target, {})
        await Files.insert_new_file(uploader.id, FileForm(id=file_id, filename='image.png', path=path,
            data={'status': 'completed'}, meta={'name': 'image.png', 'content_type': 'image/png',
            'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': {'version': 1, 'owner_id': uploader.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed', 'lease_until': 0,
                'lease_token': 'old', 'generation': 1}}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, uploader.id)
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: uploader
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(f'/api/v1/files/{file_id}/content')
            self.assertEqual(response.status_code, 404, 'Former KB writer retained access through file ownership')
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, uploader.id])))
                await db.commit()

    async def test_personal_list_search_and_count_hide_completed_import(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        uploader = await Users.insert_new_user(str(uuid.uuid4()), 'Former writer', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(uploader.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        _, path = Storage.upload_prepared(io.BytesIO(b'synthetic image'), target, {})
        await Files.insert_new_file(uploader.id, FileForm(id=file_id, filename='secret.png', path=path,
            data={'status': 'completed'}, meta={'name': 'secret.png', 'content_type': 'image/png',
            'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': {'version': 1, 'owner_id': uploader.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed', 'lease_until': 0,
                'lease_token': 'old', 'generation': 1}}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, uploader.id)
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: uploader
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                listed = await client.get('/api/v1/files/?content=false')
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertNotIn(file_id, [item['id'] for item in listed.json()['items']])
                searched = await client.get('/api/v1/files/search', params={'filename': 'secret.png'})
                self.assertIn(searched.status_code, (200, 404), searched.text)
                if searched.status_code == 200:
                    self.assertNotIn(file_id, [item['id'] for item in searched.json()])
                counted = await client.get('/api/v1/files/count')
                self.assertEqual(counted.json(), 0, 'Completed import must not count as a personal file')
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, uploader.id])))
                await db.commit()

    async def test_read_grant_member_can_read_but_not_write_imported_image(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        member = await Users.insert_new_user(str(uuid.uuid4()), 'Reader', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.png')
        _, path = Storage.upload_prepared(io.BytesIO(b'synthetic image'), target, {})
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='image.png', path=path,
            data={'status': 'completed'}, meta={'name': 'image.png', 'content_type': 'image/png',
            'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed', 'lease_until': 0,
                'lease_token': 'old', 'generation': 1}}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        await AccessGrants.grant_access('knowledge', kb.id, 'user', member.id, 'read')
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: member
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(f'/api/v1/files/{file_id}/content')
                self.assertEqual(response.status_code, 200, response.text)
                await AccessGrants.revoke_access('knowledge', kb.id, 'user', member.id, 'read')
                response = await client.get(f'/api/v1/files/{file_id}/content')
                self.assertEqual(response.status_code, 404, 'Revoked member retained v2 image access')
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, member.id])))
                await db.commit()

    async def test_completed_import_is_hidden_from_resource_management_and_corruption_fails_closed(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        writer = await Users.insert_new_user(str(uuid.uuid4()), 'Writer', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(writer.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'imported markdown'), target, {})
        managed = {'version': 1, 'owner_id': writer.id, 'knowledge_id': kb.id,
                   'operation_id': operation_id, 'target': target, 'state': 'completed',
                   'lease_until': 0, 'lease_token': 'old', 'generation': 1,
                   'identity': {'source_path': 'note.md', 'source_hash': 'a' * 64,
                                'kind': 'markdown', 'directory_id': None, 'filename': 'note.md'}}
        await Files.insert_new_file(writer.id, FileForm(id=file_id, filename='note.md', path=path,
            data={'status': 'completed'}, meta={'name': 'note.md', 'content_type': 'text/markdown',
            'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': managed}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, writer.id)
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: writer
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                from open_webui.internal.db import get_async_db
                from open_webui.utils.resource_visibility import maintenance as maintenance_var
                token = maintenance_var.set(False)
                try:
                    async with get_async_db() as db:
                        preview = await resource_service.make_preview(
                            db, writer, 'recycle', [{'type': 'file', 'id': file_id}], {},
                        )
                finally:
                    maintenance_var.reset(token)
                self.assertFalse(preview['allowed'], 'Old file owner must not manage a completed v2 import')
                self.assertIn('Resource not found', preview['items'][0]['reasons'])

                response = await client.post(f'/api/v1/files/{file_id}/data/content/update',
                                             json={'content': 'rewritten'})
                self.assertEqual(response.status_code, 404, response.text)

                await AccessGrants.grant_access('knowledge', kb.id, 'user', writer.id, 'write')
                async with get_async_db_context() as db:
                    row = await db.get(File, file_id)
                    row.meta = {
                        **row.meta,
                        'knowledge_import_lifecycle': {
                            **row.meta['knowledge_import_lifecycle'],
                            'target': {
                                **row.meta['knowledge_import_lifecycle']['target'],
                                'primary_path': '/corrupt/path',
                            },
                        },
                    }
                    await db.commit()
                response = await client.get(f'/api/v1/files/{file_id}/content')
                self.assertEqual(response.status_code, 404, 'A corrupted v2 binding must fail closed')
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, writer.id])))
                await db.commit()

    async def test_completed_import_cannot_be_reattached_to_another_knowledge(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        member = await Users.insert_new_user(str(uuid.uuid4()), 'Writer', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='Source KB', description=''))
        other_kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='Other KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'imported markdown'), target, {})
        managed = {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                   'operation_id': operation_id, 'target': target, 'state': 'completed',
                   'lease_until': 0, 'lease_token': 'old', 'generation': 1,
                   'identity': {'source_path': 'note.md', 'source_hash': 'a' * 64,
                                'kind': 'markdown', 'directory_id': None, 'filename': 'note.md'}}
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='note.md', path=path,
            data={'status': 'completed', 'content': 'indexed text'}, meta={'name': 'note.md',
            'content_type': 'text/markdown', 'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': managed}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        await AccessGrants.grant_access('knowledge', other_kb.id, 'user', member.id, 'write')
        await AccessGrants.grant_access('knowledge', kb.id, 'user', member.id, 'read')
        app = FastAPI()
        app.include_router(knowledge_api.router, prefix='/api/v1/knowledge')
        app.dependency_overrides[get_verified_user] = lambda: member
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post(f'/api/v1/knowledge/{other_kb.id}/file/add',
                    json={'file_id': file_id, 'process': False})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()['detail'], 'knowledge_import_scope_mismatch')
                self.assertFalse(await Knowledges.has_file(other_kb.id, file_id))
                batch = await client.post(
                    f'/api/v1/knowledge/{other_kb.id}/files/batch/add',
                    json=[{'file_id': file_id, 'process': False}],
                )
                self.assertEqual(batch.status_code, 409, batch.text)
                self.assertEqual(batch.json()['detail'], 'knowledge_import_scope_mismatch')
                self.assertFalse(await Knowledges.has_file(other_kb.id, file_id))
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id.in_([kb.id, other_kb.id])))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id.in_([kb.id, other_kb.id])))
                await db.execute(delete(User).where(User.id.in_([owner.id, member.id])))
                await db.commit()

    async def test_content_route_matrix_rechecks_source_knowledge_and_lifecycle(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='admin')
        reader = await Users.insert_new_user(str(uuid.uuid4()), 'Reader', f'{uuid.uuid4()}@example.test', role='user')
        writer = await Users.insert_new_user(str(uuid.uuid4()), 'Writer', f'{uuid.uuid4()}@example.test', role='user')
        unrelated = await Users.insert_new_user(str(uuid.uuid4()), 'Unrelated', f'{uuid.uuid4()}@example.test', role='user')
        admin = await Users.insert_new_user(str(uuid.uuid4()), 'Admin', f'{uuid.uuid4()}@example.test', role='admin')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'synthetic markdown'), target, {})
        managed = {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                   'operation_id': operation_id, 'target': target, 'state': 'completed',
                   'lease_until': 0, 'lease_token': 'old', 'generation': 1,
                   'identity': {'source_path': 'note.md', 'source_hash': 'a' * 64,
                                'kind': 'markdown', 'directory_id': None, 'filename': 'note.md'}}
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='note.md', path=path,
            data={'status': 'completed', 'content': 'indexed text'}, meta={'name': 'note.md', 'content_type': 'text/markdown',
            'data': {'markdown_import': {'version': 2, 'operation_id': operation_id}},
            'knowledge_import_lifecycle': managed}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        await AccessGrants.grant_access('knowledge', kb.id, 'user', reader.id, 'read')
        await AccessGrants.grant_access('knowledge', kb.id, 'user', writer.id, 'write')
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for user, expected in ((owner, 200), (reader, 200), (writer, 200), (unrelated, 404), (admin, 200)):
                    app.dependency_overrides[get_verified_user] = lambda user=user: user
                    response = await client.get(f'/api/v1/files/{file_id}/content')
                    self.assertEqual(response.status_code, expected, user.name)
                    if expected == 200:
                        self.assertEqual(response.headers.get('cache-control'), 'private, no-store')

                app.dependency_overrides[get_verified_user] = lambda: reader
                metadata = await client.get(f'/api/v1/files/{file_id}')
                self.assertEqual(metadata.status_code, 200)
                self.assertEqual(metadata.headers.get('cache-control'), 'private, no-store')
                self.assertNotIn('knowledge_import_lifecycle', metadata.json()['meta'])
                self.assertNotIn('path', metadata.json())
                indexed = await client.get(f'/api/v1/files/{file_id}/data/content')
                self.assertEqual(indexed.status_code, 200)
                self.assertEqual(indexed.json(), {'content': 'indexed text'})
                self.assertEqual(indexed.headers.get('cache-control'), 'private, no-store')
                status = await client.get(f'/api/v1/files/{file_id}/process/status')
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.headers.get('cache-control'), 'private, no-store')
                response = await client.get(f'/api/v1/files/{file_id}/content', params={'attachment': 'true'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('attachment;', response.headers['content-disposition'])
                self.assertEqual(response.headers.get('cache-control'), 'private, no-store')
                response = await client.get(f'/api/v1/files/{file_id}/content/note.md')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('cache-control'), 'private, no-store')
                response = await client.head(f'/api/v1/files/{file_id}/content')
                self.assertEqual(response.status_code, 405, 'The content GET route does not register HEAD')
                self.assertEqual(response.content, b'')
                response = await client.get(f'/api/v1/files/{file_id}/content', headers={'Range': 'bytes=0-3'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b'synt')
                self.assertEqual(response.headers.get('cache-control'), 'private, no-store')

                with patch.object(
                    file_utils.Storage,
                    'get_file',
                    wraps=file_utils.Storage.get_file,
                ) as read_original:
                    self.assertTrue(
                        await file_utils.get_image_base64_from_file_id(
                            file_id,
                            user=reader,
                        )
                    )
                    self.assertIsNone(
                        await file_utils.get_image_base64_from_file_id(
                            file_id,
                            user=unrelated,
                        )
                    )
                self.assertEqual(read_original.call_count, 1)

                app.dependency_overrides[get_verified_user] = lambda: owner
                response = await client.get(f'/api/v1/files/{file_id}/content/html')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('cache-control'), 'private, no-store')

                app.dependency_overrides[get_verified_user] = lambda: reader
                denied_rename = await client.post(f'/api/v1/files/{file_id}/rename', json={'filename': 'denied.md'})
                self.assertEqual(denied_rename.status_code, 404)
                app.dependency_overrides[get_verified_user] = lambda: writer
                granted_rename = await client.post(f'/api/v1/files/{file_id}/rename', json={'filename': 'writer.md'})
                self.assertEqual(granted_rename.status_code, 200, granted_rename.text)
                await AccessGrants.revoke_access('knowledge', kb.id, 'user', writer.id, 'write')
                await AccessGrants.grant_access('knowledge', kb.id, 'user', writer.id, 'read')
                downgraded_rename = await client.post(
                    f'/api/v1/files/{file_id}/rename',
                    json={'filename': 'downgraded.md'},
                )
                self.assertEqual(downgraded_rename.status_code, 404)
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 200)
                await AccessGrants.revoke_access('knowledge', kb.id, 'user', writer.id, 'read')
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)

                await AccessGrants.revoke_access('knowledge', kb.id, 'user', reader.id, 'read')
                app.dependency_overrides[get_verified_user] = lambda: reader
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content/note.md')).status_code, 404)

                async with get_async_db_context() as db:
                    association = await db.scalar(select(KnowledgeFile).where(KnowledgeFile.file_id == file_id))
                    await db.delete(association)
                    await db.commit()
                app.dependency_overrides[get_verified_user] = lambda: owner
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/data/content')).status_code, 404)
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/process/status')).status_code, 404)
                await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)

                async with get_async_db_context() as db:
                    recycled_knowledge = ResourceEntry(
                        id=str(uuid.uuid4()), user_id=owner.id, type='knowledge', resource_id=kb.id,
                        state='recycled', generation=1, title='KB', cutoff_at=0,
                        deleted_at=0, expires_at=0, size_bytes=0, data={},
                    )
                    db.add(recycled_knowledge)
                    await db.commit()
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)
                async with get_async_db_context() as db:
                    with self.assertRaises(HTTPException) as blocked_model_reference:
                        await models_api._verify_knowledge_file_access(
                            [{'type': 'file', 'id': file_id}],
                            admin,
                            db,
                        )
                    self.assertEqual(blocked_model_reference.exception.status_code, 403)
                async with get_async_db_context() as db:
                    entry = await db.get(ResourceEntry, recycled_knowledge.id)
                    entry.state = 'active'
                    await db.commit()
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 200)
                async with get_async_db_context() as db:
                    await models_api._verify_knowledge_file_access(
                        [{'type': 'file', 'id': file_id}],
                        admin,
                        db,
                    )

                async with get_async_db_context() as db:
                    recycled_file = ResourceEntry(
                        id=str(uuid.uuid4()), user_id=owner.id, type='file', resource_id=file_id,
                        state='recycled', generation=1, title='note.md', cutoff_at=0,
                        deleted_at=0, expires_at=0, size_bytes=0, data={},
                    )
                    db.add(recycled_file)
                    await db.commit()
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)
                async with get_async_db_context() as db:
                    entry = await db.get(ResourceEntry, recycled_file.id)
                    entry.state = 'active'
                    await db.commit()
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 200)
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(ResourceEntry).where(ResourceEntry.resource_id.in_([kb.id, file_id])))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, reader.id, writer.id, unrelated.id, admin.id])))
                await db.commit()

    async def test_sse_rechecks_source_permission_before_each_status_frame(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        member = await Users.insert_new_user(str(uuid.uuid4()), 'Member', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'status body'), target, {})
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='note.md', path=path,
            data={'status': 'pending'}, meta={'name': 'note.md', 'content_type': 'text/markdown',
            'knowledge_import_lifecycle': {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed', 'lease_until': 0,
                'lease_token': 'old', 'generation': 1}}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        await AccessGrants.grant_access('knowledge', kb.id, 'user', member.id, 'read')
        app = FastAPI()
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: member

        async def revoke_before_next_frame(_seconds):
            await AccessGrants.revoke_access('knowledge', kb.id, 'user', member.id, 'read')

        try:
            with patch.object(files_api.asyncio, 'sleep', new=revoke_before_next_frame):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.get(f'/api/v1/files/{file_id}/process/status', params={'stream': 'true'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get('cache-control'), 'private, no-store')
            events = [line for line in response.text.splitlines() if line.startswith('data: ')]
            self.assertEqual(len(events), 2, response.text)
            self.assertEqual(json.loads(events[0][6:]), {'status': 'pending'})
            self.assertEqual(json.loads(events[1][6:]), {'status': 'error', 'error': 'access_denied'})
            self.assertNotIn('completed', response.text)
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, member.id])))
                await db.commit()

    async def test_model_file_and_full_context_do_not_expand_source_permission(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        outsider = await Users.insert_new_user(str(uuid.uuid4()), 'Outsider', f'{uuid.uuid4()}@example.test', role='user')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'server secret'), target, {})
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='secret.md', path=path,
            data={'status': 'completed', 'content': 'server secret'}, meta={'name': 'secret.md',
            'content_type': 'text/markdown', 'knowledge_import_lifecycle': {
                'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed',
                'lease_until': 0, 'lease_token': 'old', 'generation': 1,
            }}))
        await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        model_knowledge = [{'type': 'file', 'id': file_id}]
        user_dict = outsider.model_dump()
        request = object()
        try:
            search = json.loads(await builtin_tools.search_knowledge_files(
                'secret',
                __request__=request,
                __user__=user_dict,
                __model_knowledge__=model_knowledge,
            ))
            self.assertEqual(search, [])

            listed = json.loads(await builtin_tools.list_knowledge(
                __request__=request,
                __user__=user_dict,
                __model_knowledge__=model_knowledge,
            ))
            self.assertNotIn(file_id, [item['id'] for item in listed['files']])

            grep = await builtin_tools.grep_knowledge_files(
                'server secret',
                __request__=request,
                __user__=user_dict,
                __model_knowledge__=model_knowledge,
            )
            self.assertIn('No accessible files', grep)
            self.assertNotIn('server secret', grep)

            viewed = json.loads(await builtin_tools.view_knowledge_file(
                file_id,
                __request__=request,
                __user__=user_dict,
            ))
            self.assertEqual(viewed, {'error': 'File not found'})

            fs_view = await builtin_tools.kb_exec(
                f'cat {file_id}',
                __request__=request,
                __user__=user_dict,
                __model_knowledge__=model_knowledge,
            )
            self.assertEqual(fs_view, f'File not found: {file_id}')
            self.assertNotIn('server secret', fs_view)

            sources = await get_sources_from_items(
                request,
                [{
                    'type': 'file',
                    'id': file_id,
                    'name': 'secret.md',
                    'context': 'full',
                    'file': {'data': {'content': 'client injected secret'}},
                }],
                queries=['server secret'],
                embedding_function=None,
                k=3,
                reranking_function=None,
                k_reranker=3,
                r=0.5,
                hybrid_bm25_weight=0.5,
                hybrid_search=False,
                full_context=True,
                user=outsider,
            )
            self.assertEqual(sources, [])
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, outsider.id])))
                await db.commit()

    async def test_admin_file_collection_and_group_grant_follow_v2_source_scope(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='user')
        member = await Users.insert_new_user(str(uuid.uuid4()), 'Group member', f'{uuid.uuid4()}@example.test', role='user')
        admin = await Users.insert_new_user(str(uuid.uuid4()), 'Admin', f'{uuid.uuid4()}@example.test', role='admin')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='KB', description=''))
        group = await Groups.insert_new_group(owner.id, GroupForm(name='Readers', description=''))
        await Groups.set_group_user_ids_by_id(group.id, [member.id])
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'scoped text'), target, {})
        await Files.insert_new_file(owner.id, FileForm(id=file_id, filename='note.md', path=path,
            data={'status': 'completed'}, meta={'name': 'note.md', 'content_type': 'text/markdown',
            'knowledge_import_lifecycle': {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                'operation_id': operation_id, 'target': target, 'state': 'completed', 'lease_until': 0,
                'lease_token': 'old', 'generation': 1}}))
        association = await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
        await AccessGrants.grant_access('knowledge', kb.id, 'group', group.id, 'read')
        try:
            collection = f'file-{file_id}'
            self.assertEqual(await filter_accessible_collections({collection}, admin), {collection})
            app = FastAPI()
            app.include_router(files_api.router, prefix='/api/v1/files')
            app.dependency_overrides[get_verified_user] = lambda: member
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 200)
            await Groups.set_group_user_ids_by_id(group.id, [])
            self.assertEqual(await filter_accessible_collections({collection}, admin), {collection})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}/content')).status_code, 404)

            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.id == association.id))
                await db.commit()
            self.assertEqual(await filter_accessible_collections({collection}, admin), set())
            await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)

            async with get_async_db_context() as db:
                row = await db.get(File, file_id)
                row.meta = {**row.meta, 'knowledge_import_lifecycle': {}}
                await db.commit()
            self.assertEqual(await filter_accessible_collections({collection}, admin), set())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                app.dependency_overrides[get_verified_user] = lambda: owner
                self.assertEqual((await client.get(f'/api/v1/files/{file_id}')).status_code, 404)
                listed = await client.get('/api/v1/files/?content=false')
                self.assertEqual(listed.status_code, 200)
                self.assertNotIn(file_id, [item['id'] for item in listed.json()['items']])
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, member.id, admin.id])))
                await db.commit()

    async def test_knowledge_search_metadata_and_export_exclude_corrupt_import_binding(self):
        owner = await Users.insert_new_user(str(uuid.uuid4()), 'KB owner', f'{uuid.uuid4()}@example.test', role='admin')
        kb = await Knowledges.insert_new_knowledge(owner.id, KnowledgeForm(name='Export KB', description=''))
        targets = []
        file_rows = []
        for filename, lifecycle in (
            (
                'valid.md',
                {'version': 1, 'owner_id': owner.id, 'knowledge_id': kb.id,
                 'operation_id': str(uuid.uuid4()), 'target': None, 'state': 'completed',
                 'lease_until': 0, 'lease_token': 'old', 'generation': 1},
            ),
            ('corrupt.md', {}),
        ):
            operation_id = lifecycle.get('operation_id', str(uuid.uuid4()))
            file_id = file_id_for(owner.id, kb.id, operation_id)
            target = Storage.prepare_upload(file_id + '.md')
            _, path = Storage.upload_prepared(io.BytesIO(f'{filename} body'.encode()), target, {})
            lifecycle = {**lifecycle, 'target': target}
            await Files.insert_new_file(owner.id, FileForm(id=file_id, filename=filename, path=path,
                data={'status': 'completed', 'content': f'{filename} indexed'}, meta={'name': filename,
                'content_type': 'text/markdown', 'knowledge_import_lifecycle': lifecycle}))
            await Knowledges.add_file_to_knowledge_by_id(kb.id, file_id, owner.id)
            targets.append(target)
            file_rows.append(file_id)
        try:
            listed = await Knowledges.search_files_by_id(kb.id, owner.id, {}, db=None)
            self.assertEqual(listed.total, 1)
            self.assertEqual([item.filename for item in listed.items], ['valid.md'])
            self.assertNotIn('knowledge_import_lifecycle', listed.items[0].meta)

            cross_kb = await Knowledges.search_knowledge_files({'user_id': owner.id})
            self.assertEqual(cross_kb.total, 1)
            self.assertEqual([item.filename for item in cross_kb.items], ['valid.md'])
            self.assertNotIn('knowledge_import_lifecycle', cross_kb.items[0].meta)

            metadata = await Knowledges.get_file_metadatas_by_id(kb.id)
            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0].id, file_rows[0])
            self.assertNotIn('knowledge_import_lifecycle', metadata[0].model_dump()['meta'])

            app = FastAPI()
            app.include_router(knowledge_api.router, prefix='/api/v1/knowledge')
            app.dependency_overrides[get_admin_user] = lambda: owner
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(f'/api/v1/knowledge/{kb.id}/export')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers.get('cache-control'), 'private, no-store')
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(archive.namelist(), ['valid.md.txt'])
                self.assertEqual(archive.read('valid.md.txt'), b'valid.md indexed')
        finally:
            for target in targets:
                Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                await db.execute(delete(File).where(File.id.in_(file_rows)))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id == owner.id))
                await db.commit()

    async def test_retrieval_processing_cannot_move_import_to_another_collection(self):
        owner = await Users.insert_new_user(
            str(uuid.uuid4()),
            'KB owner',
            f'{uuid.uuid4()}@example.test',
            role='user',
        )
        source_kb = await Knowledges.insert_new_knowledge(
            owner.id,
            KnowledgeForm(name='Source', description=''),
        )
        other_kb = await Knowledges.insert_new_knowledge(
            owner.id,
            KnowledgeForm(name='Other', description=''),
        )
        operation_id = str(uuid.uuid4())
        file_id = file_id_for(owner.id, source_kb.id, operation_id)
        target = Storage.prepare_upload(file_id + '.md')
        _, path = Storage.upload_prepared(io.BytesIO(b'original body'), target, {})
        await Files.insert_new_file(
            owner.id,
            FileForm(
                id=file_id,
                filename='note.md',
                path=path,
                data={'status': 'completed', 'content': 'server indexed text'},
                meta={
                    'name': 'note.md',
                    'content_type': 'text/markdown',
                    'knowledge_import_lifecycle': {
                        'version': 1,
                        'owner_id': owner.id,
                        'knowledge_id': source_kb.id,
                        'operation_id': operation_id,
                        'target': target,
                        'state': 'completed',
                        'lease_until': 0,
                        'lease_token': 'old',
                        'generation': 1,
                    },
                },
            ),
        )
        await Knowledges.add_file_to_knowledge_by_id(
            source_kb.id,
            file_id,
            owner.id,
        )
        stored = await Files.get_file_by_id(file_id)
        batch_file = stored.model_dump(mode='json')
        batch_file['filename'] = 'client-name.md'
        batch_file['data'] = {'content': 'client-injected body'}
        batch_file['meta'] = {'name': 'client-name.md'}

        app = FastAPI()
        app.include_router(retrieval_api.router, prefix='/api/v1/retrieval')
        app.dependency_overrides[get_verified_user] = lambda: owner
        vector_client = AsyncMock()

        try:
            with (
                patch.object(retrieval_api, 'ASYNC_VECTOR_DB_CLIENT', vector_client),
                patch.object(retrieval_api, 'save_docs_to_vector_db') as save_docs,
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url='http://test',
                ) as client:
                    response = await client.post(
                        '/api/v1/retrieval/process/file',
                        json={
                            'file_id': file_id,
                            'content': 'client-injected body',
                            'collection_name': other_kb.id,
                        },
                    )
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertEqual(
                        response.json()['detail'],
                        'knowledge_import_scope_mismatch',
                    )

                    batch = await client.post(
                        '/api/v1/retrieval/process/files/batch',
                        json={
                            'files': [batch_file],
                            'collection_name': other_kb.id,
                        },
                    )
                    self.assertEqual(batch.status_code, 200, batch.text)
                    self.assertEqual(batch.json()['results'], [])
                    self.assertEqual(
                        [error['error'] for error in batch.json()['errors']],
                        ['knowledge_import_scope_mismatch'],
                    )

                    same_scope = await client.post(
                        '/api/v1/retrieval/process/files/batch',
                        json={
                            'files': [batch_file],
                            'collection_name': source_kb.id,
                        },
                    )
                    self.assertEqual(same_scope.status_code, 200, same_scope.text)
                    self.assertEqual(
                        same_scope.json()['results'][0]['status'],
                        'completed',
                    )

                save_docs.assert_called_once()
                docs = save_docs.call_args.args[1]
                self.assertEqual(docs[0].page_content, 'server indexed text')
                self.assertEqual(docs[0].metadata['name'], 'note.md')
                self.assertEqual(docs[0].metadata['file_id'], file_id)
                vector_client.query.assert_not_awaited()
                vector_client.delete_collection.assert_not_awaited()
        finally:
            Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(
                    delete(KnowledgeFile).where(
                        KnowledgeFile.knowledge_id.in_([source_kb.id, other_kb.id])
                    )
                )
                await db.execute(delete(File).where(File.id == file_id))
                await db.execute(
                    delete(Knowledge).where(
                        Knowledge.id.in_([source_kb.id, other_kb.id])
                    )
                )
                await db.execute(delete(User).where(User.id == owner.id))
                await db.commit()

    async def test_real_write_grant_import_then_read_only_then_revoke(self):
        owner = await Users.insert_new_user(
            str(uuid.uuid4()),
            'KB owner',
            f'{uuid.uuid4()}@example.test',
            role='user',
        )
        writer = await Users.insert_new_user(
            str(uuid.uuid4()),
            'Writer',
            f'{uuid.uuid4()}@example.test',
            role='user',
        )
        kb = await Knowledges.insert_new_knowledge(
            owner.id,
            KnowledgeForm(name='Grant lifecycle', description=''),
        )
        await AccessGrants.grant_access('knowledge', kb.id, 'user', writer.id, 'write')

        app = FastAPI()
        app.include_router(knowledge_api.router, prefix='/api/v1/knowledge')
        app.include_router(files_api.router, prefix='/api/v1/files')
        app.dependency_overrides[get_verified_user] = lambda: writer
        file_id = None
        target = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url='http://test',
            ) as client:
                imported = await client.post(
                    f'/api/v1/knowledge/{kb.id}/import/file',
                    data={
                        'source_path': 'grant/image.png',
                        'source_hash': 'c' * 64,
                        'operation_id': str(uuid.uuid4()),
                        'kind': 'image',
                    },
                    files={'file': ('image.png', b'granted image', 'image/png')},
                )
                self.assertEqual(imported.status_code, 200, imported.text)
                file_id = imported.json()['file_id']
                stored = await Files.get_file_by_id(file_id)
                target = stored.meta['knowledge_import_lifecycle']['target']
                self.assertTrue(await Knowledges.has_file(kb.id, file_id))
                self.assertEqual(
                    (await client.get(f'/api/v1/files/{file_id}/content')).status_code,
                    200,
                )

                await AccessGrants.revoke_access(
                    'knowledge',
                    kb.id,
                    'user',
                    writer.id,
                    'write',
                )
                await AccessGrants.grant_access(
                    'knowledge',
                    kb.id,
                    'user',
                    writer.id,
                    'read',
                )
                self.assertEqual(
                    (await client.get(f'/api/v1/files/{file_id}/content')).status_code,
                    200,
                )
                denied_rename = await client.post(
                    f'/api/v1/files/{file_id}/rename',
                    json={'filename': 'denied.png'},
                )
                self.assertEqual(denied_rename.status_code, 404, denied_rename.text)

                await AccessGrants.revoke_access(
                    'knowledge',
                    kb.id,
                    'user',
                    writer.id,
                    'read',
                )
                self.assertEqual(
                    (await client.get(f'/api/v1/files/{file_id}/content')).status_code,
                    404,
                )
                with (
                    patch.object(knowledge_api.Files, 'insert_new_file', new=AsyncMock()) as insert,
                    patch.object(Storage, 'upload_prepared') as upload,
                ):
                    denied = await client.post(
                        f'/api/v1/knowledge/{kb.id}/import/file',
                        data={
                            'source_path': 'grant/denied.png',
                            'source_hash': 'd' * 64,
                            'operation_id': str(uuid.uuid4()),
                            'kind': 'image',
                        },
                        files={'file': ('denied.png', b'denied image', 'image/png')},
                    )
                self.assertEqual(denied.status_code, 403, denied.text)
                insert.assert_not_awaited()
                upload.assert_not_called()
        finally:
            if target:
                Storage.delete_prepared(target)
            async with get_async_db_context() as db:
                await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == kb.id))
                if file_id:
                    await db.execute(delete(File).where(File.id == file_id))
                await db.execute(delete(Knowledge).where(Knowledge.id == kb.id))
                await db.execute(delete(User).where(User.id.in_([owner.id, writer.id])))
                await db.commit()
