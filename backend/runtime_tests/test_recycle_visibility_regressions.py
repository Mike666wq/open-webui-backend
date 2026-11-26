"""Regression coverage for recycled chat/share visibility and restore.

These tests use the SQLAlchemy expressions directly so they can run without a
FastAPI app, an HTTP client or a real database.
"""

import ast
import unittest
from pathlib import Path

from sqlalchemy import select

from open_webui.models.shared_chats import SharedChat
from open_webui.utils.resource_visibility import visible


class UnshareAllPayloadTests(unittest.TestCase):
    def test_payload_uses_collected_share_ids(self):
        source = Path(__file__).parents[1] / 'open_webui' / 'routers' / 'chats.py'
        module = ast.parse(source.read_text())
        function = next(
            node for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'unshare_all_chats'
        )
        calls = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'publish_event'
        ]
        self.assertEqual(len(calls), 1)
        data = next(
            keyword.value for keyword in calls[0].keywords
            if keyword.arg == 'data'
        )
        self.assertIsInstance(data, ast.Dict)
        self.assertEqual(
            [key.value for key in data.keys if isinstance(key, ast.Constant)],
            ['count', 'share_ids', 'chat_ids'],
        )
        count, share_ids, chat_ids = data.values
        self.assertIsInstance(count, ast.Call)
        self.assertEqual(getattr(count.func, 'id', None), 'len')
        self.assertEqual(getattr(count.args[0], 'id', None), 'ids')
        self.assertEqual(getattr(share_ids, 'id', None), 'ids')
        self.assertEqual(getattr(chat_ids, 'id', None), 'ids')


class ShareVisibilityTests(unittest.TestCase):
    def test_cross_user_blocked_entries_do_not_hide_another_users_share(self):
        statement = select(SharedChat).where(
            visible('chat', SharedChat.chat_id, SharedChat.user_id)
        )
        compiled = str(statement.compile(compile_kwargs={'literal_binds': True}))
        self.assertIn('resource_entry.user_id = shared_chat.user_id', compiled)
        self.assertIn('resource_entry.type = \'chat\'', compiled)

    def test_paused_share_stays_hidden_by_blocked_filter(self):
        statement = select(SharedChat).where(visible('share', SharedChat.id))
        compiled = str(statement.compile(compile_kwargs={'literal_binds': True}))
        self.assertIn("resource_entry.state IN", compiled)
        self.assertIn("'paused'", compiled)

    def test_generic_resource_visibility_is_unchanged(self):
        from open_webui.models.knowledge import Knowledge

        statement = select(Knowledge).where(visible('knowledge', Knowledge.id))
        compiled = str(statement.compile(compile_kwargs={'literal_binds': True}))
        self.assertIn('resource_entry.type = \'knowledge\'', compiled)
        self.assertNotIn('AND knowledge.user_id', compiled)


if __name__ == '__main__':
    unittest.main()
