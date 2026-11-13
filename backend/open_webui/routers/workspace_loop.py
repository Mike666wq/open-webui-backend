"""Server-owned workspace loop: user → chat → note → knowledge → chat.

This router is included before ``notes.router`` so its literal paths win over
the legacy ``/notes/{id}`` catch-all.  The browser no longer coordinates
upload/attach/index/meta writes; every state transition is validated here.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from open_webui.env import USE_SLIM
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.chats import Chats
from open_webui.models.config import Config
from open_webui.models.files import File as FileTable
from open_webui.models.files import FileForm, Files
from open_webui.models.groups import Groups
from open_webui.models.knowledge import Knowledges
from open_webui.models.notes import Note as NoteTable
from open_webui.models.notes import NoteForm, Notes
from open_webui.models.users import UserModel
from open_webui.retrieval.utils import get_embedding_function
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.routers.knowledge import is_external_knowledge
from open_webui.routers.notes import check_notes_access
from open_webui.routers.retrieval import ProcessFileForm, process_file
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import (
    has_permission,
    has_public_read_access_grant,
    has_public_write_access_grant,
)
from open_webui.utils.auth import get_verified_user
from open_webui.utils.workspace_loop import (
    canonical_note_content,
    canonical_note_title,
    classify_snapshot_state,
    extract_chat_message_content,
    merge_note_knowledge_snapshot,
    note_snapshot_fingerprint,
    safe_markdown_filename,
    structured_loop_error,
)
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)
router = APIRouter()


class WorkspaceLoopSyncForm(BaseModel):
    force: bool = False


class NoteFromChatForm(BaseModel):
    chat_id: str
    message_id: str
    title: str | None = None
    knowledge_id: str | None = None


async def _user_group_ids(user: UserModel, db: AsyncSession) -> set[str]:
    groups = await Groups.get_groups_by_member_id(user.id, db=db, include_inherited=True)
    return {group.id for group in groups}


async def _note_access(
    note: Any, user: UserModel, permission: str, db: AsyncSession, group_ids: set[str] | None = None
) -> bool:
    if user.role == 'admin' or note.user_id == user.id:
        return True
    grants = getattr(note, 'access_grants', None) or []
    if permission == 'read' and has_public_read_access_grant(grants):
        return True
    if permission == 'write' and has_public_write_access_grant(grants):
        return True
    return await AccessGrants.has_access(
        user_id=user.id,
        resource_type='note',
        resource_id=note.id,
        permission=permission,
        user_group_ids=group_ids,
        db=db,
    )


async def _knowledge_access(
    knowledge: Any, user: UserModel, permission: str, db: AsyncSession, group_ids: set[str] | None = None
) -> bool:
    if user.role == 'admin' or knowledge.user_id == user.id:
        return True
    grants = getattr(knowledge, 'access_grants', None) or []
    if permission == 'read' and has_public_read_access_grant(grants):
        return True
    if permission == 'write' and has_public_write_access_grant(grants):
        return True
    return await AccessGrants.has_access(
        user_id=user.id,
        resource_type='knowledge',
        resource_id=knowledge.id,
        permission=permission,
        user_group_ids=group_ids,
        db=db,
    )


async def _load_note(note_id: str, user: UserModel, db: AsyncSession, permission: str = 'read') -> Any:
    note = await Notes.get_note_by_id(note_id, db=db)
    if not note:
        raise structured_loop_error('note_not_found', 'preflight', 'Note not found.', False, status_code=404)
    if not await _note_access(note, user, permission, db):
        raise structured_loop_error(
            'access_prohibited', 'preflight', 'No permission to use this note.', False, status_code=403
        )
    return note


async def _load_writable_knowledge(knowledge_id: str, user: UserModel, db: AsyncSession) -> Any:
    knowledge = await Knowledges.get_knowledge_by_id(knowledge_id, db=db)
    if not knowledge:
        raise structured_loop_error(
            'knowledge_not_found', 'preflight', 'Knowledge base not found.', False, status_code=404
        )
    if is_external_knowledge(knowledge):
        raise structured_loop_error(
            'external_knowledge_read_only',
            'preflight',
            'External knowledge bases are read-only.',
            False,
            status_code=400,
        )
    if not await _knowledge_access(knowledge, user, 'write', db):
        raise structured_loop_error(
            'access_prohibited', 'preflight', 'No write permission for this knowledge base.', False, status_code=403
        )
    return knowledge


async def _embedding_readiness(request: Request) -> dict:
    bypass = bool(await Config.get('rag.bypass_embedding_and_retrieval'))
    engine = (await Config.get('rag.embedding_engine')) or ''
    model = (await Config.get('rag.embedding_model')) or ''
    openai_key = (await Config.get('rag.openai.api_key')) or ''
    openai_url = (await Config.get('rag.openai.api_base_url')) or ''
    ollama_url = (await Config.get('rag.ollama.url')) or ''
    azure_key = (await Config.get('rag.azure_openai.api_key')) or ''
    azure_url = (await Config.get('rag.azure_openai.api_base_url')) or ''

    ready = False
    reason_code = None
    if bypass:
        ready = True
    elif engine == 'openai':
        ready = bool(model and openai_key and openai_url)
        reason_code = None if ready else 'embedding_not_configured'
    elif engine == 'ollama':
        ready = bool(model and ollama_url)
        reason_code = None if ready else 'embedding_not_configured'
    elif engine == 'azure_openai':
        ready = bool(model and azure_key and azure_url)
        reason_code = None if ready else 'embedding_not_configured'
    elif engine == '':
        ready = (not USE_SLIM) and callable(getattr(request.app.state, 'EMBEDDING_FUNCTION', None))
        reason_code = None if ready else 'embedding_not_configured'
    else:
        reason_code = 'embedding_engine_unsupported'

    return {
        'ready': ready,
        'engine': engine,
        'model': model,
        'bypass': bypass,
        'reason_code': reason_code,
    }


async def _ensure_embedding_function(request: Request, readiness: dict) -> None:
    """Refresh the app embedding callable from persisted Config before indexing."""
    if readiness.get('bypass') or not readiness.get('ready'):
        return
    engine = readiness.get('engine') or ''
    if engine == '':
        return
    model = readiness.get('model') or ''
    batch = int(await Config.get('rag.embedding_batch_size') or 10)
    if engine == 'openai':
        url = (await Config.get('rag.openai.api_base_url')) or ''
        key = (await Config.get('rag.openai.api_key')) or ''
        request.app.state.EMBEDDING_FUNCTION = get_embedding_function(engine, model, None, url, key, batch)
    elif engine == 'ollama':
        url = (await Config.get('rag.ollama.url')) or ''
        request.app.state.EMBEDDING_FUNCTION = get_embedding_function(engine, model, None, url, '', batch)
    elif engine == 'azure_openai':
        url = (await Config.get('rag.azure_openai.api_base_url')) or ''
        key = (await Config.get('rag.azure_openai.api_key')) or ''
        api_version = (await Config.get('rag.azure_openai.api_version')) or ''
        request.app.state.EMBEDDING_FUNCTION = get_embedding_function(engine, model, None, url, key, batch, api_version)


async def _knowledge_bases_payload(user: UserModel, db: AsyncSession) -> list[dict]:
    group_ids = await _user_group_ids(user, db)
    bases = await Knowledges.get_knowledge_bases(skip=0, limit=100000, db=db)
    payload: list[dict] = []
    for base in bases:
        can_read = await _knowledge_access(base, user, 'read', db, group_ids)
        if not can_read:
            continue
        external = is_external_knowledge(base)
        can_write = (not external) and await _knowledge_access(base, user, 'write', db, group_ids)
        payload.append(
            {
                'id': base.id,
                'name': base.name,
                'description': getattr(base, 'description', '') or '',
                'write_access': bool(can_write),
                'external': bool(external),
            }
        )
    return payload


async def _workspace_loop_readiness(request: Request, user: UserModel, db: AsyncSession) -> dict:
    await check_notes_access(user, db=db)
    default_permissions = await Config.get('user.permissions')
    features_notes = user.role == 'admin' or await has_permission(user.id, 'features.notes', default_permissions, db=db)
    workspace_knowledge = user.role == 'admin' or await has_permission(
        user.id, 'workspace.knowledge', default_permissions, db=db
    )
    embedding = await _embedding_readiness(request)
    knowledge_bases = await _knowledge_bases_payload(user, db)
    writable = [base for base in knowledge_bases if base['write_access'] and not base['external']]

    ready = True
    reason_code = None
    if not embedding['ready']:
        ready = False
        reason_code = embedding['reason_code'] or 'embedding_not_configured'
    elif not writable:
        ready = False
        reason_code = 'no_writable_knowledge'

    return {
        'ready': ready,
        'reason_code': reason_code,
        'user': {
            'id': user.id,
            'role': user.role,
            'notes_enabled': bool(await Config.get('notes.enable')),
            'features_notes': bool(features_notes),
            'workspace_knowledge': bool(workspace_knowledge),
        },
        'embedding': embedding,
        'knowledge_bases': knowledge_bases,
        'features': {
            'notes': bool(features_notes),
            'knowledge': bool(workspace_knowledge or knowledge_bases),
            'chat_import': True,
        },
    }


def _file_index_state(file: Any, knowledge_id: str) -> tuple[str, str | None]:
    data = dict(getattr(file, 'data', None) or {})
    meta = dict(getattr(file, 'meta', None) or {})
    index_state = dict(meta.get('index_state') or {})
    per_kb = index_state.get(knowledge_id) if isinstance(index_state.get(knowledge_id), dict) else {}
    raw = str(per_kb.get('state') or data.get('status') or meta.get('status') or 'unknown').lower()
    if raw not in {'completed', 'pending', 'failed', 'unknown'}:
        raw = 'unknown'
    error = per_kb.get('error') or data.get('error') or meta.get('error')
    return raw, str(error) if error else None


async def _snapshot_entry(
    note: Any,
    knowledge_id: str,
    snapshot: dict | None,
    current_fingerprint: str,
    base_map: dict[str, dict],
    user: UserModel,
    db: AsyncSession,
    group_ids: set[str],
) -> dict:
    base = base_map.get(knowledge_id)
    base_exists = bool(base)
    if base:
        base_exists = True
        name = base.get('name')
        external = bool(base.get('external'))
        can_write = bool(base.get('write_access')) and not external
    else:
        # The mapping might point at a KB that is no longer visible; ask the DB
        # directly so stale mappings can still be repaired or removed.
        knowledge = await Knowledges.get_knowledge_by_id(knowledge_id, db=db)
        base_exists = bool(knowledge)
        name = getattr(knowledge, 'name', None)
        external = bool(knowledge and is_external_knowledge(knowledge))
        can_write = bool(
            knowledge and not external and await _knowledge_access(knowledge, user, 'write', db, group_ids)
        )

    file_id = snapshot.get('file_id') if isinstance(snapshot, dict) else None
    fingerprint = snapshot.get('fingerprint') if isinstance(snapshot, dict) else None
    updated_at = snapshot.get('updated_at') if isinstance(snapshot, dict) else None
    file = await Files.get_file_by_id(file_id, db=db) if file_id else None
    linked = bool(file_id and await Knowledges.has_file(knowledge_id, file_id, db=db))
    index_state, index_error = ('unknown', None)
    if file:
        index_state, index_error = _file_index_state(file, knowledge_id)

    entry = {
        'knowledge_id': knowledge_id,
        'name': name or (snapshot.get('name') if isinstance(snapshot, dict) else None),
        'file_id': file_id,
        'fingerprint': fingerprint,
        'updated_at': updated_at,
        'index_state': index_state,
        'index_error': index_error,
        'can_write': can_write,
        'exists': base_exists,
        'external': external,
    }
    if not snapshot:
        entry['state'] = 'new'
        return entry

    flags = {
        'kb_exists': base_exists,
        'can_write': can_write,
        'external': external,
        'file_exists': bool(file),
        'linked': linked,
        'fingerprint_matches': fingerprint == current_fingerprint,
        'index_failed': index_state == 'failed',
        'index_pending': index_state == 'pending',
    }
    entry['state'] = classify_snapshot_state(flags)
    return entry


async def _note_readiness(request: Request, note_id: str, user: UserModel, db: AsyncSession) -> dict:
    note = await _load_note(note_id, user, db, 'read')
    group_ids = await _user_group_ids(user, db)
    loop = await _workspace_loop_readiness(request, user, db)
    title = canonical_note_title(note.title)
    content = canonical_note_content(note.data)
    current_fingerprint = note_snapshot_fingerprint(title, content)
    can_write_note = await _note_access(note, user, 'write', db, group_ids)

    base_map = {base['id']: base for base in loop['knowledge_bases']}
    mappings = (getattr(note, 'meta', None) or {}).get('note_knowledge') or {}
    mappings = mappings if isinstance(mappings, dict) else {}
    snapshots: list[dict] = []
    seen: set[str] = set()
    for knowledge_id, snapshot in mappings.items():
        if not isinstance(knowledge_id, str):
            continue
        seen.add(knowledge_id)
        snapshots.append(
            await _snapshot_entry(
                note,
                knowledge_id,
                snapshot if isinstance(snapshot, dict) else None,
                current_fingerprint,
                base_map,
                user,
                db,
                group_ids,
            )
        )
    # Writable bases with no mapping are still part of the loop state; the UI
    # shows them as "new" instead of silently hiding them.
    for base in loop['knowledge_bases']:
        if not base['write_access'] or base['id'] in seen:
            continue
        snapshots.append(
            await _snapshot_entry(note, base['id'], None, current_fingerprint, base_map, user, db, group_ids)
        )

    ready = loop['ready'] and can_write_note
    reason_code = loop['reason_code']
    if not can_write_note:
        reason_code = 'note_read_only'
    return {
        **loop,
        'ready': ready,
        'reason_code': reason_code,
        'note': {'id': note.id, 'title': note.title, 'write_access': bool(can_write_note)},
        'current_fingerprint': current_fingerprint,
        'snapshots': snapshots,
    }


async def _write_file_index_state(
    file_id: str,
    knowledge_id: str,
    state: str,
    error: str | None,
    db: AsyncSession,
    *,
    content: str | None = None,
    file_hash: str | None = None,
    filename: str | None = None,
    file_path: str | None = None,
) -> None:
    row = await db.get(FileTable, file_id)
    if row is None:
        return
    data = dict(row.data or {})
    meta = dict(row.meta or {})
    if content is not None:
        data['content'] = content
    data['status'] = state
    data['error'] = error[:500] if error else None
    meta['status'] = state
    meta['error'] = error[:500] if error else None
    index_state = dict(meta.get('index_state') or {})
    index_state[knowledge_id] = {
        'state': state,
        'error': error[:500] if error else None,
        'updated_at': int(time.time() * 1000),
        'collection_name': knowledge_id,
    }
    meta['index_state'] = index_state
    values: dict[str, Any] = {
        'data': data,
        'meta': meta,
        'updated_at': int(time.time()),
    }
    if file_hash is not None:
        values['hash'] = file_hash
    if filename is not None:
        values['filename'] = filename
    if file_path is not None:
        values['path'] = file_path
    await db.execute(update(FileTable).where(FileTable.id == file_id).values(**values))
    await db.commit()


async def _write_note_snapshot(note_id: str, knowledge_id: str, snapshot: dict, db: AsyncSession) -> dict:
    row = await db.get(NoteTable, note_id)
    if row is None:
        raise structured_loop_error(
            'note_not_found', 'metadata', 'Note disappeared before metadata write.', False, status_code=404
        )
    merged = merge_note_knowledge_snapshot(row.meta, knowledge_id, snapshot)
    await db.execute(
        update(NoteTable).where(NoteTable.id == note_id).values(meta=merged, updated_at=int(time.time_ns()))
    )
    await db.commit()
    return merged


async def _create_snapshot_file(
    user: UserModel, knowledge_id: str, note: Any, title: str, content: str, db: AsyncSession
) -> tuple[Any, str]:
    file_id = str(uuid4())
    filename = safe_markdown_filename(title)
    storage_filename = f'{file_id}-{filename}'
    contents = content.encode('utf-8')
    file_hash = hashlib.sha256(contents).hexdigest()
    tags = {'OpenWebUI-File-Id': file_id, 'OpenWebUI-File-Name': filename}
    try:
        _, file_path = await asyncio.to_thread(Storage.upload_file, io.BytesIO(contents), storage_filename, tags)
    except Exception as exc:
        raise structured_loop_error('upload_failed', 'upload', str(exc), True, status_code=500)

    meta = {
        'name': filename,
        'content_type': 'text/markdown',
        'size': len(contents),
        'file_hash': file_hash,
        'data': {},
        'source': 'note_knowledge',
        'note_id': note.id,
        'collection_name': knowledge_id,
        'status': 'pending',
    }
    file_item = await Files.insert_new_file(
        user.id,
        FileForm(
            id=file_id,
            hash=file_hash,
            filename=filename,
            path=file_path,
            data={'content': content, 'status': 'pending'},
            meta=meta,
        ),
        db=db,
    )
    if not file_item:
        try:
            await asyncio.to_thread(Storage.delete_file, file_path)
        except Exception:
            pass
        raise structured_loop_error(
            'upload_failed', 'upload', 'Could not register the snapshot file.', True, status_code=500
        )

    linked = await Knowledges.add_file_to_knowledge_by_id(
        knowledge_id=knowledge_id,
        file_id=file_id,
        user_id=user.id,
        directory_id=None,
        db=db,
    )
    if not linked:
        try:
            await asyncio.to_thread(Storage.delete_file, file_path)
        except Exception:
            pass
        await Files.delete_file_by_id(file_id, db=db)
        raise structured_loop_error(
            'file_link_failed', 'attach', 'Could not link the snapshot file to the knowledge base.', True, file_id, 500
        )
    return file_item, 'not_needed'


async def _sync_note_to_knowledge(  # noqa: C901
    request: Request,
    note_id: str,
    knowledge_id: str,
    force: bool,
    user: UserModel,
    db: AsyncSession,
) -> dict:
    await check_notes_access(user, db=db)
    note = await _load_note(note_id, user, db, 'write')
    await _load_writable_knowledge(knowledge_id, user, db)
    embedding = await _embedding_readiness(request)
    if not embedding['ready']:
        raise structured_loop_error(
            embedding['reason_code'] or 'embedding_not_configured',
            'preflight',
            'Knowledge indexing is not configured.',
            True,
            status_code=409,
        )
    await _ensure_embedding_function(request, embedding)

    title = canonical_note_title(note.title)
    content = canonical_note_content(note.data)
    fingerprint = note_snapshot_fingerprint(title, content)
    filename = safe_markdown_filename(title)
    updated_at = int(time.time() * 1000)

    meta = dict(getattr(note, 'meta', None) or {})
    mappings = dict(meta.get('note_knowledge') or {}) if isinstance(meta.get('note_knowledge'), dict) else {}
    snapshot = dict(mappings.get(knowledge_id) or {}) if isinstance(mappings.get(knowledge_id), dict) else {}
    file_id = snapshot.get('file_id')
    file = await Files.get_file_by_id(file_id, db=db) if file_id else None
    linked = bool(file_id and await Knowledges.has_file(knowledge_id, file_id, db=db))

    if not force and snapshot.get('state') == 'synced':
        state = await _note_readiness(request, note_id, user, db)
        entry = next((item for item in state['snapshots'] if item.get('knowledge_id') == knowledge_id), None)
        if entry and entry.get('state') == 'synced':
            return {
                'ok': True,
                'action': 'noop',
                'state': 'synced',
                'note_id': note_id,
                'knowledge_id': knowledge_id,
                'file_id': file_id,
                'fingerprint': fingerprint,
                'updated_at': snapshot.get('updated_at') or updated_at,
                'index_state': entry.get('index_state') or 'completed',
                'index_error': None,
                'cleanup_status': 'not_needed',
                'message': 'Snapshot already up to date.',
            }

    created = False
    repaired = False
    cleanup_status = 'not_needed'
    if not file:
        if file_id:
            repaired = True
        file, cleanup_status = await _create_snapshot_file(user, knowledge_id, note, title, content, db)
        file_id = file.id
        created = True
    elif not linked:
        linked = await Knowledges.add_file_to_knowledge_by_id(
            knowledge_id=knowledge_id,
            file_id=file_id,
            user_id=user.id,
            directory_id=None,
            db=db,
        )
        if not linked:
            raise structured_loop_error(
                'file_link_failed', 'attach', 'Could not restore the knowledge base link.', True, file_id, 500
            )
        repaired = True

    contents = content.encode('utf-8')
    file_hash = hashlib.sha256(contents).hexdigest()
    storage_filename = f'{file_id}-{filename}'
    file_path = getattr(file, 'path', None)
    try:
        _, new_path = await asyncio.to_thread(
            Storage.upload_file, io.BytesIO(contents), storage_filename, {'OpenWebUI-File-Id': file_id}
        )
        if new_path and new_path != file_path:
            file_path = new_path
    except Exception as exc:
        log.warning('Could not refresh stored snapshot bytes for %s: %s', file_id, exc)

    await _write_file_index_state(
        file_id,
        knowledge_id,
        'pending',
        None,
        db,
        content=content,
        file_hash=file_hash,
        filename=filename,
        file_path=file_path,
    )
    try:
        await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=knowledge_id, filter={'file_id': file_id})
    except Exception as exc:
        log.debug('Old snapshot vectors were not deleted for %s: %s', file_id, exc)

    try:
        await process_file(
            request,
            ProcessFileForm(file_id=file_id, content=content, collection_name=knowledge_id),
            user=user,
            db=db,
        )
    except HTTPException as exc:
        error = str(exc.detail)
        await _write_file_index_state(file_id, knowledge_id, 'failed', error, db)
        raise structured_loop_error('index_failed', 'index', error, True, file_id, exc.status_code or 500)
    except Exception as exc:
        await _write_file_index_state(file_id, knowledge_id, 'failed', str(exc), db)
        raise structured_loop_error('index_failed', 'index', str(exc), True, file_id, 500)

    await _write_file_index_state(file_id, knowledge_id, 'completed', None, db)
    new_snapshot = {
        'file_id': file_id,
        'fingerprint': fingerprint,
        'updated_at': updated_at,
        'index_state': 'completed',
        'index_error': None,
        'state': 'synced',
    }
    await _write_note_snapshot(note_id, knowledge_id, new_snapshot, db)
    action = 'created' if created else 'repaired' if repaired else 'updated'
    return {
        'ok': True,
        'action': action,
        'state': 'synced',
        'note_id': note_id,
        'knowledge_id': knowledge_id,
        'file_id': file_id,
        'fingerprint': fingerprint,
        'updated_at': updated_at,
        'index_state': 'completed',
        'index_error': None,
        'cleanup_status': cleanup_status,
        'message': 'Snapshot synchronized.',
    }


@router.get('/loop/readiness')
async def get_workspace_loop_readiness(
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await _workspace_loop_readiness(request, user, db)


@router.get('/{id}/knowledge/readiness')
async def get_note_knowledge_readiness(
    id: str,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await _note_readiness(request, id, user, db)


@router.post('/{id}/knowledge/{knowledge_id}/sync')
async def sync_note_knowledge(
    id: str,
    knowledge_id: str,
    form_data: WorkspaceLoopSyncForm,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    return await _sync_note_to_knowledge(request, id, knowledge_id, bool(form_data.force), user, db)


@router.delete('/{id}/knowledge/{knowledge_id}/snapshot')
async def delete_note_knowledge_snapshot(
    id: str,
    knowledge_id: str,
    remove_file: bool = False,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await check_notes_access(user, db=db)
    note = await _load_note(id, user, db, 'write')
    row = await db.get(NoteTable, note.id)
    meta = dict(row.meta or {}) if row else {}
    mappings = dict(meta.get('note_knowledge') or {}) if isinstance(meta.get('note_knowledge'), dict) else {}
    snapshot = mappings.pop(knowledge_id, None) or {}
    file_id = snapshot.get('file_id') if isinstance(snapshot, dict) else None
    meta['note_knowledge'] = mappings
    if row:
        await db.execute(
            update(NoteTable).where(NoteTable.id == note.id).values(meta=meta, updated_at=int(time.time_ns()))
        )
        await db.commit()

    removed_file = False
    if remove_file and file_id:
        knowledge = await Knowledges.get_knowledge_by_id(knowledge_id, db=db)
        if knowledge and is_external_knowledge(knowledge):
            raise structured_loop_error(
                'external_knowledge_read_only',
                'cleanup',
                'External knowledge bases are read-only.',
                False,
                status_code=400,
            )
        if knowledge and await _knowledge_access(knowledge, user, 'write', db):
            removed_file = bool(
                await Knowledges.remove_file_from_knowledge_by_id(knowledge_id=knowledge_id, file_id=file_id, db=db)
            )
            try:
                await ASYNC_VECTOR_DB_CLIENT.delete(collection_name=knowledge_id, filter={'file_id': file_id})
            except Exception as exc:
                log.debug('Could not delete vectors for removed snapshot %s: %s', file_id, exc)
    return {
        'ok': True,
        'removed_mapping': True,
        'removed_file': removed_file,
        'file_id': file_id,
    }


@router.post('/from-chat')
async def create_note_from_chat(
    form_data: NoteFromChatForm,
    request: Request,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await check_notes_access(user, db=db)
    chat = await Chats.get_accessible_chat_by_id(form_data.chat_id, user, db=db, permission='read')
    content = extract_chat_message_content(chat, form_data.message_id) if chat else ''
    if not content:
        raise structured_loop_error(
            'message_not_found', 'preflight', 'Chat message not found or empty.', False, status_code=404
        )
    title = (
        (form_data.title or '').strip() or content.strip().splitlines()[0][:80] or f'Chat {form_data.message_id[:8]}'
    )
    note = await Notes.insert_new_note(
        user.id,
        NoteForm(
            title=title,
            data={'content': {'md': content}},
            meta={
                'source': 'chat',
                'source_chat_id': form_data.chat_id,
                'source_message_id': form_data.message_id,
            },
        ),
        db=db,
    )
    if not note:
        raise structured_loop_error('note_create_failed', 'metadata', 'Could not create note.', True, status_code=500)

    snapshot = None
    if form_data.knowledge_id:
        try:
            snapshot = await _sync_note_to_knowledge(request, note.id, form_data.knowledge_id, True, user, db)
        except HTTPException as exc:
            snapshot = {
                'ok': False,
                'state': 'failed',
                'note_id': note.id,
                'knowledge_id': form_data.knowledge_id,
                'error': exc.detail,
            }
    return {'note': note.model_dump(), 'snapshot': snapshot}
