"""Regression tests for recycled-resource vector filtering and directory purge.

Vector-filter tests use simulated metadata and a real in-memory SQLite database
with fake ResourceEntry/KnowledgeFile rows, so they need no vector backend.
The purge test drives the real resource_service.recycle/purge against the same
in-memory database and asserts that a move_files recycle cannot be purged into
orphaned children.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'recycle-guard-test-secret')

from open_webui.internal.db import Base
from open_webui.models.files import File
from open_webui.models.knowledge import Knowledge, KnowledgeDirectory, KnowledgeFile
from open_webui.models.resources import ResourceEntry
from open_webui.retrieval.vector import resource_guard
from open_webui.retrieval.vector.main import GetResult, SearchResult
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def make_db_engine():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def make_sync_db_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def make_search_result(identities, metadatas):
    return SearchResult(
        ids=[identities],
        documents=[[f'doc:{identity}' for identity in identities]],
        metadatas=[metadatas],
        distances=[[0.1] * len(identities)],
    )


def resource_entry(kind, resource_id, user_id='u1', state='recycled', data=None):
    from open_webui.utils.resource_service import now, uid
    return ResourceEntry(
        id=uid(), type=kind, resource_id=resource_id, user_id=user_id,
        state=state, title='test', deleted_at=now(), expires_at=now() + 1000,
        generation=1, data=data or {},
    )


def knowledge_row(id, user_id='u1'):
    from open_webui.utils.resource_service import now
    return Knowledge(id=id, user_id=user_id, name='KB', description='', created_at=now(), updated_at=now())


def directory_row(id, knowledge_id, parent_id=None, user_id='u1'):
    from open_webui.utils.resource_service import now
    return KnowledgeDirectory(
        id=id, knowledge_id=knowledge_id, parent_id=parent_id, name=id,
        user_id=user_id, created_at=now(), updated_at=now(),
    )


def link_row(id, knowledge_id, file_id, directory_id=None, user_id='u1'):
    from open_webui.utils.resource_service import now
    return KnowledgeFile(
        id=id, knowledge_id=knowledge_id, file_id=file_id, directory_id=directory_id,
        user_id=user_id, created_at=now(), updated_at=now(),
    )


class GuardFilterTests(unittest.TestCase):
    """filter_result must drop rows whose metadata names a blocked id."""

    result = staticmethod(make_search_result)

    def test_blocks_all_metadata_aliases_and_keeps_clean_rows(self):
        blocked = {'file-1', 'kb-1', 'kb-base-1', 'uuid-1', 'dir-1'}
        identities = ['clean', 'by-id', 'by-file', 'by-kb', 'by-base', 'by-uuid', 'by-dir']
        metadatas = [
            {}, {'id': 'uuid-1'}, {'file_id': 'file-1'},
            {'knowledge_id': 'kb-1'}, {'knowledge_base_id': 'kb-base-1'},
            {'name': 'safe', 'file_id': 'file-1'},
            {'directory_id': 'dir-1'},
        ]
        out = resource_guard.filter_result(self.result(identities, metadatas), blocked)
        self.assertEqual(out.ids, [['clean']])
        self.assertEqual(out.documents, [['doc:clean']])
        self.assertEqual(out.metadatas, [[{}]])
        self.assertEqual(out.distances, [[0.1]])

    def test_empty_blocked_or_empty_result_is_untouched(self):
        result = self.result(['a'], [{'file_id': 'a'}])
        self.assertIs(resource_guard.filter_result(result, set()), result)
        empty = SearchResult(ids=[[]], documents=[[]], metadatas=[[]], distances=[[]])
        self.assertIs(resource_guard.filter_result(empty, {'a'}), empty)

    def test_get_result_without_distances(self):
        result = GetResult(ids=[['a', 'b']], documents=[['a', 'b']],
                           metadatas=[[{'file_id': 'a'}, {'file_id': 'safe'}]])
        out = resource_guard.filter_result(result, {'a'})
        self.assertEqual(out.ids, [['b']])
        self.assertFalse(hasattr(out, 'distances'))


class BlockedIdsTests(unittest.TestCase):
    """blocked_ids resolves every entry type to the identifiers vectors use."""

    def setUp(self):
        self.engine, self.Session = make_sync_db_engine()

    def tearDown(self):
        self.engine.dispose()

    def _blocked(self, collection_name=None):
        import open_webui.internal.db as db_module
        with self.Session() as db:
            class FakeSessionLocal:
                def __enter__(self):
                    return db

                def __exit__(self, *args):
                    return False

            with patch.object(db_module, 'SessionLocal', FakeSessionLocal):
                return resource_guard.blocked_ids(collection_name)

    def test_recycled_file_blocks_file_id(self):
        with self.Session() as db:
            db.add(resource_entry('file', 'f1'))
            db.commit()
        self.assertIn('f1', self._blocked())

    def test_recycled_knowledge_blocks_whole_collection(self):
        with self.Session() as db:
            db.add(resource_entry('knowledge', 'kb-1'))
            db.commit()
        self.assertIn('kb-1', self._blocked())

    def test_recycled_directory_blocks_descendant_files_and_dirs(self):
        with self.Session() as db:
            db.add(knowledge_row('kb-1'))
            db.add(directory_row('d-root', 'kb-1'))
            db.add(directory_row('d-child', 'kb-1', parent_id='d-root'))
            db.add(directory_row('d-grandchild', 'kb-1', parent_id='d-child'))
            db.add(link_row('link-1', 'kb-1', 'file-root', directory_id='d-root'))
            db.add(link_row('link-2', 'kb-1', 'file-child', directory_id='d-child'))
            db.add(link_row('link-3', 'kb-1', 'file-grandchild', directory_id='d-grandchild'))
            db.add(link_row('link-4', 'kb-1', 'file-outside'))
            db.add(resource_entry('knowledge_directory', 'd-root'))
            db.commit()
        blocked = self._blocked()
        for identity in ('d-root', 'd-child', 'd-grandchild', 'file-root', 'file-child', 'file-grandchild'):
            self.assertIn(identity, blocked)
        self.assertNotIn('file-outside', blocked)

    def test_recycled_knowledge_file_link_blocks_file_id(self):
        with self.Session() as db:
            db.add(knowledge_row('kb-1'))
            db.add(link_row('link-1', 'kb-1', 'file-1'))
            db.add(link_row('link-2', 'kb-1', 'file-2'))
            db.add(resource_entry('knowledge_file', 'link-1'))
            db.commit()
        blocked = self._blocked('kb-1')
        self.assertIn('file-1', blocked)
        self.assertIn('link-1', blocked)
        self.assertNotIn('file-2', blocked)
        # The file may still be linked in another, non-recycled knowledge base.
        self.assertNotIn('file-1', self._blocked('kb-2'))

    def test_recycled_directory_blocks_only_its_own_knowledge_base(self):
        with self.Session() as db:
            db.add(knowledge_row('kb-1'))
            db.add(directory_row('d-root', 'kb-1'))
            db.add(link_row('link-1', 'kb-1', 'file-root', directory_id='d-root'))
            db.add(resource_entry('knowledge_directory', 'd-root'))
            db.commit()
        self.assertIn('file-root', self._blocked('kb-1'))
        self.assertNotIn('file-root', self._blocked('kb-2'))

    def test_unrecycled_content_stays_searchable(self):
        with self.Session() as db:
            db.add(knowledge_row('kb-1'))
            db.add(link_row('link-1', 'kb-1', 'file-1'))
            db.commit()
        self.assertEqual(self._blocked(), set())

    def test_guard_search_hybrid_query_get_all_filter(self):
        """Every read path calls filter_result with the resolved blocked set."""
        wrapper = SimpleNamespace(
            search=lambda *a, **k: None, hybrid_search=lambda *a, **k: None,
            query=lambda *a, **k: None, get=lambda *a, **k: None,
        )
        from fastapi import HTTPException
        original = resource_guard.blocked_ids
        resource_guard.blocked_ids = lambda collection_name=None: {'file-1'}
        try:
            guard = resource_guard.ResourceVectorGuard(wrapper)

            def fake_result(*args, **kwargs):
                return make_search_result(['ok', 'blocked'], [{'file_id': 'safe'}, {'file_id': 'file-1'}])

            for name in ('search', 'hybrid_search', 'query', 'get'):
                setattr(wrapper, name, fake_result)
            for call in (
                lambda: guard.search('kb-1', [[0.1]]),
                lambda: guard.hybrid_search('kb-1', 'q', [[0.1]]),
                lambda: guard.query('kb-1', {}),
                lambda: guard.get('knowledge-bases'),
            ):
                out = call()
                self.assertEqual(out.ids, [['ok']])
            with self.assertRaises(HTTPException):
                guard.get('file-file-1')
        finally:
            resource_guard.blocked_ids = original


class DirectoryPurgeTests(unittest.IsolatedAsyncioTestCase):
    """A move_files recycle cannot be purged into orphaned children."""

    async def asyncSetUp(self):
        self.engine, self.Session = make_db_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        import open_webui.utils.resource_service as service
        self.service = service

        @__import__('contextlib').asynccontextmanager
        async def fake_get_async_db():
            async with self.Session() as session:
                yield session

        self.patches = [
            patch.object(service, 'get_async_db', fake_get_async_db),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.engine.dispose()

    async def test_purge_move_files_directory_is_rejected_not_orphaned(self):
        from fastapi import HTTPException
        from open_webui.utils.resource_visibility import including_recycled

        async with self.Session() as db:
            db.add(knowledge_row('kb-1'))
            db.add(directory_row('root', 'kb-1', parent_id=None))
            db.add(directory_row('child', 'kb-1', parent_id='root'))
            file = File(id='f1', user_id='u1', filename='a.txt', path='x', meta={}, data={},
                        created_at=self.service.now(), updated_at=self.service.now())
            db.add(file)
            db.add(link_row('link-1', 'kb-1', 'f1', directory_id='root'))
            await db.commit()

            root = await db.get(KnowledgeDirectory, 'root')
            with patch.object(self.service, 'including_recycled', including_recycled):
                entry = await self.service.recycle(db, root, 'knowledge_directory', 'u1', move_files=True)
                await db.commit()

            # Child directory and file link are relocated to root's parent.
            child = await db.get(KnowledgeDirectory, 'child')
            link = await db.get(KnowledgeFile, 'link-1')
            self.assertIsNone(child.parent_id)
            self.assertIsNone(link.directory_id)
            self.assertCountEqual(entry.data['moved'], [
                {'type': 'knowledge_directory', 'id': 'child', 'field': 'parent_id', 'target': None},
                {'type': 'knowledge_file', 'id': 'link-1', 'field': 'directory_id', 'target': None},
            ])

            async def checkpoint(_steps):
                await db.commit()

            with self.assertRaises(HTTPException) as ctx:
                await self.service.purge(db, root, 'knowledge_directory', entry, checkpoint)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn('moved contents', ctx.exception.detail)

            # Nothing was deleted: restore remains possible and children exist.
            self.assertIsNotNone(await db.get(KnowledgeDirectory, 'root'))
            self.assertIsNotNone(await db.get(KnowledgeDirectory, 'child'))
            self.assertIsNotNone(await db.get(KnowledgeFile, 'link-1'))
            self.assertEqual(entry.state, 'recycled')

            await self.service.restore(db, root, 'knowledge_directory', entry)
            await db.commit()
            self.assertEqual((await db.get(KnowledgeDirectory, 'child')).parent_id, 'root')
            self.assertEqual((await db.get(KnowledgeFile, 'link-1')).directory_id, 'root')


if __name__ == '__main__':
    unittest.main()
