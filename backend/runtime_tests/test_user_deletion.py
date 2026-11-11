"""Deleting an account must delete it, not answer 500.

An account owns chats, notes, knowledge and files. The visibility guard
rejects direct DELETE statements on those tables — they are supposed to go
through the recycle bin — and `delete_chats_by_user_id` swallowed the guard's
409 as a plain False. The router then answered a generic 500 while the
account stayed in the database, which is how production accumulated pending
probe accounts that no one could remove.

Deleting an account is not a recycle-bin operation: the content goes with the
account, so the whole cascade runs inside the maintenance context.
"""

import unittest
import uuid

from open_webui.main import app


class UserDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from open_webui.models.config import Config

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://p010-user-del', timeout=120
        )
        self.addAsyncCleanup(self.client.aclose)
        suffix = uuid.uuid4().hex[:10]
        self.admin_email = f'del-admin-{suffix}@example.invalid'
        await Config.upsert({'ui.enable_signup': True})
        admin = await self._call(
            'POST',
            '/api/v1/auths/signup',
            {'name': 'del-admin', 'email': self.admin_email, 'password': 'Admin-pass-123'},
        )
        self.admin_token = admin['token']
        self.admin_id = admin['id']
        if admin.get('role') != 'admin':
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.users import Users

            async with get_async_db_context() as db:
                await Users.update_user_role_by_id(self.admin_id, 'admin', db=db)
            admin = await self._call(
                'POST',
                '/api/v1/auths/signin',
                {'email': self.admin_email, 'password': 'Admin-pass-123'},
            )
            self.admin_token = admin['token']
        self.assertEqual(admin.get('role'), 'admin')
        self.created = []

    async def _call(self, method, path, body=None, token=None):
        headers = {}
        credential = token or getattr(self, 'admin_token', None)
        if credential:
            headers['Authorization'] = f'Bearer {credential}'
        response = await self.client.request(method, path, json=body, headers=headers)
        self._status = response.status_code
        try:
            return response.json()
        except Exception:
            return {'raw': response.text}

    async def _create_pending(self):
        suffix = uuid.uuid4().hex[:8]
        user = await self._call(
            'POST',
            '/api/v1/auths/add',
            {
                'name': f'del-user-{suffix}',
                'email': f'del-user-{suffix}@example.invalid',
                'password': 'User-pass-123',
                'role': 'pending',
            },
        )
        self.assertIn('id', user, user)
        self.created.append(user['id'])
        if len(self.created) == 1:
            self.addAsyncCleanup(self._sweep)
        return user

    async def _sweep(self):
        # A leftover probe account is exactly the production mess this module
        # exists to prevent, so the tests clean up even when they fail.
        for user_id in self.created:
            try:
                await self._call('DELETE', f'/api/v1/users/{user_id}')
            except Exception:
                pass

    async def test_pending_user_can_be_deleted(self):
        """The simplest account: no chats, no content, just the row."""
        user = await self._create_pending()
        result = await self._call('DELETE', f"/api/v1/users/{user['id']}")
        self.assertTrue(result is True, result)
        remaining = await self._call('GET', '/api/v1/users/?page=1')
        self.assertFalse(any(u['id'] == user['id'] for u in remaining.get('users', [])))

    async def test_user_with_chats_can_be_deleted(self):
        """A chat used to block the deletion via the visibility guard."""
        from open_webui.models.chats import ChatForm, Chats
        from open_webui.models.users import Users

        user = await self._create_pending()
        chat = await Chats.insert_new_chat(
            f'chat-{uuid.uuid4().hex[:10]}',
            user['id'],
            ChatForm(**{'title': 'delete me', 'chat': {'messages': [{'role': 'user', 'content': 'hello'}]}}),
        )
        self.assertIsNotNone(chat)
        result = await self._call('DELETE', f"/api/v1/users/{user['id']}")
        self.assertTrue(result is True, result)
        remaining = await self._call('GET', '/api/v1/users/?page=1')
        self.assertFalse(any(u['id'] == user['id'] for u in remaining.get('users', [])))

    async def test_repeated_deletion_and_primary_admin_protection(self):
        user = await self._create_pending()
        first = await self._call('DELETE', f"/api/v1/users/{user['id']}")
        self.assertTrue(first is True, first)
        # Repeating the delete must stay a 2xx, never resurface as the old
        # generic 500 the administrator could not act on.
        again = await self._call('DELETE', f"/api/v1/users/{user['id']}")
        self.assertTrue(again is True, again)
        remaining = await self._call('GET', '/api/v1/users/?page=1')
        self.assertFalse(any(u['id'] == user['id'] for u in remaining.get('users', [])))
        # The primary admin stays protected: deleting the first account must
        # be refused with 403.
        guard = await self._call('DELETE', f"/api/v1/users/{self.admin_id}")
        self.assertTrue(guard is not True, guard)
        self.assertEqual(self._status, 403)

    async def test_missing_account_is_not_an_error(self):
        """A missing id deletes nothing and answers 2xx: the desired end
        state is already true, so the administrator does not see a failure
        they cannot act on."""
        result = await self._call('DELETE', '/api/v1/users/00000000-0000-0000-0000-000000000000')
        self.assertTrue(result is True, result)


if __name__ == '__main__':
    unittest.main()
