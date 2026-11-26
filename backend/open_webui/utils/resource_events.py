"""Recipient-aware channel events use the same boundary as HTTP reads."""

from copy import deepcopy

from sqlalchemy import select

from open_webui.internal.db import get_async_db, get_async_db_context
from open_webui.utils.resource_timestamps import normalize_created_at_ns
from open_webui.utils.resource_visibility import as_viewer, including_recycled


async def _clear_boundary_ns(db, user_id, channel_id):
    from open_webui.models.resources import ResourceEntry
    boundary = await db.scalar(select(ResourceEntry.cutoff_at).where(
        ResourceEntry.type == 'channel_clear', ResourceEntry.resource_id == channel_id,
        ResourceEntry.user_id == user_id, ResourceEntry.state.in_(('recycled', 'cleared')),
    ))
    return normalize_created_at_ns(boundary)


async def message_allowed(user_id, channel_id, created_at, parent_id=None):
    """Check the clear boundary against the message the event is about."""
    from open_webui.models.messages import Message
    created_at_ns = normalize_created_at_ns(created_at)
    async with get_async_db() as db:
        boundary = await _clear_boundary_ns(db, user_id, channel_id)
        if boundary and (not created_at_ns or created_at_ns <= boundary):
            return False
        if parent_id:
            with as_viewer(user_id):
                if not await db.scalar(select(Message.id).where(Message.id == parent_id)):
                    return False
        return True


def _event_message_id(data):
    """The message an event is about, never a parent/reaction's id."""
    for key in ('message_id', 'id'):
        if data.get(key):
            return data[key]
    return (data.get('data') or {}).get('data', {}).get('id')


async def resolve_event_created_at_ns(data):
    """Timestamp of the event's own message, resolved from the database."""
    message_id = _event_message_id(data)
    if not message_id:
        return 0
    from open_webui.models.messages import Message
    # The per-recipient clear boundary decides visibility; the global guard
    # must not hide the row before that check has run.
    with including_recycled(), as_viewer(None):
        async with get_async_db_context() as db:
            created_at = await db.scalar(select(Message.created_at).where(Message.id == message_id))
    return normalize_created_at_ns(created_at)


def _replacement_body(message_data, visible):
    body = visible.model_dump()
    for key in ('temp_id', 'name'):
        if key in message_data:
            body[key] = message_data[key]
    return body


async def emit_channel_event(event, data, to=None, room=None, **kwargs):
    from open_webui.socket.main import sio
    from open_webui.models.channels import Channels
    from open_webui.models.messages import Messages
    from open_webui.models.users import Users
    channel_id = data.get('channel_id')
    target = to or room
    event_type = (data.get('data') or {}).get('type', '')
    if not channel_id or not event_type.startswith('message'):
        return await sio.emit(event, data, to=to, room=room, **kwargs)

    # Validate and push the same message: resolve the body and its timestamp
    # from the event's message_id, never from a parent/reaction's model_dump.
    message_id = data.get('message_id') or _event_message_id(data)
    message_data = (data.get('data') or {}).get('data') or {}
    created_at_ns = await resolve_event_created_at_ns(data)
    members = await Channels.get_members_by_channel_id(channel_id)
    approved_members = []
    for member in members:
        if not member.user_id:
            continue
        user = await Users.get_user_by_id(member.user_id)
        if user and user.role in ('admin', 'user'):
            approved_members.append(member)

    replacement = None
    if message_id and event_type != 'message:delete':
        with as_viewer(None):
            visible = await Messages.get_message_by_id(message_id)
        if visible:
            replacement = _replacement_body(message_data, visible)

    for member in approved_members:
        if not await message_allowed(member.user_id, channel_id, created_at_ns):
            continue
        payload = deepcopy(data)
        with as_viewer(member.user_id):
            if event_type == 'message:delete':
                payload['data']['data'] = {'id': message_id}
            elif replacement is not None:
                payload['data']['data'] = deepcopy(replacement)
            destination = target if target and not target.startswith('channel:') else f'user:{member.user_id}'
            await sio.emit(event, payload, to=destination, **kwargs)


async def resource_changed_allowed(user_id):
    """Refresh events carry no body, but still require an approved account."""
    from open_webui.models.users import Users
    user = await Users.get_user_by_id(user_id)
    return bool(user and user.role in ('admin', 'user'))
