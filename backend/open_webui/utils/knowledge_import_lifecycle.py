"""Durable reservations and conservative recovery for knowledge imports only.

The storage target and fencing token are server-owned metadata, never accepted
from a multipart client. A stable File primary key serializes an operation.
"""
from __future__ import annotations

import logging
import time
import uuid

from sqlalchemy import and_, or_

from sqlalchemy import select, update

from open_webui.internal.db import get_async_db
from open_webui.models.resources import ResourceEntry, ResourceTask
from open_webui.utils.resource_visibility import including_recycled

log = logging.getLogger(__name__)
META_KEY = 'knowledge_import_lifecycle'
LEASE_SECONDS = 300


def file_id_for(owner_id: str, knowledge_id: str, operation_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'open-webui:knowledge-import:v2:{owner_id}:{knowledge_id}:{operation_id}'))


def exclude_imported_files(file_model=None):
    """SQL filter that hides v2 import records from personal listings.

    The completed association remains queryable through its knowledge base;
    ordinary files are unaffected.
    """
    from open_webui.models.files import File
    model = file_model or File
    return model.meta[META_KEY].is_(None)


def has_managed_marker(row) -> bool:
    """Return whether a trusted server field was supplied, even if malformed."""
    meta = getattr(row, 'meta', None)
    return isinstance(meta, dict) and META_KEY in meta


def validate_managed_file(row: File) -> dict | None:
    """Reject client supplied or corrupted records before any recovery/purge."""
    state = (row.meta or {}).get(META_KEY)
    if not isinstance(state, dict) or state.get('version') != 1:
        return None
    owner, knowledge_id, operation_id = state.get('owner_id'), state.get('knowledge_id'), state.get('operation_id')
    target = state.get('target')
    if not all(isinstance(v, str) and v for v in (owner, knowledge_id, operation_id)):
        return None
    try:
        if str(uuid.UUID(operation_id)) != operation_id or row.id != file_id_for(owner, knowledge_id, operation_id):
            return None
    except (ValueError, TypeError):
        return None
    if owner != row.user_id or not isinstance(target, dict) or row.path != target.get('primary_path'):
        return None
    name = target.get('storage_name')
    if not isinstance(name, str) or not name.startswith(row.id + '.'):
        return None
    return state


def managed_file_visible_in_knowledge(row, knowledge_id: str) -> bool:
    """Filter a knowledge join without trusting public import markers."""
    if not has_managed_marker(row):
        return True
    managed = validate_managed_file(row)
    return bool(
        managed
        and managed.get('state') == 'completed'
        and managed.get('knowledge_id') == knowledge_id
    )


async def renew(file_id: str, token: str, generation: int, *, state: str | None = None) -> bool:
    from open_webui.models.files import File
    now = int(time.time())
    async with get_async_db() as db:
        row = await db.get(File, file_id)
        managed = validate_managed_file(row) if row else None
        if not managed or managed.get('lease_token') != token or managed.get('generation') != generation:
            return False
        if managed.get('state') not in ('reserved', 'uploading', 'processing') or managed.get('lease_until', 0) < now:
            return False
        next_managed = {**managed, 'lease_until': now + LEASE_SECONDS}
        if state:
            next_managed['state'] = state
        result = await db.execute(update(File).where(
            File.id == file_id,
            File.meta[META_KEY]['lease_token'].as_string() == token,
            File.meta[META_KEY]['generation'].as_integer() == generation,
            File.meta[META_KEY]['state'].as_string() == managed['state'],
            File.meta[META_KEY]['lease_until'].as_integer() >= now,
        ).values(meta={**row.meta, META_KEY: next_managed}, updated_at=now))
        await db.commit()
        return result.rowcount == 1


async def recover_expired(limit: int = 50) -> list[str]:
    """Claim expired imports and submit at most one durable recycle task each."""
    from open_webui.models.files import File
    from open_webui.models.users import Users
    from open_webui.utils import resource_service as service

    now = int(time.time())
    task_ids = []
    with including_recycled():
        async with get_async_db() as db:
            ids = list((await db.scalars(select(File.id).where(
                File.meta[META_KEY]['version'].as_integer() == 1,
                File.meta[META_KEY]['state'].as_string().in_(('reserved', 'uploading', 'processing', 'failed', 'interrupted', 'cleanup_pending')),
                File.meta[META_KEY]['lease_until'].as_integer() < now,
            ).limit(limit))).all())
        for file_id in ids:
          with including_recycled():
            try:
                async with get_async_db() as db:
                    row = await db.get(File, file_id)
                    managed = validate_managed_file(row) if row else None
                    if not managed:
                        continue
                    old_state = managed.get('state')
                    key = f'knowledge-import:{file_id}:recycle'
                    prior = await db.scalar(select(ResourceTask).where(ResourceTask.user_id == row.user_id, ResourceTask.key == key))
                    if prior and prior.state in ('pending', 'running', 'completed'):
                        if prior.state in ('pending', 'running'):
                            task_ids.append(prior.id)
                        continue
                    old_token = managed.get('lease_token')
                    generation = managed.get('generation')
                    if old_state not in ('reserved', 'uploading', 'processing', 'failed', 'interrupted', 'cleanup_pending') or managed.get('lease_until', 0) >= now:
                        continue
                    claimed = {**managed, 'state': 'cleanup_pending', 'lease_until': now + LEASE_SECONDS,
                               'lease_token': str(uuid.uuid4()), 'generation': generation + 1}
                    result = await db.execute(update(File).where(
                        File.id == file_id,
                        File.meta[META_KEY]['lease_token'].as_string() == old_token,
                        File.meta[META_KEY]['generation'].as_integer() == generation,
                        File.meta[META_KEY]['state'].as_string() == old_state,
                        File.meta[META_KEY]['lease_until'].as_integer() < now,
                    ).values(meta={**row.meta, META_KEY: claimed},
                             data={**(row.data or {}), 'status': 'failed'}, updated_at=now))
                    await db.commit()
                    if result.rowcount != 1:
                        continue
                    row = await db.get(File, file_id, populate_existing=True)
                    # Never remove an item that reached a business association.
                    if await service.protections(db, row, 'file', {}):
                        continue
                    owner = await Users.get_user_by_id(row.user_id, db=db)
                    if not owner:
                        continue
                    existing = await db.scalar(select(ResourceTask).where(ResourceTask.user_id == owner.id, ResourceTask.key == key))
                    entry = await service.entry_for(db, 'file', file_id, owner.id)
                    if entry and entry.state in ('recycled', 'purging', 'paused'):
                        continue
                    if existing and existing.state not in ('failed', 'partial'):
                        if existing.state in ('pending', 'running'):
                            task_ids.append(existing.id)
                        continue
                    preview = await service.make_preview(db, owner, 'recycle', [{'type': 'file', 'id': file_id}], {})
                    if preview['allowed']:
                        if existing:
                            # Retain the stable task identity but refresh its
                            # preflight after a recoverable or stale failure.
                            preview_row = await db.get(ResourceTask, preview['id'])
                            existing.payload = preview_row.payload
                            existing.results = []
                            existing.state = 'pending'
                            existing.lease_until = 0
                            existing.updated_at = now
                            await db.commit()
                            task = existing
                        else:
                            task = await service.submit(db, owner, preview['id'], key)
                        task_ids.append(task.id)
            except Exception:
                log.exception('Could not recover import reservation %s', file_id)
    return task_ids
