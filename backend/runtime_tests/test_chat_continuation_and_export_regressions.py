"""Regression coverage for shared chat continuation and export pagination.

These tests exercise the writable-access helper with fake users/chats, compile
the PostgreSQL search SQL without a live database, and inspect the SQLAlchemy
statement built by ``Chats.get_chats``.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'chat-continuation-test-secret')

from fastapi import HTTPException
from open_webui.main import verify_chat_ownership
from open_webui.models.chats import Chat, Chats, chat_search_message_content_match_sql
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite


class FakeUser:
    def __init__(self, id: str, role: str = 'user'):
        self.id = id
        self.role = role


def fake_chat(chat_id: str = 'chat-1', user_id: str = 'owner') -> SimpleNamespace:
    return SimpleNamespace(
        id=chat_id,
        user_id=user_id,
        title='title',
        chat={'history': {}, 'messages': {}},
        created_at=1,
        updated_at=1,
        share_id=None,
        archived=False,
        pinned=False,
        meta={},
        variables={},
        folder_id=None,
        tasks=None,
        summary=None,
        current_message_id=None,
        last_read_at=None,
        timer_at=None,
    )


class VerifyChatOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_can_complete(self):
        user = FakeUser('owner')
        with (
            patch('open_webui.main.is_temporary_chat_id', return_value=False),
            patch.object(Chats, 'get_accessible_chat_by_id', new=AsyncMock(return_value=fake_chat())) as accessible,
        ):
            await verify_chat_ownership('chat-1', user)
        accessible.assert_awaited_once_with('chat-1', user, permission='write')

    async def test_continue_share_user_can_complete(self):
        user = FakeUser('guest')
        with (
            patch('open_webui.main.is_temporary_chat_id', return_value=False),
            patch.object(Chats, 'get_accessible_chat_by_id', new=AsyncMock(return_value=fake_chat())) as accessible,
        ):
            await verify_chat_ownership('chat-1', user)
        accessible.assert_awaited_once_with('chat-1', user, permission='write')

    async def test_unauthorized_user_is_rejected(self):
        user = FakeUser('intruder')
        with (
            patch('open_webui.main.is_temporary_chat_id', return_value=False),
            patch.object(Chats, 'get_accessible_chat_by_id', new=AsyncMock(return_value=None)),
        ):
            with self.assertRaises(HTTPException) as caught:
                await verify_chat_ownership('chat-1', user)
        self.assertEqual(caught.exception.status_code, 404)


class AccessibleChatWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_grant_with_continue_share_mode_is_writable(self):
        user = FakeUser('guest')
        chat = fake_chat()
        shared = SimpleNamespace(chat={'share_mode': 'continue'})
        with (
            patch.object(Chats, 'get_chat_by_id', new=AsyncMock(return_value=chat)),
            patch('open_webui.models.shared_chats.SharedChats.get_by_chat_id', new=AsyncMock(return_value=shared)),
            patch(
                'open_webui.models.access_grants.AccessGrants.has_access',
                new=AsyncMock(return_value=True),
            ) as has_access,
        ):
            result = await Chats.get_accessible_chat_by_id('chat-1', user, permission='write')
        self.assertIsNotNone(result)
        has_access.assert_awaited_once()
        self.assertEqual(has_access.await_args.kwargs['permission'], 'read')

    async def test_no_grant_is_not_writable(self):
        user = FakeUser('intruder')
        chat = fake_chat()
        shared = SimpleNamespace(chat={'share_mode': 'continue'})
        with (
            patch.object(Chats, 'get_chat_by_id', new=AsyncMock(return_value=chat)),
            patch('open_webui.models.shared_chats.SharedChats.get_by_chat_id', new=AsyncMock(return_value=shared)),
            patch('open_webui.models.access_grants.AccessGrants.has_access', new=AsyncMock(return_value=False)),
        ):
            result = await Chats.get_accessible_chat_by_id('chat-1', user, permission='write')
        self.assertIsNone(result)

    async def test_read_only_share_is_not_writable(self):
        user = FakeUser('reader')
        chat = fake_chat()
        shared = SimpleNamespace(chat={'share_mode': None})
        with (
            patch.object(Chats, 'get_chat_by_id', new=AsyncMock(return_value=chat)),
            patch('open_webui.models.shared_chats.SharedChats.get_by_chat_id', new=AsyncMock(return_value=shared)),
            patch('open_webui.models.access_grants.AccessGrants.has_access', new=AsyncMock(return_value=True)),
        ):
            result = await Chats.get_accessible_chat_by_id('chat-1', user, permission='write')
        self.assertIsNone(result)


class ChatSearchSQLTests(unittest.TestCase):
    def test_postgres_uses_jsonb_function_for_history_messages(self):
        sql = chat_search_message_content_match_sql('postgresql', 'term')
        self.assertNotIn('json_each(Chat.chat#>', sql)
        self.assertIn('jsonb_each(Chat.chat#>', sql)
        self.assertIn("jsonb_typeof(history_message.value->'content')", sql)

    def test_postgres_statement_compiles_with_literal_params(self):
        stmt = select(Chat.id).where(text(chat_search_message_content_match_sql('postgresql', 'term')))
        compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
        self.assertIn('jsonb_each(chat.chat#>', compiled.lower())
        self.assertNotIn('json_each(chat.chat#>', compiled.lower())

    def test_sqlite_branch_is_unchanged(self):
        sql = chat_search_message_content_match_sql('sqlite', 'term')
        self.assertIn("json_each(Chat.chat, '$.history.messages')", sql)
        stmt = select(Chat.id).where(text(sql))
        compiled = str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
        self.assertIn('json_each(chat.chat', compiled.lower())


class GetChatsPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_skip_and_limit_reach_compiled_statement(self):
        captured = {}

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        class FakeSession:
            async def execute(self, stmt):
                captured['stmt'] = stmt
                return FakeResult()

        class FakeContext:
            async def __aenter__(self):
                return FakeSession()

            async def __aexit__(self, *args):
                return False

        with patch('open_webui.models.chats.get_async_db_context', return_value=FakeContext()):
            result = await Chats.get_chats(skip=10, limit=25)

        self.assertEqual(result, [])
        compiled = str(captured['stmt'].compile(dialect=sqlite.dialect(), compile_kwargs={'literal_binds': True}))
        self.assertIn('OFFSET 10', compiled)
        self.assertIn('LIMIT 25', compiled)


if __name__ == '__main__':
    unittest.main()
