"""User-owned resource management. Administrative responses contain aggregates only."""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.internal.db import get_async_session
from open_webui.models.resources import ResourceEntry, ResourceRule, ResourceTask
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils import resource_service as service
from open_webui.utils.resource_visibility import including_recycled

router = APIRouter()
Kind = Literal['chat', 'file', 'note', 'knowledge', 'share', 'channel', 'knowledge_file', 'knowledge_directory']


class Item(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Kind
    id: str = Field(min_length=1, max_length=200)


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    include_pinned: bool = False
    include_shared: bool = False


class Filters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Kind | None = None
    query: str = Field(default='', max_length=200)
    before: int | None = Field(default=None, ge=0)
    min_bytes: int = Field(default=0, ge=0)
    sort: Literal['title', 'size_bytes', 'updated_at'] = 'updated_at'


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['recycle', 'restore', 'purge']
    items: list[Item] = Field(default_factory=list, max_length=500)
    filters: Filters | None = None
    options: Options = Field(default_factory=Options)


class Execute(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=100)


@router.get('/capabilities')
async def capabilities(user=Depends(get_verified_user)):
    return {'protocol': 2, 'retention_days': 7, 'types': service.KINDS,
            'preflight_required': True, 'personal_channel_clear': True}


@router.get('/overview')
async def overview(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    resources = (await service.list_resources(db, user, all_results=True))['items']
    counts = {kind: sum(r['type'] == kind for r in resources) for kind in service.KINDS}
    recycled = (await db.scalars(select(ResourceEntry).where(
        ResourceEntry.user_id == user.id, ResourceEntry.state.in_(('recycled', 'purging', 'paused', 'legacy')),
    ))).all()
    tasks = (await db.scalars(select(ResourceTask).where(
        ResourceTask.user_id == user.id, ResourceTask.state != 'preview',
    ))).all()
    return {
        'protocol': 2, 'counts': counts, 'chats': counts['chat'], 'files': counts['file'],
        'notes': counts['note'], 'knowledge': counts['knowledge'],
        'total_bytes': sum(r['size_bytes'] for r in resources),
        'recycled_count': len(recycled),
        'recycled_bytes': sum(r.size_bytes for r in recycled if r.type == 'file'),
        'object_bytes_removed': sum(
            r.get('object_bytes', 0) for task in tasks for r in task.results if r.get('success')
        ),
        'failed_tasks': sum(task.state in ('failed', 'partial') for task in tasks),
        'capacity_basis': 'registered_upload_bytes',
    }


@router.get('/list')
async def resources(
    type: Kind | None = None, query: str = Query(default='', max_length=200),
    before: int | None = Query(default=None, ge=0), min_bytes: int = Query(default=0, ge=0),
    sort: Literal['title', 'size_bytes', 'updated_at'] = 'updated_at',
    offset: int = Query(default=0, ge=0), limit: int = Query(default=25, ge=1, le=100),
    user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session),
):
    return await service.list_resources(db, user, dict(
        type=type, query=query, before=before, min_bytes=min_bytes, sort=sort, offset=offset, limit=limit,
    ))


@router.post('/preflight')
async def preflight(body: Preview, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    if body.filters and body.items:
        raise HTTPException(400, 'Choose explicit items or all matching filters')
    return await service.make_preview(
        db, user, body.action, [i.model_dump() for i in body.items], body.options.model_dump(),
        body.filters.model_dump() if body.filters else None,
    )


@router.post('/tasks')
async def execute(body: Execute, background_tasks: BackgroundTasks, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    task = await service.submit(db, user, body.preview_id, body.idempotency_key)
    task_id = task.id
    background_tasks.add_task(service.run_task, task_id)
    return service.task_response(task)


@router.get('/tasks')
async def tasks(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    rows = (await db.scalars(select(ResourceTask).where(
        ResourceTask.user_id == user.id, ResourceTask.state != 'preview',
    ).order_by(ResourceTask.created_at.desc()).limit(100))).all()
    return [service.task_response(task) for task in rows]


@router.post('/tasks/{id}/retry')
async def retry(id: str, background_tasks: BackgroundTasks, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    task = await db.get(ResourceTask, id)
    if not task or task.user_id != user.id:
        raise HTTPException(404, 'Task not found')
    if task.state not in ('failed', 'partial'):
        raise HTTPException(409, 'Task is not retryable')
    if task.action != 'purge':
        raise HTTPException(409, 'Preview the remaining resources again')
    task.state, task.lease_until = 'pending', 0
    await db.commit()
    background_tasks.add_task(service.run_task, id)
    return service.task_response(task)


class TemporaryUpload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    draft_active: bool
    draft_id: str = Field(default='legacy', min_length=1, max_length=200)


@router.put('/uploads/{id}/temporary')
async def register_temporary_upload(id: str, body: TemporaryUpload, user=Depends(get_verified_user),
                                    db: AsyncSession = Depends(get_async_session)):
    """Explicit client registration; unknown/legacy uploads remain protected."""
    row = await service.owned_row(db, user, 'file', id)
    drafts = set((row.meta or {}).get('resource_draft_ids', []))
    if body.draft_active:
        drafts.add(body.draft_id)
    else:
        drafts.discard(body.draft_id)
    row.meta = {**(row.meta or {}), 'resource_temporary': True,
                'resource_draft_active': bool(drafts), 'resource_draft_ids': sorted(drafts)}
    row.updated_at = service.now()
    await db.commit()
    return {'registered': True, 'draft_active': bool(drafts)}


def recycled_response(entry):
    return {
        'id': entry.id, 'original_id': entry.resource_id,
        'type': 'channel' if entry.type == 'channel_clear' else entry.type,
        'title': entry.title, 'size_bytes': entry.size_bytes if entry.type != 'channel_clear' else 0,
        'deleted_at': entry.deleted_at, 'expires_at': entry.expires_at, 'state': entry.state,
        'restorable': entry.state in ('recycled', 'paused') and entry.expires_at > service.now() and not entry.data.get('restored_private'),
        'meta': {'reason': entry.data.get('reason'), 'generation': entry.generation,
                 'effect': 'clear_only_for_me' if entry.type == 'channel_clear' else 'recycle'},
    }


@router.get('/recycle-bin')
async def recycle_bin(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    rows = (await db.scalars(select(ResourceEntry).where(
        ResourceEntry.user_id == user.id, ResourceEntry.state.in_(('recycled', 'purging', 'paused', 'legacy')),
    ).order_by(ResourceEntry.deleted_at.desc()))).all()
    return [recycled_response(entry) for entry in rows]


class LegacyDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    type: Kind
    permanent: bool = False


async def legacy_action(body, action, user, db):
    preview = await service.make_preview(db, user, action,
        [{'id': id, 'type': body.type} for id in body.ids], {})
    if not preview['allowed']:
        raise HTTPException(409, {'reason': 'No eligible resources', 'items': preview['items']})
    task = await service.submit(db, user, preview['id'], service.uid())
    await service.run_task(task.id)
    await db.refresh(task)
    count = sum(r['success'] for r in task.results)
    if not count:
        raise HTTPException(409, {'results': task.results})
    return {'restored' if action == 'restore' else 'deleted': count,
            'permanent': action == 'purge', 'results': task.results, 'task_id': task.id}


@router.post('/delete')
async def legacy_delete(body: LegacyDelete, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    return await legacy_action(body, 'purge' if body.permanent else 'recycle', user, db)


@router.post('/restore')
async def legacy_restore(body: LegacyDelete, user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    return await legacy_action(body, 'restore', user, db)


@router.post('/recycle-bin/purge-expired')
async def purge_expired(user=Depends(get_admin_user)):
    await service.maintenance_tick()
    return {'queued': True}


@router.get('/recycle-bin/{resource_type}/{resource_id}')
async def get_recycle_item(resource_type: Kind, resource_id: str, user=Depends(get_verified_user),
                           db: AsyncSession = Depends(get_async_session)):
    entry = await db.get(ResourceEntry, resource_id)
    accepted = ('channel', 'channel_clear') if resource_type == 'channel' else (resource_type,)
    if not entry or entry.user_id != user.id or entry.type not in accepted:
        entry = await service.entry_for(db, 'channel_clear' if resource_type == 'channel' else resource_type, resource_id, user.id)
    if not entry or entry.user_id != user.id:
        raise HTTPException(404, 'Item not found')
    return recycled_response(entry)


class RuleUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    temporary_uploads: bool


@router.get('/rules')
async def rules(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    rule = await db.get(ResourceRule, 'default')
    return {'retention_days': 7, 'temporary_uploads': bool(rule and rule.temporary_uploads),
            'temporary_idle_days': 7, 'can_edit': user.role == 'admin',
            'temporary_scope': 'explicitly_registered_unreferenced_uploads'}


@router.put('/rules')
async def set_rules(body: RuleUpdate, user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    rule = await db.get(ResourceRule, 'default')
    if not rule:
        rule = ResourceRule(id='default')
        db.add(rule)
    rule.temporary_uploads, rule.updated_at, rule.updated_by = body.temporary_uploads, service.now(), user.id
    await db.commit()
    return await rules(user, db)


@router.get('/admin/overview')
async def admin_overview(user=Depends(get_admin_user), db: AsyncSession = Depends(get_async_session)):
    with including_recycled():
        accounts = {}
        for kind in service.KINDS:
            cls = service.models()[kind]
            rows = (await db.execute(select(cls.user_id, func.count()).group_by(cls.user_id))).all()
            for user_id, count in rows:
                accounts.setdefault(user_id, {'user_id': user_id, 'counts': {}})['counts'][kind] = count
        states = (await db.execute(select(
            ResourceTask.user_id, ResourceTask.state, func.count(),
        ).where(ResourceTask.state != 'preview').group_by(ResourceTask.user_id, ResourceTask.state))).all()
        for user_id, state, count in states:
            accounts.setdefault(user_id, {'user_id': user_id, 'counts': {}}).setdefault('tasks', {})[state] = count
        return {'accounts': list(accounts.values())}
