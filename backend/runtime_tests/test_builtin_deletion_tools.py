"""Agent deletion guard: confirmation tokens, permissions and recycle semantics.

These tests exercise the real builtin tool functions against a real SQLite
database, plus the token store as a pure unit. They encode
`docs/AGENT_TOOL_CONTRACT.md` §7 (tokens), §8 (recycle) and §9 (permissions).
"""

import time
import unittest
import uuid
from unittest.mock import patch

from fastapi import Request
from open_webui.internal.db import get_async_db_context
from open_webui.models.notes import Note, NoteForm, Notes
from open_webui.models.users import User, Users
from open_webui.tools import deletion_guard
from open_webui.tools.builtin import delete_note, preview_delete
from sqlalchemy import delete

OWNER_ID = 'p007-owner-0001'
OTHER_ID = 'p007-other-0002'
OWNER_EMAIL = 'p007-owner@local.test'
OTHER_EMAIL = 'p007-other@local.test'


def tool_request() -> Request:
    return Request(
        {
            'type': 'http',
            'method': 'POST',
            'path': '/api/chat/completions',
            'headers': [],
            'app': None,
            'state': {},
        }
    )


def user_dict(user_id: str, role: str = 'admin') -> dict:
    # Mirrors the dict shape the chat middleware passes as __user__; UserModel
    # requires the three epoch timestamps.
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
    import json

    return json.loads(envelope)


class TokenStoreTests(unittest.TestCase):
    """deletion_guard is pure in-process state; test it without a database."""

    def setUp(self):
        deletion_guard.reset_tokens()

    def tearDown(self):
        deletion_guard.reset_tokens()

    def test_roundtrip_consumes_once(self):
        key = deletion_guard.make_resource_key('note', 'note-1')
        token = deletion_guard.issue_token(OWNER_ID, key)

        self.assertTrue(deletion_guard.consume_token(token, OWNER_ID, key))
        # Replay must fail: a token is single use.
        self.assertFalse(deletion_guard.consume_token(token, OWNER_ID, key))

    def test_bound_to_user(self):
        key = deletion_guard.make_resource_key('note', 'note-1')
        token = deletion_guard.issue_token(OWNER_ID, key)

        self.assertFalse(deletion_guard.consume_token(token, OTHER_ID, key))
        # A mismatched attempt must not burn the rightful owner's token.
        self.assertTrue(deletion_guard.consume_token(token, OWNER_ID, key))

    def test_bound_to_resource(self):
        token = deletion_guard.issue_token(OWNER_ID, deletion_guard.make_resource_key('note', 'note-1'))

        self.assertFalse(
            deletion_guard.consume_token(token, OWNER_ID, deletion_guard.make_resource_key('note', 'note-2'))
        )
        self.assertTrue(
            deletion_guard.consume_token(token, OWNER_ID, deletion_guard.make_resource_key('note', 'note-1'))
        )

    def test_expiry(self):
        key = deletion_guard.make_resource_key('note', 'note-1')
        with patch.object(deletion_guard, 'TOKEN_TTL_SECONDS', -1):
            token = deletion_guard.issue_token(OWNER_ID, key)

        self.assertFalse(deletion_guard.consume_token(token, OWNER_ID, key))

    def test_unknown_token(self):
        self.assertFalse(
            deletion_guard.consume_token('not-a-real-token', OWNER_ID, deletion_guard.make_resource_key('note', 'x'))
        )
        self.assertFalse(deletion_guard.consume_token('', OWNER_ID, 'note:x'))

    def test_capacity_is_bounded(self):
        with patch.object(deletion_guard, 'MAX_TOKENS', 5):
            tokens = [
                deletion_guard.issue_token(OWNER_ID, deletion_guard.make_resource_key('note', f'n{i}'))
                for i in range(12)
            ]

        self.assertLessEqual(deletion_guard.pending_count(), 5)
        # The oldest tokens must be the ones evicted.
        self.assertFalse(
            deletion_guard.consume_token(tokens[0], OWNER_ID, deletion_guard.make_resource_key('note', 'n0'))
        )
        self.assertTrue(
            deletion_guard.consume_token(tokens[-1], OWNER_ID, deletion_guard.make_resource_key('note', 'n11'))
        )

    def test_resource_key_shapes(self):
        self.assertEqual(deletion_guard.make_resource_key('note', 'n1'), 'note:n1')
        self.assertEqual(deletion_guard.make_resource_key('knowledge', 'k1'), 'knowledge:k1')
        self.assertEqual(
            deletion_guard.make_resource_key('knowledge_file', 'f1', 'k1'),
            'knowledge_file:k1:f1',
        )


class _DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        deletion_guard.reset_tokens()
        self.addCleanup(deletion_guard.reset_tokens)

        async with get_async_db_context() as db:
            await db.execute(delete(Note).where(Note.user_id.in_([OWNER_ID, OTHER_ID])))
            await db.execute(delete(User).where(User.id.in_([OWNER_ID, OTHER_ID])))
            await db.commit()

        await Users.insert_new_user(
            id=OWNER_ID, name='Owner', email=OWNER_EMAIL, role='admin'
        )
        await Users.insert_new_user(
            id=OTHER_ID, name='Other', email=OTHER_EMAIL, role='admin'
        )

        self.owner = user_dict(OWNER_ID)
        self.other = user_dict(OTHER_ID)
        self.request = tool_request()

    async def asyncTearDown(self):
        async with get_async_db_context() as db:
            await db.execute(delete(Note).where(Note.user_id.in_([OWNER_ID, OTHER_ID])))
            await db.execute(delete(User).where(User.id.in_([OWNER_ID, OTHER_ID])))
            await db.commit()

    async def make_note(self, user_id: str = OWNER_ID, title: str = '测试笔记'):
        return await Notes.insert_new_note(
            user_id,
            NoteForm(
                title=title,
                data={'content': {'md': '# 内容\n\n正文。'}},
                access_grants=[],
            ),
        )


class PreviewDeleteTests(_DatabaseTestCase):
    async def test_preview_note_returns_token_and_impact(self):
        note = await self.make_note()

        result = parse(
            await preview_delete(
                'note', note.id, None, __request__=self.request, __user__=self.owner
            )
        )

        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
        self.assertTrue(result['data']['confirmation_token'])
        impact = result['data']['impact']
        self.assertEqual(impact['resource_type'], 'note')
        self.assertEqual(impact['resource_id'], note.id)
        self.assertFalse(impact['is_shared'])
        self.assertTrue(impact['recoverable_until'].endswith('Z'))

    async def test_preview_unknown_note_is_not_found(self):
        result = parse(
            await preview_delete(
                'note', str(uuid.uuid4()), None, __request__=self.request, __user__=self.owner
            )
        )

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'NOT_FOUND')

    async def test_preview_by_non_owner_is_denied(self):
        note = await self.make_note()

        result = parse(
            await preview_delete('note', note.id, None, __request__=self.request, __user__=self.other)
        )

        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'PERMISSION_DENIED')
        # No token may be issued for a delete that cannot succeed.
        self.assertIsNone(result['data'])

    async def test_preview_rejects_bad_arguments(self):
        bad_type = parse(
            await preview_delete(
                'chat', 'x', None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(bad_type['code'], 'INVALID_ARGUMENT')

        missing_knowledge = parse(
            await preview_delete(
                'knowledge_file', 'f1', None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(missing_knowledge['code'], 'INVALID_ARGUMENT')

        empty_id = parse(
            await preview_delete('note', '', None, __request__=self.request, __user__=self.owner)
        )
        self.assertEqual(empty_id['code'], 'INVALID_ARGUMENT')

    async def test_preview_requires_request_context(self):
        note = await self.make_note()
        result = parse(await preview_delete('note', note.id, None, __request__=None, __user__=self.owner))
        self.assertEqual(result['code'], 'INTERNAL')


class DeleteNoteTests(_DatabaseTestCase):
    async def test_delete_without_token_is_refused(self):
        note = await self.make_note()

        result = parse(
            await delete_note(note.id, '', __request__=self.request, __user__=self.owner)
        )

        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
        # The note must still exist.
        self.assertIsNotNone(await Notes.get_note_by_id(note.id))

    async def test_delete_with_other_users_token_is_refused(self):
        note = await self.make_note()
        preview = parse(
            await preview_delete('note', note.id, None, __request__=self.request, __user__=self.owner)
        )
        token = preview['data']['confirmation_token']

        result = parse(
            await delete_note(note.id, token, __request__=self.request, __user__=self.other)
        )

        self.assertEqual(result['code'], 'PERMISSION_DENIED')
        self.assertIsNotNone(await Notes.get_note_by_id(note.id))

    async def test_delete_token_cannot_be_replayed(self):
        note = await self.make_note()
        preview = parse(
            await preview_delete('note', note.id, None, __request__=self.request, __user__=self.owner)
        )
        token = preview['data']['confirmation_token']

        first = parse(await delete_note(note.id, token, __request__=self.request, __user__=self.owner))
        self.assertEqual(first['status'], 'success', first)

        second = parse(await delete_note(note.id, token, __request__=self.request, __user__=self.owner))
        self.assertEqual(second['code'], 'CONFIRMATION_REQUIRED')

    async def test_delete_moves_note_to_recycle_bin(self):
        note = await self.make_note()
        preview = parse(
            await preview_delete('note', note.id, None, __request__=self.request, __user__=self.owner)
        )

        result = parse(
            await delete_note(
                note.id,
                preview['data']['confirmation_token'],
                __request__=self.request,
                __user__=self.owner,
            )
        )

        self.assertEqual(result['status'], 'success', result)
        self.assertEqual(result['code'], 'OK')
        self.assertEqual(result['data']['note_id'], note.id)
        self.assertTrue(result['data']['recycle_expires_at'].endswith('Z'))
        self.assertIn('回收站', result['message'])

        from open_webui.models.resources import ResourceEntry
        from open_webui.utils.resource_visibility import visible
        from sqlalchemy import select

        async with get_async_db_context() as db:
            entry = await db.scalar(
                select(ResourceEntry).where(
                    ResourceEntry.type == 'note', ResourceEntry.resource_id == note.id
                )
            )
            # `install_resource_visibility()` only runs at app startup, so assert
            # the predicate the listener uses rather than relying on the listener.
            hidden = await db.scalar(select(Note.id).where(Note.id == note.id, visible('note', Note.id)))

        self.assertIsNotNone(entry, 'expected a recycle entry for the deleted note')
        self.assertIn(entry.state, ('recycled', 'purging', 'paused'))
        self.assertIsNone(hidden, 'a recycled note must be hidden from normal queries')

    async def test_delete_unknown_note(self):
        result = parse(
            await delete_note(str(uuid.uuid4()), 'sometoken', __request__=self.request, __user__=self.owner)
        )
        self.assertEqual(result['code'], 'NOT_FOUND')

    async def test_delete_requires_request_context(self):
        note = await self.make_note()
        result = parse(await delete_note(note.id, 'tok', __request__=None, __user__=self.owner))
        self.assertEqual(result['code'], 'INTERNAL')


class KnowledgePreviewTests(_DatabaseTestCase):
    """`preview_delete` also serves the knowledge tools added in S2."""

    async def test_preview_unknown_knowledge(self):
        result = parse(
            await preview_delete(
                'knowledge', str(uuid.uuid4()), None, __request__=self.request, __user__=self.owner
            )
        )
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['code'], 'NOT_FOUND')

    async def test_preview_knowledge_permission_is_enforced(self):
        from open_webui.models.knowledge import KnowledgeForm, Knowledges

        # A plain 'user' (no group grant, not an admin) must be refused.
        plain_id = 'p007-plain-0003'
        await Users.insert_new_user(id=plain_id, name='Plain', email='p007-plain@local.test', role='user')
        plain = user_dict(plain_id, role='user')

        knowledge = await Knowledges.insert_new_knowledge(
            OWNER_ID,
            KnowledgeForm(name='私有库', description='仅所有者', access_grants=[]),
        )

        try:
            denied = parse(
                await preview_delete(
                    'knowledge', knowledge.id, None, __request__=self.request, __user__=plain
                )
            )
            self.assertEqual(denied['code'], 'PERMISSION_DENIED')

            allowed = parse(
                await preview_delete(
                    'knowledge', knowledge.id, None, __request__=self.request, __user__=self.owner
                )
            )
            self.assertEqual(allowed['status'], 'confirmation_required')
            self.assertEqual(allowed['data']['impact']['file_count'], 0)
            self.assertEqual(allowed['data']['impact']['directory_count'], 0)
        finally:
            await Knowledges.delete_knowledge_by_id(knowledge.id)
            async with get_async_db_context() as db:
                await db.execute(delete(User).where(User.id == plain_id))
                await db.commit()


if __name__ == '__main__':
    unittest.main()
