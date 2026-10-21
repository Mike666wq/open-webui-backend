"""Recipient-aware channel events use the same boundary as HTTP reads."""

from copy import deepcopy

from sqlalchemy import select

from open_webui.internal.db import get_async_db
from open_webui.utils.resource_visibility import as_viewer


async def message_allowed(user_id, channel_id, created_at, parent_id=None):
    from open_webui.models.messages import Message
    from open_webui.models.resources import ResourceEntry
    async with get_async_db() as db:
        boundary = await db.scalar(select(ResourceEntry.cutoff_at).where(
            ResourceEntry.type == 'channel_clear', ResourceEntry.resource_id == channel_id,
            ResourceEntry.user_id == user_id, ResourceEntry.state.in_(('recycled', 'cleared')),
        ))
        if boundary and created_at <= boundary:
            return False
        if parent_id:
            with as_viewer(user_id):
                if not await db.scalar(select(Message.id).where(Message.id == parent_id)):
                    return False
        return True


async def emit_channel_event(event, data, to=None, room=None, **kwargs):
    from open_webui.socket.main import sio
    from open_webui.models.channels import Channels
    from open_webui.models.messages import Messages
    channel_id = data.get('channel_id')
    target = to or room
    event_type = data.get('data', {}).get('type', '')
    if not channel_id or not event_type.startswith('message'):
        return await sio.emit(event, data, to=to, room=room, **kwargs)
    # User rooms work across Socket.IO workers as well as across devices.
    # Membership is the durable audience, not this process's socket pool.
    members = await Channels.get_members_by_channel_id(channel_id)
    for member in members:
        payload = deepcopy(data)
        message_data = data.get('data', {}).get('data') or {}
        if not await message_allowed(member.user_id, channel_id,
                                     message_data.get('created_at', 0), message_data.get('parent_id')):
            continue
        with as_viewer(member.user_id):
            if event_type == 'message:delete':
                # A deleted message has no row to re-fetch. Its body is never
                # required for a removal event, and may contain an old reply.
                payload['data']['data'] = {'id': data['message_id']}
            else:
                visible = await Messages.get_message_by_id(data['message_id'])
                if not visible:
                    continue
                if 'content' in message_data or 'reply_to_message' in message_data:
                    payload['data']['data'] = {
                        **visible.model_dump(),
                        **{key: message_data[key] for key in ('temp_id', 'name') if key in message_data},
                    }
            destination = target if target and not target.startswith('channel:') else f'user:{member.user_id}'
            await sio.emit(event, payload, to=destination, **kwargs)
