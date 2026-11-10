"""Agent builtin tool injection matrix.

Encodes docs/AGENT_TOOL_CONTRACT.md §10: discovery tools survive a knowledge
attachment, write tools stay out of note-attached chats, and the per-model
categories keep working.
"""

import unittest

from fastapi import Request
from open_webui.internal.db import get_async_db_context
from open_webui.models.users import User, Users
from open_webui.utils.tools import get_builtin_tools
from sqlalchemy import delete

INJECT_USER_ID = 'p007-inject-user-0001'
INJECT_EMAIL = 'p007-inject@local.test'

DISCOVERY = ('list_knowledge_bases', 'search_knowledge_bases', 'query_knowledge_bases')
WRITE = (
    'create_knowledge_base',
    'update_knowledge_base',
    'add_text_to_knowledge',
    'create_knowledge_directory',
    'remove_file_from_knowledge',
    'delete_knowledge_base',
)
DELETE = ('delete_note', 'preview_delete')


def plain_request() -> Request:
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


def make_model(knowledge=None, notes=True, knowledge_read=True, knowledge_manage=True):
    meta = {
        'capabilities': {'builtin_tools': True},
        'builtinTools': {
            'notes': notes,
            'knowledge': knowledge_read,
            'knowledge_manage': knowledge_manage,
        },
    }
    if knowledge is not None:
        meta['knowledge'] = knowledge
    return {'id': 'test-model', 'info': {'meta': meta}}


class ToolInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with get_async_db_context() as db:
            await db.execute(delete(User).where(User.id == INJECT_USER_ID))
            await db.commit()

        await Users.insert_new_user(
            id=INJECT_USER_ID, name='Inject', email=INJECT_EMAIL, role='admin'
        )
        user = await Users.get_user_by_email(INJECT_EMAIL)
        self.user = user.model_dump()
        self.request = plain_request()

    async def asyncTearDown(self):
        async with get_async_db_context() as db:
            await db.execute(delete(User).where(User.id == INJECT_USER_ID))
            await db.commit()

    async def names(self, model, is_note_chat=False):
        tools = await get_builtin_tools(
            self.request,
            {'__user__': self.user},
            {'file_upload': True},
            model,
            is_note_chat=is_note_chat,
        )
        return set(tools)

    # --- K11: discovery survives an attachment ---------------------------

    async def test_knowledge_base_discovery_survives_attachment(self):
        """An attached collection must not hide the other knowledge bases."""
        for attachment in (
            None,
            [{'type': 'collection', 'id': 'kb-1'}],
            [{'type': 'file', 'id': 'file-1'}],
            [{'type': 'note', 'id': 'note-1'}],
        ):
            with self.subTest(attachment=attachment):
                names = await self.names(make_model(attachment))
                self.assertIn('list_knowledge_bases', names)
                self.assertIn('search_knowledge_bases', names)

    async def test_attached_collection_still_exposes_its_own_tools(self):
        names = await self.names(make_model([{'type': 'collection', 'id': 'kb-1'}]))
        for tool in ('list_knowledge', 'search_knowledge_files', 'grep_knowledge_files', 'view_knowledge_file'):
            self.assertIn(tool, names)

    async def test_attached_file_gets_view_file(self):
        names = await self.names(make_model([{'type': 'file', 'id': 'file-1'}]))
        self.assertIn('view_file', names)

    async def test_attached_note_gets_view_note(self):
        names = await self.names(make_model([{'type': 'note', 'id': 'note-1'}]))
        self.assertIn('view_note', names)

    async def test_no_attachment_gets_query_knowledge_bases(self):
        names = await self.names(make_model())
        self.assertIn('query_knowledge_bases', names)

    # --- K12 / §10.3: note-attached chat --------------------------------

    async def test_note_chat_has_notes_tools_but_no_writes(self):
        names = await self.names(make_model(), is_note_chat=True)

        for tool in ('search_notes', 'view_note', 'write_note', 'replace_note_content'):
            self.assertIn(tool, names)

        for tool in WRITE:
            self.assertNotIn(tool, names, f'{tool} must not be offered inside a note chat')
        self.assertNotIn('delete_note', names)

    async def test_note_chat_can_read_knowledge(self):
        """§10.3: a note chat may read the knowledge base, just not write it."""
        names = await self.names(make_model(), is_note_chat=True)
        self.assertIn('list_knowledge_bases', names)
        self.assertIn('search_knowledge_bases', names)

    # --- K13: per-model categories ---------------------------------------

    async def test_notes_off_removes_delete_note(self):
        names = await self.names(make_model(notes=False))
        self.assertNotIn('delete_note', names)
        # `preview_delete` also serves knowledge deletion, so it stays while
        # knowledge_manage is on (§10.2). It must disappear only when both
        # deletion paths are off.
        self.assertIn('preview_delete', names)

    async def test_preview_delete_gone_when_both_paths_off(self):
        names = await self.names(make_model(notes=False, knowledge_manage=False))
        self.assertNotIn('preview_delete', names)
        self.assertNotIn('delete_note', names)

    async def test_knowledge_manage_off_removes_write_tools(self):
        names = await self.names(make_model(knowledge_manage=False))
        for tool in WRITE:
            self.assertNotIn(tool, names)
        # Read tools stay available.
        self.assertIn('list_knowledge_bases', names)

    async def test_knowledge_read_off_removes_read_and_write(self):
        """Writes presuppose reads; a read-off category must not leave a write door."""
        names = await self.names(make_model(knowledge_read=False))
        for tool in DISCOVERY:
            self.assertNotIn(tool, names)
        for tool in WRITE:
            self.assertNotIn(tool, names)

    async def test_write_tools_present_by_default(self):
        names = await self.names(make_model())
        for tool in WRITE:
            self.assertIn(tool, names)

    async def test_preview_delete_is_never_duplicated(self):
        """It serves both the notes and the knowledge deletion paths."""
        tools = await get_builtin_tools(
            self.request,
            {'__user__': self.user},
            {'file_upload': True},
            make_model(),
        )
        self.assertIn('preview_delete', tools)
        self.assertEqual(
            len([name for name in tools if name == 'preview_delete']),
            1,
        )


if __name__ == '__main__':
    unittest.main()
