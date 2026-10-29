"""Batch directory import and attach-only file placement (P-006 S4).

Covers the markdown-import groundwork on the knowledge router:
- POST /{id}/directory/batch get-or-create trees (idempotent, verbatim names)
- path-normalization security rules (traversal/absolute/drive-letter rejected
  before any write; percent-decoding happens first, so %2F is a separator)
- POST /{id}/file/add with `process` flag (attach-only placement skips
  vectorization; default true keeps the legacy FILE_NOT_PROCESSED gate)
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from open_webui.constants import ERROR_MESSAGES
from sqlalchemy import delete

from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File, FileForm, Files
from open_webui.models.knowledge import (
    Knowledge,
    KnowledgeDirectory,
    KnowledgeFile,
    KnowledgeForm,
    Knowledges,
)
from open_webui.routers import knowledge as api
from open_webui.utils.auth import get_verified_user


class KnowledgeImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = SimpleNamespace(id='kb-import-owner', role='user')
        self.other = SimpleNamespace(id='kb-import-other', role='user')
        self.knowledge_ids: list[str] = []
        self.file_ids: list[str] = []
        await self._cleanup()
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/api/v1/knowledge')
        self.app.dependency_overrides[get_verified_user] = lambda: self.owner
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def asyncTearDown(self):
        await self._cleanup()

    async def _cleanup(self):
        async with get_async_db_context() as db:
            await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id.in_(self.knowledge_ids)))
            await db.execute(delete(KnowledgeDirectory).where(KnowledgeDirectory.knowledge_id.in_(self.knowledge_ids)))
            await db.execute(delete(Knowledge).where(Knowledge.id.in_(self.knowledge_ids)))
            await db.execute(delete(File).where(File.id.in_(self.file_ids)))
            await db.commit()

    async def create_knowledge(self, user_id=None):
        kb = await Knowledges.insert_new_knowledge(
            user_id=user_id or self.owner.id,
            form_data=KnowledgeForm(name='Import KB', description='P-006 S4'),
        )
        self.assertIsNotNone(kb)
        self.knowledge_ids.append(kb.id)
        return kb

    async def create_file(self, data=None, user_id=None, filename='note.md'):
        file = await Files.insert_new_file(
            user_id=user_id or self.owner.id,
            form_data=FileForm(
                id=str(uuid.uuid4()),
                filename=filename,
                path=f'/tmp/{uuid.uuid4()}',
                data={'content': 'note body'} if data is None else data,
            ),
        )
        self.assertIsNotNone(file)
        self.file_ids.append(file.id)
        return file

    async def batch(self, kb_id, directories, user=None):
        if user is not None:
            self.app.dependency_overrides[get_verified_user] = lambda: user
        try:
            return await self.client.post(
                f'/api/v1/knowledge/{kb_id}/directory/batch', json={'directories': directories}
            )
        finally:
            if user is not None:
                self.app.dependency_overrides[get_verified_user] = lambda: self.owner

    async def directory_count(self, kb_id):
        return len(await Knowledges.get_all_directories(kb_id))

    # --- batch directory get-or-create -----------------------------------

    async def test_batch_directories_create_tree_and_are_idempotent(self):
        kb = await self.create_knowledge()
        response = await self.batch(kb.id, ['19.Zabbix', 'linux基础/sub', 'linux基础/swap'])
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()
        # Every requested path AND its intermediate levels are mapped.
        self.assertEqual(set(mapping), {'19.Zabbix', 'linux基础', 'linux基础/sub', 'linux基础/swap'})

        # Names are stored verbatim (no slugification), parents resolved correctly.
        sub = await Knowledges.get_directory_by_id(mapping['linux基础/sub'])
        parent = await Knowledges.get_directory_by_id(mapping['linux基础'])
        self.assertEqual(sub.name, 'sub')
        self.assertEqual(sub.parent_id, parent.id)
        self.assertEqual(parent.name, 'linux基础')
        self.assertIsNone(parent.parent_id)
        zabbix = await Knowledges.get_directory_by_id(mapping['19.Zabbix'])
        self.assertEqual(zabbix.name, '19.Zabbix')
        self.assertIsNone(zabbix.parent_id)

        # Idempotent: a second identical call returns the same IDs for the
        # paths it re-requests and creates no duplicate directories.
        again_response = await self.batch(kb.id, ['linux基础/sub', '19.Zabbix'])
        self.assertEqual(again_response.status_code, 200, again_response.text)
        again = again_response.json()
        self.assertEqual(
            {k: mapping[k] for k in again},
            again,
        )
        self.assertEqual(await self.directory_count(kb.id), 4)

    async def test_directory_names_keep_encoding_dots_and_spaces(self):
        kb = await self.create_knowledge()
        response = await self.batch(kb.id, ['a b/c.d', 'x%20y', '%E7%AC%94%E8%AE%B0'])
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()
        self.assertEqual(
            set(mapping), {'a b', 'a b/c.d', 'x y', '笔记'}
        )
        names = {d.name for d in await Knowledges.get_all_directories(kb.id)}
        self.assertEqual(names, {'a b', 'c.d', 'x y', '笔记'})

    async def test_encoded_slash_decodes_into_a_separator(self):
        kb = await self.create_knowledge()
        response = await self.batch(kb.id, ['a%2Fb'])
        self.assertEqual(response.status_code, 200, response.text)
        mapping = response.json()
        self.assertEqual(set(mapping), {'a', 'a/b'})
        child = await Knowledges.get_directory_by_id(mapping['a/b'])
        self.assertEqual(child.name, 'b')
        self.assertEqual(child.parent_id, mapping['a'])

    async def test_dangerous_paths_rejected_atomically_before_any_write(self):
        kb = await self.create_knowledge()
        for bad in ['%2e%2e/etc', 'C:/x', '..\\x', '/abs', '', '.', '%2fetc']:
            response = await self.batch(kb.id, [bad])
            self.assertEqual(response.status_code, 400, f'{bad!r}: {response.text}')
            self.assertEqual(await self.directory_count(kb.id), 0, bad)

        # One bad entry must not leave the good ones behind (validate-all-
        # before-any-write).
        response = await self.batch(kb.id, ['good-dir', '..'])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(await self.directory_count(kb.id), 0)

    async def test_batch_directories_require_knowledge_write_access(self):
        kb = await self.create_knowledge()
        response = await self.batch(kb.id, ['notes'], user=self.other)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(await self.directory_count(kb.id), 0)

    # --- file/add process flag --------------------------------------------

    async def test_attach_only_image_is_placed_without_vectorization(self):
        kb = await self.create_knowledge()
        directories = (await self.batch(kb.id, ['19.Zabbix/images'])).json()
        file = await self.create_file(data={}, filename='diagram.png')
        with patch.object(api, 'process_file', new=AsyncMock()) as vectorize:
            response = await self.client.post(
                f'/api/v1/knowledge/{kb.id}/file/add',
                json={'file_id': file.id, 'directory_id': directories['19.Zabbix/images'], 'process': False},
            )
        self.assertEqual(response.status_code, 200, response.text)
        vectorize.assert_not_awaited()
        self.assertTrue(await Knowledges.has_file(kb.id, file.id))
        placed = {
            file.id: directory_id
            for file, directory_id in await Knowledges.get_files_with_directory_ids(kb.id)
        }
        self.assertEqual(placed[file.id], directories['19.Zabbix/images'])

    async def test_default_process_vectorizes_extracted_content(self):
        kb = await self.create_knowledge()
        file = await self.create_file()
        with patch.object(api, 'process_file', new=AsyncMock()) as vectorize:
            response = await self.client.post(
                f'/api/v1/knowledge/{kb.id}/file/add', json={'file_id': file.id}
            )
        self.assertEqual(response.status_code, 200, response.text)
        vectorize.assert_awaited_once()
        self.assertEqual(vectorize.await_args.args[1].collection_name, kb.id)
        self.assertTrue(await Knowledges.has_file(kb.id, file.id))

    async def test_default_process_still_gates_unextracted_files(self):
        kb = await self.create_knowledge()
        file = await self.create_file(data={})
        with patch.object(api, 'process_file', new=AsyncMock()) as vectorize:
            response = await self.client.post(
                f'/api/v1/knowledge/{kb.id}/file/add', json={'file_id': file.id}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], ERROR_MESSAGES.FILE_NOT_PROCESSED)
        vectorize.assert_not_awaited()
        self.assertFalse(await Knowledges.has_file(kb.id, file.id))

    async def test_file_add_requires_knowledge_write_access_before_reading_file(self):
        kb = await self.create_knowledge()
        file = await self.create_file(user_id=self.owner.id)
        self.app.dependency_overrides[get_verified_user] = lambda: self.other
        try:
            with patch.object(api, 'Files', new=AsyncMock()) as files:
                response = await self.client.post(
                    f'/api/v1/knowledge/{kb.id}/file/add', json={'file_id': file.id}
                )
        finally:
            self.app.dependency_overrides[get_verified_user] = lambda: self.owner
        self.assertEqual(response.status_code, 400, response.text)
        files.get_file_by_id.assert_not_awaited()
        self.assertFalse(await Knowledges.has_file(kb.id, file.id))


if __name__ == '__main__':
    unittest.main()
