"""Resource operations, conservative references and a restartable purge queue."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import logging
import re
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError

from open_webui.internal.db import Base, get_async_db
from open_webui.models.resources import ResourceEntry, ResourceRule, ResourceTask
from open_webui.utils.resource_visibility import including_recycled

log = logging.getLogger(__name__)
RETENTION = 7 * 86400
KINDS = ('chat', 'file', 'note', 'knowledge', 'share', 'channel')
ASSOCIATIONS = ('knowledge_file', 'knowledge_directory')


def now():
    return int(time.time())


def uid():
    return str(uuid.uuid4())


def models():
    from open_webui.models.chats import Chat
    from open_webui.models.files import File
    from open_webui.models.notes import Note
    from open_webui.models.knowledge import Knowledge, KnowledgeFile, KnowledgeDirectory
    from open_webui.models.shared_chats import SharedChat
    from open_webui.models.channels import Channel
    return dict(zip(KINDS + ASSOCIATIONS, (Chat, File, Note, Knowledge, SharedChat, Channel, KnowledgeFile, KnowledgeDirectory)))


def columns(row):
    return {column.key: getattr(row, column.key) for column in inspect(type(row)).columns}


def stamp(row, entry=None):
    return hashlib.sha256(json.dumps(
        [columns(row), columns(entry) if entry else None], sort_keys=True, default=str
    ).encode()).hexdigest()


def size(row, kind):
    if kind != 'file':
        return 0
    value = (row.meta or {}).get('size', 0)
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def title(row, kind):
    if kind == 'knowledge_file':
        return f'Knowledge attachment · {row.file_id}'
    return str(getattr(row, {'file': 'filename', 'knowledge': 'name', 'channel': 'name', 'knowledge_directory': 'name'}.get(kind, 'title'), '') or 'Untitled')


async def entry_for(db, kind, resource_id, user_id=None):
    query = select(ResourceEntry).where(
        ResourceEntry.type == kind, ResourceEntry.resource_id == resource_id,
    )
    if user_id:
        query = query.where(ResourceEntry.user_id == user_id)
    return await db.scalar(query.with_for_update())


async def require_permission(user, kind):
    if user.role not in ('admin', 'user'):
        raise HTTPException(403, 'Account is not approved')
    if user.role == 'admin':
        return
    from open_webui.models.config import Config
    from open_webui.utils.access_control import has_permission
    permissions = await Config.get('user.permissions')
    permission = {'chat': 'chat.delete', 'note': 'features.notes', 'knowledge': 'workspace.knowledge'}.get(kind)
    if permission and not await has_permission(user.id, permission, permissions):
        raise HTTPException(403, f'Permission denied: {permission}')


async def owned_row(db, user, kind, resource_id, *, global_channel=False, knowledge_write=False):
    if kind not in KINDS + ASSOCIATIONS:
        raise HTTPException(400, 'Invalid resource type')
    await require_permission(user, 'knowledge' if kind in ASSOCIATIONS else kind)
    row = await db.scalar(select(models()[kind]).where(
        models()[kind].id == resource_id,
    ).with_for_update())
    if not row:
        raise HTTPException(404, 'Resource not found')
    if kind == 'channel':
        from open_webui.models.channels import ChannelMember
        if global_channel:
            if row.user_id != user.id and user.role != 'admin':
                raise HTTPException(403, 'Only the owner or administrator may delete a channel for everyone')
        elif row.user_id != user.id and not await db.scalar(select(ChannelMember.id).where(
            ChannelMember.channel_id == resource_id, ChannelMember.user_id == user.id,
            ChannelMember.left_at.is_(None),
        )):
            raise HTTPException(404, 'Conversation not found')
    elif kind in ASSOCIATIONS or (kind == 'knowledge' and knowledge_write):
        parent = await db.get(models()['knowledge'], row.knowledge_id) if kind in ASSOCIATIONS else row
        if not parent:
            raise HTTPException(404, 'Knowledge not found')
        if parent.user_id != user.id:
            from open_webui.models.access_grants import AccessGrants
            allowed = knowledge_write and (user.role == 'admin' or await AccessGrants.has_access(
                user_id=user.id, resource_type='knowledge', resource_id=parent.id,
                permission='write', db=db,
            ))
            if not allowed:
                raise HTTPException(404, 'Knowledge not accessible')
    elif row.user_id != user.id:
        # The management page deliberately does not add an administrator
        # backdoor for inspecting another person's private content.
        raise HTTPException(404, 'Resource not found')
    if kind == 'file':
        from open_webui.utils.knowledge_import_lifecycle import has_managed_marker, validate_managed_file
        if has_managed_marker(row):
            from open_webui.utils.resource_visibility import maintenance
            trusted = maintenance.get()
            managed = validate_managed_file(row)
            if not trusted or not managed or managed['owner_id'] != row.user_id:
                raise HTTPException(404, 'Resource not found')
    return row


async def references(db, file_id, exclude=()):
    """Count references across persisted history, not just the mounted branch.

    Exact IDs and URL-contained IDs are both conservative matches. A reference
    in old JSON is retained even when its semantics cannot be reconstructed.
    """
    from open_webui.models.chats import Chat, ChatFile
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.channels import ChannelFile
    from open_webui.models.knowledge import KnowledgeFile
    excluded = set(tuple(x) for x in exclude)
    refs = set()
    for cls, field, kind in ((ChatFile, 'chat_id', 'chat'), (ChannelFile, 'channel_id', 'channel'),
                              (KnowledgeFile, 'knowledge_id', 'knowledge')):
        for parent in (await db.scalars(select(getattr(cls, field)).where(cls.file_id == file_id))).all():
            if (kind, parent) not in excluded:
                refs.add((kind, parent))
    for mapper in Base.registry.mappers:
        name, cls = mapper.local_table.name, mapper.class_
        if name not in ('chat', 'chat_message', 'shared_chat', 'note', 'model', 'folder', 'message'):
            continue
        fields = [c for c in mapper.columns if c.key in ('chat', 'data', 'meta', 'params', 'files', 'content')]
        if not fields:
            continue
        pk = getattr(cls, 'id')
        parent_field = 'chat_id' if name == 'chat_message' else 'channel_id' if name == 'message' else 'id'
        parent = getattr(cls, parent_field)
        # Filter candidate JSON in SQL and stream matches. This is intentionally
        # conservative, but does not load every chat body into the worker.
        from sqlalchemy import Text, cast
        candidates = or_(*(cast(field, Text).contains(file_id, autoescape=True) for field in fields))
        result = await db.stream(select(pk, parent, *fields).where(candidates))
        async for values in result:
            kind = {'chat_message': 'chat', 'shared_chat': 'share', 'message': 'channel'}.get(name, name)
            if (kind, values[1]) in excluded:
                continue
            # False positives keep a file; false negatives could destroy it.
            if file_id in json.dumps(list(values[2:]), default=str):
                refs.add((kind, values[1]))
    return sorted(refs)


async def protections(db, row, kind, options):
    reasons = []
    if kind == 'channel':
        return reasons
    if kind in ASSOCIATIONS:
        parent = await db.get(models()['knowledge'], row.knowledge_id)
        return await protections(db, parent, 'knowledge', options)
    if kind == 'chat':
        from open_webui.models.chat_messages import ChatMessage
        from open_webui.tasks import item_tasks
        if await db.scalar(select(ChatMessage.id).where(
            ChatMessage.chat_id == row.id, ChatMessage.done.is_(False),
        ).limit(1)) or row.timer_at or item_tasks.get(row.id):
            reasons.append('active_generation')
        if row.pinned and not options.get('include_pinned'):
            reasons.append('pinned')
    if kind == 'note':
        from open_webui.models.notes import PinnedNote
        if await db.scalar(select(PinnedNote.id).where(PinnedNote.note_id == row.id).limit(1)):
            if not options.get('include_pinned'):
                reasons.append('pinned')
    if kind in ('chat', 'note', 'knowledge', 'share'):
        from open_webui.models.access_grants import AccessGrant
        shared = bool(await db.scalar(select(AccessGrant.id).where(
            AccessGrant.resource_type == ('shared_chat' if kind == 'share' else kind),
            AccessGrant.resource_id == row.id,
        ).limit(1)))
        if kind == 'chat':
            shared = shared or bool(row.share_id)
        if kind == 'share':
            shared = True
        if shared and not options.get('include_shared'):
            reasons.append('shared_confirmation_required')
    if kind == 'file':
        from open_webui.utils.knowledge_import_lifecycle import has_managed_marker
        if has_managed_marker(row):
            from open_webui.utils.resource_visibility import maintenance
            if maintenance.get():
                return reasons
        state = (row.data or {}).get('status')
        if state in ('pending', 'processing', 'uploading'):
            reasons.append('active_upload_or_processing')
        refs = await references(db, row.id)
        if refs:
            reasons.append('referenced')
        # Old clients do not register draft attachments with the server.
        # Only managed uploads can ever be automatically collected.
        if (row.meta or {}).get('resource_draft_active'):
            reasons.append('draft_attachment')
    return reasons


def _epoch_seconds(value):
    """Normalise mixed-precision timestamps to epoch seconds.

    Some models store nanoseconds (time.time_ns()) and others seconds
    (time.time()).  A value above 1e15 is clearly nanoseconds.
    """
    v = int(value or 0)
    if v > 1_000_000_000_000_000:
        return v // (10 ** 9)
    return v


def describe(row, kind, entry=None):
    return {
        'id': row.id, 'type': kind, 'title': title(row, kind), 'size_bytes': size(row, kind),
        'created_at': _epoch_seconds(getattr(row, 'created_at', 0)),
        'updated_at': _epoch_seconds(getattr(row, 'updated_at', 0)),
        'version': stamp(row, entry),
        'href': {'chat': f'/c/{row.id}', 'note': f'/notes/{row.id}',
                 'knowledge': f'/workspace/knowledge/{row.id}', 'channel': f'/channels/{row.id}',
                 'share': f'/s/{row.id}', 'file': f'/api/v1/files/{row.id}/content',
                 'knowledge_file': f'/workspace/knowledge/{getattr(row, "knowledge_id", "")}',
                 'knowledge_directory': f'/workspace/knowledge/{getattr(row, "knowledge_id", "")}'}[kind],
    }


async def attachment_impact(db, row, kind, user_id):
    from open_webui.models.chats import ChatFile
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.knowledge import KnowledgeFile
    relation = {'chat': (ChatFile, 'chat_id'), 'knowledge': (KnowledgeFile, 'knowledge_id')}.get(kind)
    file_ids = set()
    if relation:
        cls, field = relation
        file_ids.update((await db.scalars(select(cls.file_id).where(getattr(cls, field) == row.id))).all())
    values = [getattr(row, name, None) for name in ('chat', 'data', 'meta')]
    if kind == 'chat':
        values += list((await db.execute(select(ChatMessage.files, ChatMessage.content).where(ChatMessage.chat_id == row.id))).all())
    serialized = json.dumps(values, default=str)
    candidates = set(re.findall(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![a-zA-Z0-9])', serialized))
    candidates.update(re.findall(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{32}(?![a-zA-Z0-9])', serialized))
    file_ids.update(candidates)
    impacts = []
    if kind == 'knowledge_file':
        file_ids.add(row.file_id)
    if not file_ids:
        return impacts
    for file in (await db.scalars(select(models()['file']).where(models()['file'].id.in_(file_ids)))).all():
        entry = await entry_for(db, 'file', file.id)
        refs = await references(db, file.id, exclude=[('knowledge', row.knowledge_id)] if kind == 'knowledge_file' else [(kind, row.id)])
        reasons = []
        if refs:
            reasons.append('referenced')
        if file.user_id != user_id:
            reasons.append('other_owner')
        if (file.data or {}).get('status') in ('pending', 'processing', 'uploading'):
            reasons.append('active_upload_or_processing')
        if (file.meta or {}).get('resource_draft_active'):
            reasons.append('draft_attachment')
        if entry and entry.state != 'active':
            reasons.append('already_recycled')
        impacts.append({'id': file.id, 'version': stamp(file, entry), 'references': refs,
                        'size_bytes': size(file, 'file'), 'reasons': reasons})
    return sorted(impacts, key=lambda item: item['id'])


async def impact_version(db, row, kind, entry, user_id):
    from open_webui.models.access_grants import AccessGrant
    from open_webui.models.notes import PinnedNote
    refs = await references(db, row.id) if kind == 'file' else []
    attachments = await attachment_impact(db, row, kind, user_id) if kind in ('chat', 'knowledge', 'note', 'knowledge_file') else []
    grants = list((await db.execute(select(AccessGrant.id).where(
        AccessGrant.resource_type == ('shared_chat' if kind == 'share' else kind),
        AccessGrant.resource_id == row.id,
    ).order_by(AccessGrant.id))).scalars().all())
    pins = list((await db.scalars(select(PinnedNote.id).where(PinnedNote.note_id == row.id))).all()) if kind == 'note' else []
    members = []
    if kind == 'knowledge_directory':
        for member_kind, member in await knowledge_descendants(db, row):
            members.append([member_kind, stamp(member, await entry_for(db, member_kind, member.id)),
                            await attachment_impact(db, member, member_kind, user_id) if member_kind == 'knowledge_file' else []])
    value = [stamp(row, entry), refs, attachments, grants, sorted(pins), members]
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest(), attachments


async def list_resources(db, user, filters=None, *, all_results=False):
    from open_webui.models.channels import ChannelMember
    filters = filters or {}
    kinds = (filters['type'],) if filters.get('type') in KINDS else KINDS
    result = []
    for kind in kinds:
        cls = models()[kind]
        query = select(cls)
        if kind == 'file':
            from open_webui.utils.knowledge_import_lifecycle import validate_managed_file
        if kind == 'channel':
            query = query.where(cls.id.in_(select(ChannelMember.channel_id).where(
                ChannelMember.user_id == user.id, ChannelMember.left_at.is_(None),
            )))
        else:
            query = query.where(cls.user_id == user.id)
        # Read only list metadata. Full histories, note bodies and share
        # snapshots are read and hashed only for an explicit impact preview.
        name = getattr(cls, {'file': 'filename', 'knowledge': 'name', 'channel': 'name'}.get(kind, 'title'))
        fields = [cls.id, name, cls.created_at, cls.updated_at]
        if kind == 'file':
            fields.append(cls.meta)
        query = query.with_only_columns(*fields)
        for values in (await db.execute(query)).all():
            rid, display_name, created, updated = values[:4]
            meta = values[4] if kind == 'file' else {}
            amount = (meta or {}).get('size', 0)
            item = {
                'id': rid, 'type': kind, 'title': display_name or 'Untitled',
                'size_bytes': max(0, int(amount)) if isinstance(amount, (int, float)) else 0,
                'created_at': _epoch_seconds(created),
                'updated_at': _epoch_seconds(updated),
                'href': {'chat': f'/c/{rid}', 'note': f'/notes/{rid}',
                         'knowledge': f'/workspace/knowledge/{rid}', 'channel': f'/channels/{rid}',
                         'share': f'/s/{rid}', 'file': f'/api/v1/files/{rid}/content'}[kind],
            }
            if filters.get('query') and filters['query'].casefold() not in item['title'].casefold():
                continue
            if filters.get('before') and item['updated_at'] > filters['before']:
                continue
            if filters.get('min_bytes', 0) > item['size_bytes']:
                continue
            if kind == 'file':
                row = await db.get(cls, rid)
                from open_webui.utils.knowledge_import_lifecycle import has_managed_marker
                if has_managed_marker(row):
                    from open_webui.utils.resource_visibility import maintenance
                    if not maintenance.get():
                        continue
            result.append(item)
    sort = filters.get('sort', 'updated_at')
    if sort not in ('title', 'size_bytes', 'updated_at'):
        sort = 'updated_at'
    result.sort(key=lambda x: (x[sort], x['type'], x['id']), reverse=sort != 'title')
    total = len(result)
    offset = filters.get('offset', 0)
    return {'items': result if all_results else result[offset:offset + filters.get('limit', 25)], 'total': total}


async def make_preview(db, user, action, items, options, filters=None, *, knowledge_write=False):
    if filters is not None:
        items = [{'type': i['type'], 'id': i['id']} for i in (await list_resources(db, user, filters, all_results=True))['items']]
    if not 1 <= len(items) <= 500:
        raise HTTPException(400, 'Select 1 to 500 resources; narrow your filters for larger selections')
    results = []
    seen = set()
    from open_webui.utils.resource_visibility import maintenance as outer_maintenance
    trusted_recovery = outer_maintenance.get()
    outer_token = outer_maintenance.set(False)
    try:
        with including_recycled():
            for item in items:
                identity = (item['type'], item['id'])
                if identity in seen:
                    continue
                seen.add(identity)
                try:
                    kind, rid = identity
                    if action in ('restore', 'purge'):
                        entry = await db.get(ResourceEntry, rid)
                        accepted = ('channel', 'channel_clear') if kind == 'channel' else (kind,)
                        if not entry or entry.user_id != user.id or entry.type not in accepted:
                            entry = await entry_for(db, 'channel_clear' if kind == 'channel' else kind, rid, user.id)
                        if not entry:
                            # Old metadata IDs are aliases, not original IDs.
                            candidates = (await db.scalars(select(ResourceEntry).where(
                                ResourceEntry.user_id == user.id, ResourceEntry.type == kind,
                                ResourceEntry.state == 'legacy',
                            ))).all()
                            entry = next((e for e in candidates if rid in e.data.get('legacy_ids', [])), None)
                        if not entry:
                            raise HTTPException(404, 'Recycle item not found')
                        if entry.state == 'legacy':
                            raise HTTPException(409, 'legacy_body_unavailable')
                        rid = entry.resource_id
                    else:
                        entry = await entry_for(db, 'channel_clear' if kind == 'channel' and not options.get('global_channel') else kind, rid, user.id)
                    row = await owned_row(db, user, kind, rid, global_channel=bool(options.get('global_channel') or (entry and entry.type == 'channel')),
                                          knowledge_write=knowledge_write or bool(entry and entry.user_id == user.id and entry.data.get('knowledge_write')))
                    if kind == 'file':
                        from open_webui.utils.knowledge_import_lifecycle import has_managed_marker

                        if has_managed_marker(row) and not trusted_recovery:
                            raise HTTPException(404, 'Resource not found')

                    reasons = await protections(db, row, kind, options) if action == 'recycle' else []
                    if action == 'restore':
                        if not entry or entry.state not in ('recycled', 'paused') or entry.expires_at <= now():
                            reasons.append('not_restorable_or_expired')
                        elif kind == 'share' and entry.data.get('restored_private'):
                            reasons.append('share_requires_republish')
                    if action == 'purge' and (not entry or entry.state not in ('recycled', 'purging', 'paused')):
                        reasons.append('not_in_recycle_bin')
                    if action == 'recycle' and kind != 'channel' and entry and entry.state in ('recycled', 'purging', 'paused'):
                        reasons.append('already_recycled')
                    item_result = {**describe(row, kind, entry), 'reasons': reasons, 'allowed': not reasons}
                    item_result['version'], attachments = await impact_version(db, row, kind, entry, user.id)
                    if attachments:
                        item_result['recycled_files'] = sum(not f['reasons'] for f in attachments)
                        item_result['retained_files'] = sum(bool(f['reasons']) for f in attachments)
                        item_result['attachment_bytes'] = sum(f['size_bytes'] for f in attachments if not f['reasons'])
                    if kind == 'file':
                        refs = await references(db, rid)
                        item_result['retained_references'] = len(refs)
                    if kind == 'chat':
                        item_result['pauses_share'] = bool(row.share_id)
                    if kind == 'channel':
                        item_result['effect'] = 'recycle_channel_for_all' if options.get('global_channel') or (entry and entry.type == 'channel') else 'clear_only_for_me'
                        item_result['scope'] = 'global' if item_result['effect'] == 'recycle_channel_for_all' else 'personal'
                    item_result['knowledge_write'] = knowledge_write or bool(entry and entry.user_id == user.id and entry.data.get('knowledge_write'))
                    if entry:
                        item_result['entry_id'] = entry.id
                    results.append(item_result)
                except HTTPException as exc:
                    results.append({**item, 'allowed': False, 'reasons': [str(exc.detail)]})
            task = ResourceTask(
                id=uid(), user_id=user.id, key=uid(), action=action, state='preview',
                created_at=now(), updated_at=now(), lease_until=0, attempts=0, results=[],
                payload={'items': results, 'options': options, 'expires_at': now() + 600, 'knowledge_write': knowledge_write},
            )
            db.add(task)
            await db.commit()
    finally:
        outer_maintenance.reset(outer_token)
    return {'id': task.id, 'expires_at': task.payload['expires_at'], 'items': results,
            'allowed': sum(r['allowed'] for r in results),
            'estimated_bytes': sum(r.get('size_bytes', 0) + r.get('attachment_bytes', 0) for r in results if r['allowed'])}


async def submit(db, user, preview_id, key):
    existing = await db.scalar(select(ResourceTask).where(ResourceTask.user_id == user.id, ResourceTask.key == key))
    if existing:
        return existing
    preview = await db.get(ResourceTask, preview_id)
    if not preview or preview.user_id != user.id or preview.state != 'preview':
        raise HTTPException(404, 'Preview not found')
    if preview.payload['expires_at'] <= now():
        raise HTTPException(409, 'Preview expired; preview again')
    if not any(item['allowed'] for item in preview.payload['items']):
        raise HTTPException(409, 'No eligible resources')
    task = ResourceTask(
        id=uid(), user_id=user.id, key=key, action=preview.action, state='pending',
        created_at=now(), updated_at=now(), lease_until=0, attempts=0,
        payload=preview.payload, results=[],
    )
    db.add(task)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        task = await db.scalar(select(ResourceTask).where(ResourceTask.user_id == user.id, ResourceTask.key == key))
    return task


async def set_entry(db, row, kind, user_id, *, state='recycled', data=None):
    entry = await entry_for(db, kind, row.id, user_id)
    if entry is None:
        entry = ResourceEntry(id=uid(), type=kind, resource_id=row.id, user_id=user_id, generation=0)
        db.add(entry)
    entry.state, entry.title = state, title(row, 'channel' if kind == 'channel_clear' else kind)
    entry.size_bytes = size(row, kind)
    if kind == 'channel_clear':
        entry.cutoff_at = time.time_ns()
    entry.deleted_at, entry.expires_at = now(), now() + RETENTION
    entry.generation += 1
    entry.data = data or {}
    return entry


async def knowledge_descendants(db, directory):
    """Keep original rows and locations; never reconstruct bodies from titles."""
    from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
    directories = (await db.scalars(select(KnowledgeDirectory).where(
        KnowledgeDirectory.knowledge_id == directory.knowledge_id,
    ))).all()
    ids, ordered = {directory.id}, []
    while True:
        children = [item for item in directories if item.parent_id in ids and item.id not in ids]
        if not children:
            break
        ids.update(item.id for item in children)
        ordered.extend(('knowledge_directory', item) for item in children)
    files = (await db.scalars(select(KnowledgeFile).where(KnowledgeFile.directory_id.in_(ids)))).all()
    if len(files) + len(ordered) > 2000:
        raise HTTPException(409, 'This folder is too large for one cleanup; select smaller groups')
    return ordered + [('knowledge_file', item) for item in files]


async def recycle(db, row, kind, user_id, *, global_channel=False, move_files=False):
    from open_webui.models.chats import ChatFile
    from open_webui.models.knowledge import KnowledgeFile
    from open_webui.models.shared_chats import SharedChat
    children = []
    if kind == 'channel' and not global_channel:
        # Save each clear operation for audit; only the latest generation can
        # be undone, so an old undo cannot reveal a newer cleared range.
        old = await entry_for(db, 'channel_clear', row.id, user_id)
        previous = {'cutoff': old.cutoff_at, 'state': old.state, 'generation': old.generation,
                    'expires_at': old.expires_at, 'deleted_at': old.deleted_at, 'data': old.data} if old else None
        return await set_entry(db, row, 'channel_clear', user_id, data={'previous': previous})
    entry = await set_entry(db, row, kind, user_id)
    if kind == 'knowledge_directory':
        if move_files:
            from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
            moved = []
            for cls, field, member_kind in ((KnowledgeDirectory, 'parent_id', 'knowledge_directory'),
                                             (KnowledgeFile, 'directory_id', 'knowledge_file')):
                for member in (await db.scalars(select(cls).where(getattr(cls, field) == row.id))).all():
                    setattr(member, field, row.parent_id)
                    moved.append({'type': member_kind, 'id': member.id, 'field': field, 'target': row.parent_id})
            entry.data = {'moved': moved}
            return entry
        else:
            for child_kind, member in await knowledge_descendants(db, row):
                old = await entry_for(db, child_kind, member.id)
                if not old or old.state == 'active':
                    child = await recycle(db, member, child_kind, user_id) if child_kind == 'knowledge_file' else await set_entry(db, member, child_kind, user_id)
                    children.append({'id': child.id, 'generation': child.generation})
    if kind == 'chat':
        for share in (await db.scalars(select(SharedChat).where(SharedChat.chat_id == row.id))).all():
            await set_entry(db, share, 'share', share.user_id, state='paused', data={'parent': entry.id})
        row.share_id = None
    if kind == 'share':
        from open_webui.models.chats import Chat
        original = await db.get(Chat, row.chat_id)
        if original and original.share_id == row.id:
            original.share_id = None
    # Attachments are only cascaded when every other persisted reference is
    # absent. Keep the relationship rows so undo restores the exact content.
    if kind in ('chat', 'knowledge', 'note', 'knowledge_file'):
        for impact in await attachment_impact(db, row, kind, user_id):
            if not impact['reasons']:
                file = await db.get(models()['file'], impact['id'])
                child = await set_entry(db, file, 'file', user_id, data={'parent': entry.id})
                children.append({'id': child.id, 'generation': child.generation})
    entry.data = {'children': children}
    return entry


async def restore(db, row, kind, entry):
    if entry.state == 'purging' or entry.expires_at <= now():
        raise HTTPException(409, 'Item expired or purge has started')
    if entry.type == 'channel_clear':
        previous = entry.data.get('previous')
        if previous:
            entry.cutoff_at = previous['cutoff']
            entry.state = 'cleared' if previous['expires_at'] <= now() else previous['state']
            entry.expires_at, entry.deleted_at = previous['expires_at'], previous['deleted_at']
            entry.data = previous['data']
        else:
            entry.state = 'active'
            entry.data = {}
        entry.generation += 1
        return
    entry.state = 'active'
    entry.generation += 1
    for moved in entry.data.get('moved', []):
        member = await db.get(models()[moved['type']], moved['id'])
        # Retain intentional moves made since deletion; never overwrite them.
        if member and getattr(member, moved['field']) == moved['target']:
            setattr(member, moved['field'], row.id)
    for child in entry.data.get('children', []):
        current = await db.get(ResourceEntry, child['id'])
        if current and current.state == 'recycled' and current.generation == child['generation']:
            child_row = await db.get(models()[current.type], current.resource_id)
            if child_row:
                await restore(db, child_row, current.type, current)
    # A restored share stays private. Republishing from the original chat
    # creates a new token; old URLs never become public as an undo side effect.
    if kind == 'share':
        entry.state = 'paused'
        entry.data = {**entry.data, 'restored_private': True, 'reason': 'share_requires_republish'}
    if kind == 'chat' and row.folder_id:
        from open_webui.models.folders import Folder
        if not await db.get(Folder, row.folder_id):
            row.folder_id = None


async def purge(db, row, kind, entry, checkpoint):
    from open_webui.models.access_grants import AccessGrant
    from open_webui.models.chats import ChatFile
    from open_webui.models.chat_messages import ChatMessage
    from open_webui.models.channels import ChannelFile
    from open_webui.models.knowledge import KnowledgeFile, KnowledgeDirectory
    from open_webui.models.notes import PinnedNote
    from open_webui.models.shared_chats import SharedChat
    if entry.type == 'channel_clear':
        entry.state = 'cleared'  # Personal hide never deletes another member's content.
        return {'logical': True, 'object_bytes': 0}
    steps = dict(entry.data.get('purge_steps', {}))
    if kind == 'knowledge_directory':
        # Children first to satisfy PostgreSQL foreign keys too. Persist a
        # checkpoint after each external deletion so a retry is idempotent.
        descendants = await knowledge_descendants(db, row)
        for child_kind, child_row in reversed(descendants):
            child = await entry_for(db, child_kind, child_row.id)
            if not child or child.state == 'active':
                raise HTTPException(409, 'Folder contents changed; preview again')
            async def child_checkpoint(child_steps, child_id=child.id):
                child.data = {**child.data, 'purge_steps': child_steps}
                await checkpoint({**steps, child_id: True})
            await purge(db, child_row, child_kind, child, child_checkpoint)
    elif kind == 'knowledge_file':
        from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
        if not steps.get('vector'):
            if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=row.knowledge_id):
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=row.knowledge_id, filter={'file_id': row.file_id})
            steps['vector'] = True
            await checkpoint(steps)
    elif kind == 'file':
        refs = await references(db, row.id)
        if refs:
            raise HTTPException(409, 'referenced')
        from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
        from open_webui.storage.provider import Storage
        from open_webui.utils.knowledge_import_lifecycle import META_KEY, validate_managed_file
        managed = validate_managed_file(row)
        if (row.meta or {}).get(META_KEY) and not managed:
            raise HTTPException(409, 'invalid_import_storage_target')
        if managed and not steps.get('knowledge_vector'):
            if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=managed['knowledge_id']):
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=managed['knowledge_id'], filter={'file_id': row.id})
            steps['knowledge_vector'] = True
            await checkpoint(steps)
        if not steps.get('vector'):
            # Delete the collection, not a backend-specific delete-with-no-IDs.
            if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=f'file-{row.id}'):
                await ASYNC_VECTOR_DB_CLIENT.delete_collection(collection_name=f'file-{row.id}')
            steps['vector'] = True
            await checkpoint(steps)
        if not steps.get('object'):
            if managed:
                await asyncio.to_thread(Storage.delete_prepared, managed['target'])
            elif row.path:
                await asyncio.to_thread(Storage.delete_file, row.path)
            steps['object'] = True
            await checkpoint(steps)
        for cls in (ChatFile, ChannelFile, KnowledgeFile):
            await db.execute(delete(cls).where(cls.file_id == row.id))
    elif kind == 'knowledge':
        from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
        if not steps.get('metadata_vector'):
            if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name='knowledge-bases'):
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name='knowledge-bases', ids=[row.id])
            steps['metadata_vector'] = True
            await checkpoint(steps)
        if not steps.get('vector'):
            if (row.meta or {}).get('source') != 'external' and await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=row.id):
                await ASYNC_VECTOR_DB_CLIENT.delete_collection(collection_name=row.id)
            steps['vector'] = True
            await checkpoint(steps)
        from open_webui.models.models import Model
        for model in (await db.scalars(select(Model).with_for_update())).all():
            meta = dict(model.meta or {})
            knowledge = meta.get('knowledge')
            if isinstance(knowledge, list):
                retained = [item for item in knowledge if not isinstance(item, dict) or item.get('id') != row.id]
                if len(retained) != len(knowledge):
                    model.meta = {**meta, 'knowledge': retained}
        # External connection settings are shared administrator-owned resources;
        # removing one knowledge base must not delete the connection itself.
        await db.execute(delete(KnowledgeFile).where(KnowledgeFile.knowledge_id == row.id))
        await db.execute(delete(KnowledgeDirectory).where(KnowledgeDirectory.knowledge_id == row.id))
    elif kind == 'chat':
        from open_webui.models.automations import AutomationRun
        await db.execute(update(AutomationRun).where(AutomationRun.chat_id == row.id).values(chat_id=None))
        await db.execute(delete(ChatMessage).where(ChatMessage.chat_id == row.id))
        await db.execute(delete(ChatFile).where(ChatFile.chat_id == row.id))
        shares = (await db.scalars(select(SharedChat).where(SharedChat.chat_id == row.id))).all()
        for share in shares:
            await db.execute(delete(AccessGrant).where(
                AccessGrant.resource_type == 'shared_chat', AccessGrant.resource_id == share.id,
            ))
            share_entry = await entry_for(db, 'share', share.id)
            if share_entry:
                share_entry.state = 'purged'
        await db.execute(delete(SharedChat).where(SharedChat.chat_id == row.id))
    elif kind == 'note':
        await db.execute(delete(PinnedNote).where(PinnedNote.note_id == row.id))
    elif kind == 'channel':
        from open_webui.models.channels import ChannelMember, ChannelWebhook
        from open_webui.models.messages import Message, MessageReaction
        await db.execute(delete(MessageReaction).where(MessageReaction.message_id.in_(
            select(Message.id).where(Message.channel_id == row.id),
        )))
        for cls in (Message, ChannelFile, ChannelMember, ChannelWebhook):
            await db.execute(delete(cls).where(cls.channel_id == row.id))
    await db.execute(delete(AccessGrant).where(
        AccessGrant.resource_type == ('shared_chat' if kind == 'share' else kind),
        AccessGrant.resource_id == row.id,
    ))
    await db.delete(row)
    entry.state = 'purged'
    # Once a parent has gone, its retained attachment can be purged. Expiry
    # processing retries dependency failures after the reference disappears.
    return {'logical': True, 'vector_removed': bool(steps.get('vector')),
            'object_bytes': entry.size_bytes if kind == 'file' and steps.get('object') else 0}


def task_response(task):
    return {key: getattr(task, key) for key in
            ('id', 'action', 'state', 'created_at', 'updated_at', 'attempts', 'results')}


class LeaseLost(Exception):
    pass


async def run_task(task_id):
    """Durable claim, per-item commits, checkpoints and fencing on retry.

    A purge belongs to one task even after its lease expires. Another preview
    cannot adopt an in-progress purge; only the same durable task may resume.
    Object and vector deletions are idempotent, and every checkpoint verifies
    ownership before changing database state.
    """
    from open_webui.models.users import Users
    token = uid()
    async with get_async_db() as db:
        claim = await db.execute(update(ResourceTask).where(
            ResourceTask.id == task_id,
            or_(ResourceTask.state == 'pending',
                and_(ResourceTask.state == 'running', ResourceTask.lease_until < now())),
        ).values(state='running', lease_token=token, lease_until=now() + 300,
                 attempts=ResourceTask.attempts + 1, updated_at=now()))
        await db.commit()
        if claim.rowcount != 1:
            return
        task = await db.get(ResourceTask, task_id)
        user = await Users.get_user_by_id(task.user_id)
        payload, action = task.payload, task.action
        results = list(task.results or [])

    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            async with get_async_db() as db:
                renewed = await db.execute(update(ResourceTask).where(
                    ResourceTask.id == task_id, ResourceTask.lease_token == token,
                    ResourceTask.state == 'running',
                ).values(lease_until=now() + 300, updated_at=now()))
                await db.commit()
                if renewed.rowcount != 1:
                    return

    async def fence(db):
        # An UPDATE reserves SQLite's writer lock too, so concurrent previews
        # cannot both pass a read-then-write version check on that database.
        result = await db.execute(update(ResourceTask).where(
            ResourceTask.id == task_id, ResourceTask.lease_token == token,
            ResourceTask.state == 'running', ResourceTask.lease_until >= now(),
        ).values(lease_until=now() + 300))
        if result.rowcount != 1:
            raise LeaseLost()
        task = await db.scalar(select(ResourceTask).where(ResourceTask.id == task_id)
                               .execution_options(populate_existing=True))
        return task

    pulse = asyncio.create_task(heartbeat())
    try:
        done = {(r['type'], r['id']) for r in results if r.get('success')}
        for item in payload['items']:
            identity = (item['type'], item['id'])
            if identity in done:
                continue
            result = {'type': item['type'], 'id': item['id'], 'success': False}
            async with get_async_db() as db:
                try:
                    task = await fence(db)
                    if not item['allowed']:
                        raise HTTPException(409, ', '.join(item['reasons']))
                    if not user:
                        raise HTTPException(403, 'Account unavailable')
                    with including_recycled():
                        row = await owned_row(db, user, item['type'], item['id'], global_channel=item.get('scope') == 'global', knowledge_write=item.get('knowledge_write', False))
                        entry_kind = 'channel_clear' if item['type'] == 'channel' and item.get('scope') != 'global' else item['type']
                        entry = await entry_for(db, entry_kind, row.id, user.id)
                        resuming = bool(entry and action == 'purge' and entry.state == 'purging'
                                        and entry.data.get('purge_task') == task_id)
                        current_version, _ = await impact_version(db, row, item['type'], entry, user.id)
                        if current_version != item['version'] and not resuming:
                            raise HTTPException(409, 'resource_changed_preview_again')
                        if action == 'recycle':
                            reasons = await protections(db, row, item['type'], payload.get('options', {}))
                            if reasons:
                                raise HTTPException(409, ', '.join(reasons))
                            recycled_entry = await recycle(db, row, item['type'], user.id, global_channel=item.get('scope') == 'global',
                                          move_files=payload.get('options', {}).get('move_files', False))
                            if item.get('knowledge_write'):
                                recycled_entry.data = {**recycled_entry.data, 'knowledge_write': True}
                        elif action == 'restore':
                            if not entry or entry.state not in ('recycled', 'paused'):
                                raise HTTPException(409, 'not_restorable_or_expired')
                            await restore(db, row, item['type'], entry)
                        else:
                            if not entry or entry.state not in ('recycled', 'paused', 'purging'):
                                raise HTTPException(409, 'not_in_recycle_bin')
                            if not resuming:
                                entry.state = 'purging'
                                entry.generation += 1
                                entry.data = {**entry.data, 'purge_task': task_id}
                            await db.commit()

                            async def checkpoint(steps):
                                nonlocal task
                                task = await fence(db)
                                entry.data = {**entry.data, 'purge_steps': dict(steps)}
                                await db.commit()

                            result.update(await purge(db, row, item['type'], entry, checkpoint))
                        task = await fence(db)
                        result['success'] = True
                        result['event_pending'] = True
                        results = [r for r in results if (r['type'], r['id']) != identity] + [result]
                        task.results, task.updated_at = results, now()
                        await db.commit()
                    # Event delivery is retried from the committed task below.
                    # A Socket.IO outage must never undo a successful result.
                except LeaseLost:
                    await db.rollback()
                    return
                except asyncio.CancelledError:
                    await db.rollback()
                    raise
                except Exception as exc:
                    await db.rollback()
                    result['reason'] = str(exc.detail) if isinstance(exc, HTTPException) else type(exc).__name__
                    log.warning('Resource task %s item %s failed: %s', task_id, identity, result['reason'])
                    results = [r for r in results if (r['type'], r['id']) != identity] + [result]
                    try:
                        task = await fence(db)
                    except LeaseLost:
                        return
                    task.results, task.updated_at = results, now()
                    await db.commit()
        async with get_async_db() as db:
            try:
                task = await fence(db)
            except LeaseLost:
                return
            successes = sum(r['success'] for r in results)
            task.state = 'completed' if successes == len(results) else 'partial' if successes else 'failed'
            task.results, task.lease_until, task.updated_at = results, 0, now()
            await db.commit()
        await deliver_task_events(task_id)
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse


async def resource_changed(user_id, kind, resource_id, action):
    """No private body is included; each device re-fetches through its guard."""
    from open_webui.socket.main import sio
    await sio.emit('resources:changed', {
        'type': kind, 'id': resource_id, 'action': action,
    }, room=f'user:{user_id}')
    if kind == 'note' and action != 'restore':
        await sio.close_room(f'note:{resource_id}')
        await sio.close_room(f'doc_note:{resource_id}')
        from open_webui.socket.main import YDOC_MANAGER
        async with YDOC_MANAGER.lock(f'note:{resource_id}'):
            await YDOC_MANAGER.clear_document(f'note:{resource_id}')


async def deliver_task_events(task_id):
    async with get_async_db() as db:
        task = await db.scalar(select(ResourceTask).where(ResourceTask.id == task_id).with_for_update())
        if not task:
            return
        results = [dict(item) for item in task.results]
        for item in results:
            if not item.get('success') or not item.get('event_pending'):
                continue
            try:
                await resource_changed(task.user_id, item['type'], item['id'], task.action)
                item['event_pending'] = False
            except Exception:
                log.warning('Resource task %s: refresh event deferred', task_id, exc_info=True)
        task.results = results
        await db.commit()


async def legacy_delete(user, kind, resource_id, *, options=None):
    """Old DELETE routes use the same policy, never an irreversible shortcut."""
    async with get_async_db() as db:
        preview = await make_preview(db, user, 'recycle', [{'type': kind, 'id': resource_id}], options or {}, knowledge_write=kind == 'knowledge')
        if not preview['allowed']:
            raise HTTPException(409, {'reason': 'protected_resource', 'items': preview['items']})
        task = await submit(db, user, preview['id'], uid())
        task_id = task.id
    await run_task(task_id)
    async with get_async_db() as db:
        task = await db.get(ResourceTask, task_id)
        if task.state != 'completed':
            raise HTTPException(409, {'reason': 'resource_operation_failed', 'results': task.results})
    return True


async def legacy_delete_many(user, kind, ids, *, options=None):
    if not ids:
        return True
    if len(ids) > 500:
        raise HTTPException(409, 'Use paginated Resources & Storage cleanup for more than 500 items')
    async with get_async_db() as db:
        preview = await make_preview(db, user, 'recycle', [{'type': kind, 'id': id} for id in ids], options or {})
        if preview['allowed'] != len(ids):
            raise HTTPException(409, {'reason': 'Some items are protected; review Resources & Storage', 'items': preview['items']})
        task = await submit(db, user, preview['id'], uid())
        task_id = task.id
    await run_task(task_id)
    async with get_async_db() as db:
        task = await db.get(ResourceTask, task_id)
        if task.state != 'completed':
            raise HTTPException(409, {'results': task.results})
    return True


async def knowledge_cleanup(user, knowledge_id, *, file_ids=(), directory_ids=(), move_files=False, confirm_shared=False):
    """All old detach/reset/sync paths retain associations for seven days."""
    from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
    async with get_async_db() as db:
        await owned_row(db, user, 'knowledge', knowledge_id, knowledge_write=True)
        dirs = (await db.scalars(select(KnowledgeDirectory).where(
            KnowledgeDirectory.knowledge_id == knowledge_id, KnowledgeDirectory.id.in_(directory_ids),
        ))).all()
        selected_dirs = {row.id for row in dirs}
        roots = [row for row in dirs if row.parent_id not in selected_dirs]
        covered = set()
        if not move_files:
            for root in roots:
                covered.update(row.id for kind, row in await knowledge_descendants(db, root) if kind == 'knowledge_file')
        links = (await db.scalars(select(KnowledgeFile).where(
            KnowledgeFile.knowledge_id == knowledge_id, KnowledgeFile.file_id.in_(file_ids),
        ))).all()
        items = [{'type': 'knowledge_directory', 'id': row.id} for row in roots]
        items.extend({'type': 'knowledge_file', 'id': row.id} for row in links if row.id not in covered)
        if not items:
            return True
        preview = await make_preview(db, user, 'recycle', items,
                                     {'move_files': move_files, 'include_shared': confirm_shared}, knowledge_write=True)
        if preview['allowed'] != len(items):
            raise HTTPException(409, {'reason': 'Protected knowledge content; review impact first', 'items': preview['items']})
        task = await submit(db, user, preview['id'], uid())
        task_id = task.id
    await run_task(task_id)
    async with get_async_db() as db:
        task = await db.get(ResourceTask, task_id)
        if task.state != 'completed':
            raise HTTPException(409, {'reason': 'Knowledge cleanup incomplete; inspect tasks', 'results': task.results})
    return True


async def maintenance_tick():
    from open_webui.utils.knowledge_import_lifecycle import recover_expired
    task_ids = await recover_expired()
    async with get_async_db() as db:
        task_ids.extend(list((await db.scalars(select(ResourceTask.id).where(
            or_(ResourceTask.state == 'pending',
                and_(ResourceTask.state == 'running', ResourceTask.lease_until < now())),
        ).limit(20))).all()))
        # Purges that lost an external service remain durable even though the
        # item is already in 'purging', rather than silently getting stranded.
        failed_purges = (await db.scalars(select(ResourceTask).where(
            ResourceTask.action == 'purge', ResourceTask.state.in_(('failed', 'partial')),
            ResourceTask.updated_at < now() - 60,
        ).limit(20))).all()
        for task in failed_purges:
            if any(item.data.get('purge_task') == task.id for item in (await db.scalars(select(ResourceEntry).where(ResourceEntry.state == 'purging'))).all()):
                task.state, task.lease_until = 'pending', 0
                task_ids.append(task.id)
        entries = (await db.scalars(select(ResourceEntry).where(
            ResourceEntry.state.in_(('recycled', 'paused')), ResourceEntry.expires_at <= now(),
        ).limit(50))).all()
        from open_webui.models.users import Users
        with including_recycled():
            for entry in entries:
                if entry.type == 'channel_clear':
                    entry.state = 'cleared'
                    continue
                row = await db.get(models()[entry.type], entry.resource_id)
                if not row:
                    entry.state = 'purged'
                    continue
                key = f'expiry:{entry.id}:{entry.generation}'
                old_task = await db.scalar(select(ResourceTask).where(ResourceTask.user_id == entry.user_id, ResourceTask.key == key))
                if old_task:
                    if old_task.state in ('failed', 'partial') and old_task.updated_at < now() - 60:
                        # A parent may have been purged after its attachment was
                        # retained. Recheck before retrying that dependency.
                        if entry.type != 'file' or not await references(db, entry.resource_id):
                            old_task.state, old_task.lease_until = 'pending', 0
                            task_ids.append(old_task.id)
                    continue
                item = {**describe(row, entry.type, entry), 'allowed': True, 'reasons': [], 'entry_id': entry.id,
                        **({'scope': 'global'} if entry.type == 'channel' else {})}
                item['version'], _ = await impact_version(db, row, entry.type, entry, entry.user_id)
                task = ResourceTask(id=uid(), user_id=entry.user_id, key=key, action='purge', state='pending',
                                    created_at=now(), updated_at=now(), lease_until=0, attempts=0,
                                    results=[], payload={'items': [item], 'options': {}})
                db.add(task)
                task_ids.append(task.id)
            await db.commit()
        rule = await db.get(ResourceRule, 'default')
        if rule and rule.temporary_uploads:
            from open_webui.models.files import File
            files = (await db.scalars(select(File).where(File.updated_at < now() - RETENTION).limit(100))).all()
            for file in files:
                # Opt-in metadata only: legacy uploads may still be unsent drafts.
                if (file.meta or {}).get('resource_temporary') is not True:
                    continue
                with including_recycled():
                    old = await entry_for(db, 'file', file.id)
                    if (not old or old.state == 'active') and not await protections(db, file, 'file', {}):
                        owner = await Users.get_user_by_id(file.user_id)
                        if owner:
                            preview = await make_preview(db, owner, 'recycle', [{'type': 'file', 'id': file.id}], {})
                            if preview['allowed']:
                                task = await submit(db, owner, preview['id'], f'temporary:{file.id}:{file.updated_at}')
                                task_ids.append(task.id)
            await db.commit()
        await db.execute(delete(ResourceTask).where(ResourceTask.state == 'preview', ResourceTask.created_at < now() - 86400))
        await db.commit()
    for task_id in dict.fromkeys(task_ids):
        await run_task(task_id)
    async with get_async_db() as db:
        events = list((await db.scalars(select(ResourceTask.id).where(
            ResourceTask.state.in_(('completed', 'partial', 'failed')),
        ).order_by(ResourceTask.updated_at.desc()).limit(200))).all())
    for task_id in events:
        await deliver_task_events(task_id)


async def resource_worker():
    while True:
        try:
            await maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Resource worker tick failed; durable tasks will be retried')
        await asyncio.sleep(30)
