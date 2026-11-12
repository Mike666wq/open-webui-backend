"""A chat may be recycled unless it is genuinely generating right now.

The protection answered `active_generation` for any row with `done = False`,
for any set `timer_at`, forever. A stream that was interrupted — the tab
closed, the container restarted, the provider died — leaves such a row behind
and never clears it, so the chat became permanently undeletable and the
preview kept insisting a reply was in flight days later.

A live reply refreshes its row on every chunk, so freshness is what separates
"generating" from "leftover". These tests pin both sides of that line.
"""

import time
import unittest
import uuid

from open_webui.main import app
from open_webui.utils.resource_service import STALE_GENERATION_SECONDS


class ChatRecycleProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from open_webui.models.config import Config

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://p012-recycle', timeout=120
        )
        self.addAsyncCleanup(self.client.aclose)
        suffix = uuid.uuid4().hex[:10]
        await Config.upsert({'ui.enable_signup': True})
        admin = await self._call(
            'POST',
            '/api/v1/auths/signup',
            {'name': 'rec', 'email': f'rec-{suffix}@example.invalid', 'password': 'Admin-pass-123'},
        )
        self.token = admin['token']
        self.user_id = admin['id']
        if admin.get('role') != 'admin':
            from open_webui.internal.db import get_async_db_context
            from open_webui.models.users import Users

            async with get_async_db_context() as db:
                await Users.update_user_role_by_id(self.user_id, 'admin', db=db)
            signed = await self._call(
                'POST', '/api/v1/auths/signin',
                {'email': f'rec-{suffix}@example.invalid', 'password': 'Admin-pass-123'},
            )
            self.token = signed['token']
        self.chats = []

    async def _call(self, method, path, body=None):
        headers = {'Authorization': f'Bearer {self.token}'} if getattr(self, 'token', None) else {}
        response = await self.client.request(method, path, json=body, headers=headers)
        try:
            return response.json()
        except Exception:
            return {'raw': response.text}

    async def _chat(self, title='probe'):
        from open_webui.models.chats import ChatForm, Chats

        chat_id = str(uuid.uuid4())
        await Chats.insert_new_chat(
            chat_id,
            self.user_id,
            ChatForm(**{'chat': {'messages': {}}, 'title': title}),
        )
        self.chats.append(chat_id)
        if len(self.chats) == 1:
            self.addAsyncCleanup(self._sweep)
        return chat_id

    async def _sweep(self):
        from open_webui.models.chats import Chats

        for chat_id in self.chats:
            try:
                await Chats.delete_chat_by_id(chat_id)
            except Exception:
                pass

    async def _message(self, chat_id, *, role, done, age_seconds=0):
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chat_messages import ChatMessage

        message_id = f'{chat_id}-{uuid.uuid4().hex[:8]}'
        stamp = int(time.time()) - age_seconds
        async with get_async_db_context() as db:
            db.add(
                ChatMessage(
                    id=message_id,
                    chat_id=chat_id,
                    user_id=self.user_id,
                    role=role,
                    content={'content': 'x'},
                    done=done,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            await db.commit()

    async def _preflight(self, chat_ids):
        return await self._call(
            'POST',
            '/api/v1/resources/preflight',
            {'action': 'recycle', 'items': [{'type': 'chat', 'id': cid} for cid in chat_ids]},
        )

    async def test_stale_unfinished_message_does_not_block_deletion(self):
        """The bug: a dead stream left `done = False` and locked the chat."""
        chat_id = await self._chat('stale')
        await self._message(
            chat_id, role='assistant', done=False, age_seconds=STALE_GENERATION_SECONDS + 60
        )
        result = await self._preflight([chat_id])
        item = result['items'][0]
        self.assertTrue(item['allowed'], item)
        self.assertNotIn('active_generation', item['reasons'])

    async def test_fresh_unfinished_message_still_blocks_deletion(self):
        """Deleting mid-stream is still refused: that would lose live work."""
        chat_id = await self._chat('live')
        await self._message(chat_id, role='assistant', done=False, age_seconds=5)
        item = (await self._preflight([chat_id]))['items'][0]
        self.assertFalse(item['allowed'], item)
        self.assertIn('active_generation', item['reasons'])

    async def test_finished_message_never_blocks(self):
        chat_id = await self._chat('done')
        await self._message(chat_id, role='assistant', done=True)
        item = (await self._preflight([chat_id]))['items'][0]
        self.assertTrue(item['allowed'], item)

    async def test_non_assistant_rows_are_not_generation(self):
        """Only assistant rows stand for a stream; others must not block."""
        chat_id = await self._chat('user-role')
        await self._message(chat_id, role='user', done=False, age_seconds=1)
        item = (await self._preflight([chat_id]))['items'][0]
        self.assertTrue(item['allowed'], item)

    async def test_far_past_timer_does_not_block(self):
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat

        chat_id = await self._chat('timer-stale')
        async with get_async_db_context() as db:
            row = await db.get(Chat, chat_id)
            row.timer_at = time.time_ns() - (STALE_GENERATION_SECONDS + 60) * 1_000_000_000
            await db.commit()
        item = (await self._preflight([chat_id]))['items'][0]
        self.assertTrue(item['allowed'], item)

    async def test_future_timer_still_blocks(self):
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat

        chat_id = await self._chat('timer-live')
        async with get_async_db_context() as db:
            row = await db.get(Chat, chat_id)
            row.timer_at = time.time_ns() + 3600 * 1_000_000_000
            await db.commit()
        item = (await self._preflight([chat_id]))['items'][0]
        self.assertFalse(item['allowed'], item)
        self.assertIn('active_generation', item['reasons'])


if __name__ == '__main__':
    unittest.main()
