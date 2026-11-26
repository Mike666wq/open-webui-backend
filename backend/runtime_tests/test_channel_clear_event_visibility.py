"""Channel-clear events must not leak old bodies or drop new ones.

Regression coverage for three failures:
* reaction/reply/edit payloads carry a parent's timestamp (often seconds or
  missing) instead of the event message's nanosecond timestamp, so every such
  event was silently dropped after a clear;
* the body pushed to a viewer could be re-fetched from a different message
  than the one whose timestamp was checked;
* channel members whose account role had been demoted to ``pending`` still
  received message bodies.
"""

import time
import unittest
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

from open_webui.internal.db import get_async_db
from open_webui.models.channels import Channel, ChannelMember
from open_webui.models.messages import Message, MessageReaction
from open_webui.models.resources import ResourceEntry
from open_webui.models.users import User
from open_webui.utils.resource_events import (
    emit_channel_event,
    message_allowed,
    normalize_created_at_ns,
)
from open_webui.utils.resource_service import resource_changed


class _FakeSio:
    def __init__(self):
        self.emitted = []

    async def emit(self, event, data, **kwargs):
        self.emitted.append((event, data, kwargs))

    async def close_room(self, room):
        pass


class NormalizeCreatedAtTests(unittest.TestCase):
    def test_seconds_milliseconds_microseconds_and_nanoseconds(self):
        seconds = 1_700_000_000
        self.assertEqual(normalize_created_at_ns(seconds), seconds * 1_000_000_000)
        self.assertEqual(normalize_created_at_ns(seconds * 1000), seconds * 1_000_000_000)
        self.assertEqual(normalize_created_at_ns(seconds * 1_000_000), seconds * 1_000_000_000)
        self.assertEqual(normalize_created_at_ns(seconds * 1_000_000_000), seconds * 1_000_000_000)
        self.assertEqual(normalize_created_at_ns(str(seconds)), seconds * 1_000_000_000)
        self.assertEqual(normalize_created_at_ns(f'{seconds}.5'), seconds * 1_000_000_000 + 500_000_000)

    def test_missing_invalid_and_non_numeric_values(self):
        for value in (None, 0, -1, '', 'not-a-time', True, {}, []):
            self.assertEqual(normalize_created_at_ns(value), 0, value)


class ChannelClearEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.suffix = uuid.uuid4().hex[:10]
        self.channel_id = f'channel-{self.suffix}'
        self.old_ns = time.time_ns() - 10_000_000_000
        self.new_ns = time.time_ns() + 10_000_000_000
        self.cutoff_ns = time.time_ns()
        self.user_ids = {
            'alice': f'alice-{self.suffix}',
            'bob': f'bob-{self.suffix}',
            'pending': f'pending-{self.suffix}',
        }
        async with get_async_db() as db:
            for key, role in (('alice', 'user'), ('bob', 'user'), ('pending', 'pending')):  # noqa: E501
                db.add(User(
                    id=self.user_ids[key], name=key, username=f'{key}-{self.suffix}',
                    email=f'{key}-{self.suffix}@example.invalid',
                    role=role, last_active_at=int(time.time()),
                    created_at=int(time.time()), updated_at=int(time.time()),
                ))
            db.add(Channel(
                id=self.channel_id, user_id=self.user_ids['alice'], name='clear-events',
                type='group', created_at=self.old_ns, updated_at=self.old_ns,
            ))
            for key in self.user_ids:
                db.add(ChannelMember(
                    id=f'member-{key}-{self.suffix}', channel_id=self.channel_id,
                    user_id=self.user_ids[key], joined_at=self.old_ns,
                    created_at=self.old_ns, updated_at=self.old_ns,
                ))
            db.add(Message(
                id=f'old-{self.suffix}', channel_id=self.channel_id,
                user_id=self.user_ids['alice'], content='body before clear',
                created_at=self.old_ns, updated_at=self.old_ns,
            ))
            db.add(Message(
                id=f'new-{self.suffix}', channel_id=self.channel_id,
                user_id=self.user_ids['alice'], content='body after clear',
                created_at=self.new_ns, updated_at=self.new_ns,
            ))
            db.add(Message(
                id=f'parent-{self.suffix}', channel_id=self.channel_id,
                user_id=self.user_ids['alice'], content='parent body',
                created_at=self.old_ns, updated_at=self.old_ns,
            ))
            db.add(MessageReaction(
                id=f'reaction-{self.suffix}', user_id=self.user_ids['alice'],
                message_id=f'old-{self.suffix}', name='thumbsup', created_at=self.cutoff_ns,
            ))
            await db.commit()

    async def asyncTearDown(self):
        from sqlalchemy import select
        from open_webui.utils.resource_visibility import including_recycled
        with including_recycled():
            async with get_async_db() as db:
                for model, column in (
                    (MessageReaction, 'message_id'), (Message, 'channel_id'),
                    (ChannelMember, 'channel_id'), (ResourceEntry, 'resource_id'),
                    (Channel, 'id'),
                ):
                    rows = (await db.execute(select(model).where(
                        getattr(model, column).like(f'%{self.suffix}%')
                    ))).scalars().all()
                    for row in rows:
                        await db.delete(row)
                for user_id in self.user_ids.values():
                    user = await db.get(User, user_id)
                    if user:
                        await db.delete(user)
                await db.commit()

    async def _set_clear(self, user_id, state='recycled'):
        from open_webui.utils.resource_visibility import including_recycled
        with including_recycled():
            async with get_async_db() as db:
                db.add(ResourceEntry(
                    id=f'clear-{user_id}-{self.suffix}', type='channel_clear',
                    resource_id=self.channel_id, user_id=user_id, state=state,
                    title='clear', cutoff_at=self.cutoff_ns, deleted_at=int(time.time()),
                    expires_at=int(time.time()) + 3600, generation=1,
                ))
                await db.commit()

    def _event(self, message_id, event_type='message', payload=None):
        body = {'id': message_id, 'created_at': str(self.cutoff_ns), 'content': 'payload body'}
        if payload is not None:
            body = payload
        return {
            'channel_id': self.channel_id,
            'message_id': message_id,
            'data': {'type': event_type, 'data': body},
        }

    async def _emit(self, data):
        fake = _FakeSio()
        from open_webui.socket import main as socket_main
        from open_webui.utils import resource_events
        with patch.object(socket_main, 'sio', fake), patch.object(resource_events, 'sio', fake, create=True):
            await emit_channel_event('events:channel', data, to=f'channel:{self.channel_id}')
        return fake.emitted

    async def test_seconds_created_at_is_normalized_not_dropped(self):
        await self._set_clear(self.user_ids['alice'])
        seconds = int(self.new_ns // 1_000_000_000)
        self.assertTrue(await message_allowed(self.user_ids['alice'], self.channel_id, seconds))
        self.assertFalse(await message_allowed(self.user_ids['alice'], self.channel_id, seconds - 20))

    async def test_missing_created_at_resolves_event_message_not_parent(self):
        await self._set_clear(self.user_ids['alice'])
        old_id, new_id = f'old-{self.suffix}', f'new-{self.suffix}'
        # Payload has no created_at; the visible message id is the event's own.
        new_emitted = await self._emit(self._event(new_id, payload={'id': new_id, 'content': 'hi'}))
        self.assertEqual(len(new_emitted), 2)
        for _, payload, kwargs in new_emitted:
            self.assertEqual(payload['data']['data']['id'], new_id)
            self.assertEqual(payload['data']['data']['content'], 'body after clear')
            self.assertTrue(kwargs['to'].startswith('user:'))
        old_emitted = await self._emit(self._event(old_id, payload={'id': old_id, 'content': 'old'}))
        self.assertEqual([kw['to'] for _, _, kw in old_emitted], [f'user:{self.user_ids["bob"]}'])

    async def test_new_message_after_clear_reaches_members(self):
        await self._set_clear(self.user_ids['bob'])
        old_id, new_id = f'old-{self.suffix}', f'new-{self.suffix}'
        data = self._event(new_id)
        data['data']['data']['created_at'] = 0  # caller-supplied timestamp is wrong
        emitted = await self._emit(data)
        self.assertEqual(len(emitted), 2)
        for _, payload, _kwargs in emitted:
            self.assertEqual(payload['data']['data']['id'], new_id)
            self.assertEqual(payload['data']['data']['created_at'], self.new_ns)
            self.assertEqual(payload['data']['data']['content'], 'body after clear')

    async def test_old_message_before_clear_is_blocked(self):
        await self._set_clear(self.user_ids['bob'])
        emitted = await self._emit(self._event(f'old-{self.suffix}'))
        self.assertEqual([kw['to'] for _, _, kw in emitted], [f'user:{self.user_ids["alice"]}'])
        for _, payload, _kw in emitted:
            self.assertEqual(payload['data']['data']['content'], 'body before clear')

    async def test_reaction_and_reply_events_use_event_message_timestamp(self):
        await self._set_clear(self.user_ids['alice'])
        parent_id = f'parent-{self.suffix}'
        # Reply event: message_id is the new reply, payload is the old parent.
        data = self._event(f'new-{self.suffix}', 'message:reply', {
            'id': parent_id, 'created_at': self.old_ns, 'content': 'parent body',
        })
        emitted = await self._emit(data)
        self.assertEqual(len(emitted), 2)
        for _, payload, _kwargs in emitted:
            self.assertEqual(payload['data']['data']['id'], f'new-{self.suffix}')
            self.assertEqual(payload['data']['data']['content'], 'body after clear')
        # Reaction event on the old parent must still be blocked for a viewer
        # who cleared before it, even though the reaction row itself is newer.
        await self._set_clear(self.user_ids['bob'])
        reaction = self._event(parent_id, 'message:reaction:add', {
            'id': parent_id, 'created_at': str(self.cutoff_ns), 'name': 'thumbsup',
        })
        self.assertEqual([kw['to'] for _, _, kw in await self._emit(reaction)], [])

    async def test_pending_role_receives_no_body(self):
        await self._set_clear(self.user_ids['alice'])
        emitted = await self._emit(self._event(f'new-{self.suffix}'))
        recipients = {kwargs['to'] for _, _, kwargs in emitted}
        self.assertNotIn(f'user:{self.user_ids["pending"]}', recipients)
        self.assertEqual(len(emitted), 2)

        fake = _FakeSio()
        from open_webui.socket import main as socket_main
        with patch.object(socket_main, 'sio', fake):
            await resource_changed(self.user_ids['pending'], 'chat', 'some-chat', 'recycle')
        self.assertEqual(fake.emitted, [])
        with patch.object(socket_main, 'sio', fake):
            await resource_changed(self.user_ids['alice'], 'chat', 'some-chat', 'recycle')
        self.assertEqual(len(fake.emitted), 1)


if __name__ == '__main__':
    unittest.main()
