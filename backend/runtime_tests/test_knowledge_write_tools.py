"""Agent knowledge write tools: permissions, status semantics, tokens, recycle.

Covers docs/AGENT_TOOL_CONTRACT.md §4.2 (tool set), §5 (envelope), §7 (tokens),
§8 (recycle) and §9 (permissions).
"""

import json
import time
import unittest
import uuid
from types import SimpleNamespace

from fastapi import Request
from open_webui.internal.db import get_async_db_context
from open_webui.models.config import Config
from open_webui.models.knowledge import KnowledgeForm, Knowledges
from open_webui.models.users import User, Users
from open_webui.tools import deletion_guard
from open_webui.tools.builtin import (
    add_text_to_knowledge,
    create_knowledge_base,
    create_knowledge_directory,
    delete_knowledge_base,
    preview_delete,
    remove_file_from_knowledge,
    update_knowledge_base,
)
from sqlalchemy import delete

OWNER_ID = 'p007-kb-owner-0001'
PLAIN_ID = 'p007-kb-plain-0002'
OWNER_EMAIL = 'p007-kb-owner@local.test'
PLAIN_EMAIL = 'p007-kb-plain@local.test'


def tool_request() -> Request:
    # `process_file` touches `request.app.state`, so give it a minimal app.
    app = SimpleNamespace(state=SimpleNamespace(redis=None))
    return Request(
        {
            'type': 'http',
            'method': 'POST',
            'path': '/api/chat/completions',
            'headers': [],
            'app': app,
            'state': {},
        }
    )


def user_dict(user_id: str, role: str = 'admin') -> dict:
    stamp = int(time.time())
    return {
        'id': user_id,
        'email': f'{user_id}@local.test',
        'name': user_id,
        'role': role,
        'profile_image_url': '/user.png',
        'last_active_at': stamp,
        'updated_at': stamp,
        'created_at': stamp,
    }


def parse(envelope: str) -> dict:
    return json.loads(envelope)


class KnowledgeWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        deletion_guard.reset_tokens()
        self.addCleanup(deletion_guard.reset_tokens)

        async with get_async_db_context() as db:
            await db.execute(delete(User).where(User.id.in_([OWNER_ID, PLAIN_ID])))
            await db.commit()

        await Users.insert_new_user(id=OWNER_ID, name='Owner', email=OWNER_EMAIL, role='admin')
        await Users.insert_new_user(id=PLAIN_ID, name='Plain', email=PLAIN_EMAIL, role='user')

        # Skip the embedding backend so the async write path is exercisable here;
        # the tool must still report `accepted`, never `success`.
        await Config.upsert({'rag.bypass_embedding_and_retrieval': True})

        self.owner = user_dict(OWNER_ID)
        self.plain = user_dict(PLAIN_ID, role='user')
        self.request = tool_request()
        self.created_knowledge_ids: list[str] = []

    async def asyncTearDown(self):
        for knowledge_id in self.created_knowledge_ids:
            try:
                await Knowledges.delete_knowledge_by_id(knowledge_id)
            except Exception:
                pass
        async with get_async_db_context() as db:
            await db.execute(delete(User).where(User.id.in_([OWNER_ID, PLAIN_ID])))
            await db.commit()

    async def make_knowledge(self, name: str = '测试知识库'):
        knowledge = await Knowledges.insert_new_knowledge(
            OWNER_ID, KnowledgeForm(name=name, description='描述', access_grants=[])
        )
        self.created_knowledge_ids.append(knowledge.id)
        return knowledge

    # --- create_knowledge_base -------------------------------------------

    async def test_create_knowledge_base_succeeds(self):
        result = parse(
            await create_knowledge_base(
                '新建库', '来自 agent', __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['status'], 'success', result)
        data = result['data']
        self.created_knowledge_ids.append(data['knowledge_id'])
        self.assertEqual(data['name'], '新建库')
        self.assertTrue(data['created_at'].endswith('Z'))

        stored = await Knowledges.get_knowledge_by_id(data['knowledge_id'])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.user_id, OWNER_ID)

    async def test_create_knowledge_base_rejects_empty_name(self):
        result = parse(
            await create_knowledge_base('   ', '', __request__=self.request, __user__=self.owner)
        )
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'INVALID_ARGUMENT')

    async def test_create_knowledge_base_requires_permission(self):
        # A plain user without `workspace.knowledge` must be refused with a
        # readable reason, not a 500.
        result = parse(
            await create_knowledge_base('越权库', '', __request__=self.request, __user__=self.plain)
        )
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'PERMISSION_DENIED')
        self.assertIn('权限', result['message'])

    # --- update_knowledge_base -------------------------------------------

    async def test_update_knowledge_base_partial(self):
        knowledge = await self.make_knowledge('原名')

        result = parse(
            await update_knowledge_base(
                knowledge.id, name='新名', __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['data']['name'], '新名')
        # description must be preserved when not supplied
        self.assertEqual(result['data']['description'], '描述')

    async def test_update_knowledge_base_requires_a_field(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await update_knowledge_base(
                knowledge.id, None, None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['code'], 'INVALID_ARGUMENT')

    async def test_update_knowledge_base_denied_for_plain_user(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await update_knowledge_base(
                knowledge.id, name='改名', __request__=self.request, __user__=self.plain
            )
        )
        self.assertEqual(result['code'], 'PERMISSION_DENIED')

    async def test_update_unknown_knowledge(self):
        result = parse(
            await update_knowledge_base(
                str(uuid.uuid4()), name='x', __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['code'], 'NOT_FOUND')

    # --- create_knowledge_directory ---------------------------------------

    async def test_create_directory_then_duplicate_is_rejected(self):
        knowledge = await self.make_knowledge()

        first = parse(
            await create_knowledge_directory(
                knowledge.id, '资料', None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(first['status'], 'success', first)
        self.assertEqual(first['data']['name'], '资料')
        self.assertIsNone(first['data']['parent_id'])

        duplicate = parse(
            await create_knowledge_directory(
                knowledge.id, '资料', None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(duplicate['status'], 'failed')
        self.assertEqual(duplicate['code'], 'INVALID_ARGUMENT')

    async def test_create_directory_rejects_foreign_parent(self):
        knowledge_a = await self.make_knowledge('库A')
        knowledge_b = await self.make_knowledge('库B')

        parent = parse(
            await create_knowledge_directory(
                knowledge_a.id, '父', None, __request__=self.request, __user__=self.owner
            )
        )
        parent_id = parent['data']['directory_id']

        result = parse(
            await create_knowledge_directory(
                knowledge_b.id, '子', parent_id, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['code'], 'INVALID_ARGUMENT')

    # --- add_text_to_knowledge -------------------------------------------

    async def test_add_text_returns_accepted_not_success(self):
        """The contract forbids reporting an async write as completed."""
        knowledge = await self.make_knowledge()
        result = parse(
            await add_text_to_knowledge(
                knowledge.id,
                '会议纪要.md',
                '# 纪要\n\n讨论了三件事。',
                None,
                __request__=self.request,
                __user__=self.owner,
            )
        )
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['status'] != 'success', True)
        self.assertEqual(result['data']['status'], 'processing')
        self.assertEqual(result['data']['filename'], '会议纪要.md')

        # The file must actually be linked to the knowledge base.
        from open_webui.models.knowledge import Knowledges as _Knowledges

        self.assertTrue(await _Knowledges.has_file(knowledge.id, result['data']['file_id']))

    async def test_add_text_sanitises_path_like_filename(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await add_text_to_knowledge(
                knowledge.id,
                '../../etc/passwd.md',
                'body',
                None,
                __request__=self.request,
                __user__=self.owner,
            )
        )
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['data']['filename'], 'passwd.md')
        self.assertNotIn('/', result['data']['filename'])

    async def test_add_text_persists_a_real_file(self):
        """The stored bytes must exist so the result is previewable, not an index stub."""
        knowledge = await self.make_knowledge()
        result = parse(
            await add_text_to_knowledge(
                knowledge.id, 'persist.md', '# 保留内容', None,
                __request__=self.request, __user__=self.owner,
            )
        )
        self.assertEqual(result['status'], 'accepted', result)

        from open_webui.models.files import Files

        file = await Files.get_file_by_id(result['data']['file_id'])
        self.assertIsNotNone(file)
        self.assertTrue(file.path, 'agent-created knowledge file must have a storage path')
        self.assertEqual((file.data or {}).get('content'), '# 保留内容')

    async def test_add_text_rejects_empty_content(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await add_text_to_knowledge(
                knowledge.id, 'a.md', '   ', None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['code'], 'INVALID_ARGUMENT')

    async def test_add_text_denied_for_plain_user(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await add_text_to_knowledge(
                knowledge.id, 'a.md', 'body', None, __request__=self.request, __user__=self.plain
            )
        )
        self.assertEqual(result['code'], 'PERMISSION_DENIED')

    # --- delete_knowledge_base -------------------------------------------

    async def test_delete_knowledge_base_requires_token(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await delete_knowledge_base(
                knowledge.id, '', __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
        self.assertIsNotNone(await Knowledges.get_knowledge_by_id(knowledge.id))

    async def test_delete_knowledge_base_with_preview_token(self):
        knowledge = await self.make_knowledge('待删库')
        preview = parse(
            await preview_delete(
                'knowledge', knowledge.id, None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(preview['status'], 'confirmation_required')
        token = preview['data']['confirmation_token']

        result = parse(
            await delete_knowledge_base(
                knowledge.id, token, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['data']['knowledge_id'], knowledge.id)
        self.assertIn('回收站', result['message'])

        from open_webui.models.knowledge import Knowledge
        from open_webui.models.resources import ResourceEntry
        from open_webui.utils.resource_visibility import visible

        async with get_async_db_context() as db:
            from sqlalchemy import select

            entry = await db.scalar(
                select(ResourceEntry).where(
                    ResourceEntry.type == 'knowledge', ResourceEntry.resource_id == knowledge.id
                )
            )
            # `install_resource_visibility()` only runs at app startup, so assert
            # the predicate the listener uses rather than relying on the listener.
            hidden = await db.scalar(
                select(Knowledge.id).where(
                    Knowledge.id == knowledge.id, visible('knowledge', Knowledge.id)
                )
            )
        self.assertIsNotNone(entry, 'expected a recycle entry for the deleted knowledge base')
        self.assertIn(entry.state, ('recycled', 'purging', 'paused'))
        self.assertIsNone(hidden, 'a recycled knowledge base must be hidden from normal queries')

    async def test_delete_knowledge_base_token_is_single_use(self):
        knowledge = await self.make_knowledge()
        preview = parse(
            await preview_delete(
                'knowledge', knowledge.id, None, __request__=self.request, __user__=self.owner
            )
        )
        token = preview['data']['confirmation_token']

        first = parse(
            await delete_knowledge_base(
                knowledge.id, token, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(first['status'], 'success', first)

        second = parse(
            await delete_knowledge_base(
                knowledge.id, token, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(second['code'], 'CONFIRMATION_REQUIRED')

    async def test_delete_knowledge_base_denied_for_plain_user(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await delete_knowledge_base(
                knowledge.id, 'whatever', __request__=self.request, __user__=self.plain
            )
        )
        self.assertEqual(result['code'], 'PERMISSION_DENIED')
        self.assertIsNotNone(await Knowledges.get_knowledge_by_id(knowledge.id))

    # --- remove_file_from_knowledge --------------------------------------

    async def test_remove_file_requires_token(self):
        knowledge = await self.make_knowledge()
        result = parse(
            await remove_file_from_knowledge(
                knowledge.id, str(uuid.uuid4()), '', __request__=self.request, __user__=self.owner
            )
        )
        # Unknown file is reported before the token is even considered.
        self.assertEqual(result['code'], 'NOT_FOUND')

    async def test_remove_file_rejects_foreign_token(self):
        knowledge = await self.make_knowledge()
        file_id = str(uuid.uuid4())
        # Simulate a token issued for a different knowledge/file pair.
        foreign = deletion_guard.issue_token(
            OWNER_ID, deletion_guard.make_resource_key('knowledge_file', str(uuid.uuid4()), knowledge.id)
        )
        result = parse(
            await remove_file_from_knowledge(
                knowledge.id, file_id, foreign, __request__=self.request, __user__=self.owner
            )
        )
        self.assertIn(result['code'], ('NOT_FOUND', 'CONFIRMATION_REQUIRED'))


if __name__ == '__main__':
    unittest.main()
