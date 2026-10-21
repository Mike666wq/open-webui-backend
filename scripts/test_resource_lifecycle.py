#!/usr/bin/env python3
"""Exercise real routes against a throwaway database; never contacts a server."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

from dev_backend import REPO_ROOT, configuration


async def verify():
    import httpx
    from open_webui.main import app
    from open_webui.internal.db import get_async_db
    from open_webui.models.resources import ResourceEntry
    from open_webui.utils.resource_visibility import install_resource_visibility
    from sqlalchemy import select
    install_resource_visibility()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://resource-test') as client:
        async def call(method, path, body=None, token=None, expected=200):
            res = await client.request(method, path, json=body, headers={'Authorization': f'Bearer {token}'} if token else {})
            assert res.status_code == expected, (method, path, res.status_code, res.text[:1500])
            data = res.json()
            if method == 'POST' and (path == '/api/v1/resources/tasks' or path.endswith('/retry')) and res.status_code == 200:
                # ASGITransport completes background tasks before returning.
                rows = await client.get('/api/v1/resources/tasks', headers={'Authorization': f'Bearer {token}'})
                data = next(item for item in rows.json() if item['id'] == data['id'])
            return data

        account = await call('POST', '/api/v1/auths/signup', {
            'name': 'Resource Test', 'email': 'resource@example.invalid', 'password': secrets.token_urlsafe(20),
        })
        token = account['token']
        chat = await call('POST', '/api/v1/chats/new', {'chat': {
            'title': 'Lifecycle synthetic chat',
            'history': {'messages': {'q': {'id': 'q', 'role': 'user', 'content': 'A body that must survive recovery',
                                          'parentId': None, 'childrenIds': []}}, 'currentId': 'q'},
        }}, token)
        chat_id = chat['id']
        preview = await call('POST', '/api/v1/resources/preflight', {
            'action': 'recycle', 'items': [{'type': 'chat', 'id': chat_id}],
        }, token)
        assert preview['allowed'] == 1, preview
        executed = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': preview['id'], 'idempotency_key': 'synthetic-delete-1',
        }, token)
        assert executed['state'] == 'completed', executed
        assert executed['results'][0]['success'], executed
        repeat = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': preview['id'], 'idempotency_key': 'synthetic-delete-1',
        }, token)
        assert repeat['id'] == executed['id']
        await call('GET', f'/api/v1/chats/{chat_id}', token=token, expected=401)
        listing = await call('GET', '/api/v1/resources/list?type=chat', token=token)
        assert listing['total'] == 0, listing
        recycled = await call('GET', '/api/v1/resources/recycle-bin', token=token)
        assert len(recycled) == 1 and recycled[0]['original_id'] == chat_id
        # The old APK sends recycle-item IDs. Verify that compatibility path,
        # then read the full original body rather than trusting a success toast.
        restored = await call('POST', '/api/v1/resources/restore', {
            'ids': [recycled[0]['id']], 'type': 'chat',
        }, token)
        assert restored['restored'] == 1, restored
        original = await call('GET', f'/api/v1/chats/{chat_id}', token=token)
        assert original['chat']['history']['messages']['q']['content'] == 'A body that must survive recovery', original
        # Established DELETE routes must now recycle rather than lose data.
        await call('DELETE', f'/api/v1/chats/{chat_id}', token=token)
        recycled = await call('GET', '/api/v1/resources/recycle-bin', token=token)
        assert len(recycled) == 1
        await call('POST', '/api/v1/resources/restore', {'ids': [recycled[0]['id']], 'type': 'chat'}, token)
        await call('GET', f'/api/v1/chats/{chat_id}', token=token)
        passed = ['body_restore', 'legacy_recycle_id', 'idempotency', 'old_delete_route',
                  'list_exclusion', 'deep_link_exclusion']

        async def preview(action, kind, rid, as_token=token, options=None):
            return await call('POST', '/api/v1/resources/preflight', {
                'action': action, 'items': [{'type': kind, 'id': rid}], 'options': options or {},
            }, as_token)

        async def act(action, kind, rid, as_token=token, options=None):
            pre = await preview(action, kind, rid, as_token, options)
            assert pre['allowed'] == 1, pre
            job = await call('POST', '/api/v1/resources/tasks', {
                'preview_id': pre['id'], 'idempotency_key': secrets.token_hex(12),
            }, as_token)
            assert job['state'] == 'completed', job
            return job

        from open_webui.models.config import Config
        await Config.upsert({'ui.enable_signup': True})
        # A second synthetic account exercises the real HTTP authorization path.
        other = await call('POST', '/api/v1/auths/signup', {
            'name': 'Other Resource Test', 'email': 'other@example.invalid',
            'password': secrets.token_urlsafe(20),
        })
        from open_webui.models.users import Users
        await Users.update_user_by_id(other['id'], {'role': 'user'})
        blocked = await preview('recycle', 'chat', chat_id, other['token'])
        assert blocked['allowed'] == 0, blocked
        await call('POST', '/api/v1/resources/restore', {'ids': ['unknown-entry'], 'type': 'chat'}, token, 409)
        passed += ['cross_account_rejected', 'zero_success_is_error']

        # A preflight is invalidated by an intervening content edit.
        pre = await preview('recycle', 'chat', chat_id)
        from open_webui.models.chats import Chats, Chat, ChatFile
        async with get_async_db() as db:
            row = await db.get(Chat, chat_id)
            row.title = 'Changed after preview'
            await db.commit()
        job = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': pre['id'], 'idempotency_key': secrets.token_hex(12),
        }, token)
        assert job['state'] == 'failed' and not job['results'][0]['success'], job
        assert job['results'][0]['reason'] == 'resource_changed_preview_again', job
        await call('POST', f'/api/v1/chats/{chat_id}/pin', {}, token)
        pre = await preview('recycle', 'chat', chat_id)
        assert 'pinned' in pre['items'][0]['reasons'], pre
        await call('POST', f'/api/v1/chats/{chat_id}/pin', {}, token)
        passed += ['stale_preflight_rejected', 'pinned_protected']

        # Public links remain suspended even after the original body is restored.
        shared = await call('POST', f'/api/v1/chats/{chat_id}/share', {}, token)
        sid = shared['share_id']
        await call('GET', f'/api/v1/chats/share/{sid}', token=token)
        pre = await preview('recycle', 'chat', chat_id)
        assert 'shared_confirmation_required' in pre['items'][0]['reasons'], pre
        await act('recycle', 'chat', chat_id, options={'include_shared': True})
        await call('GET', f'/api/v1/chats/share/{sid}', token=token, expected=401)
        await act('restore', 'chat', chat_id)
        await call('GET', f'/api/v1/chats/share/{sid}', token=token, expected=401)
        assert (await call('GET', f'/api/v1/chats/{chat_id}', token=token))['share_id'] is None
        passed += ['shared_confirmation', 'share_stays_paused_after_restore']

        # Real files and Chroma vectors, with injected object-store failure.
        import io
        from open_webui.models.files import Files, FileForm, File
        from open_webui.storage.provider import Storage
        from open_webui.retrieval.vector.factory import get_vector_db_client
        from open_webui.retrieval.vector.main import VectorItem
        from open_webui.utils.resource_visibility import including_recycled
        from unittest.mock import patch
        from open_webui.utils import resource_service as service
        file_id = secrets.token_hex(16)
        content, path = Storage.upload_file(io.BytesIO(b'synthetic attachment body'), file_id + '.txt', {})
        file = await Files.insert_new_file(account['id'], FileForm(
            id=file_id, filename='Resource attachment.txt', path=path,
            data={'status': 'completed'}, meta={'size': len(content)},
        ))
        get_vector_db_client().insert('file-' + file_id, [VectorItem(
            id='chunk-1', text='synthetic vector body', vector=[0.1, 0.2, 0.3], metadata={'file_id': file_id},
        ).model_dump()])
        async with get_async_db() as db:
            db.add(ChatFile(id=secrets.token_hex(16), user_id=account['id'], chat_id=chat_id,
                            file_id=file_id, created_at=service.now(), updated_at=service.now()))
            await db.commit()
        pre = await preview('recycle', 'file', file_id)
        assert 'referenced' in pre['items'][0]['reasons'], pre
        # The old URL cannot bypass the same attachment protection.
        await call('DELETE', '/api/v1/files/' + file_id, token=token, expected=409)
        await act('recycle', 'chat', chat_id)
        assert Path(path).read_bytes() == content
        await act('restore', 'chat', chat_id)
        async with get_async_db() as db:
            assert await db.scalar(select(ChatFile.id).where(ChatFile.chat_id == chat_id, ChatFile.file_id == file_id))
        assert (await call('GET', f'/api/v1/chats/{chat_id}', token=token))['chat']['history']['messages']['q']['content'] == 'A body that must survive recovery'
        passed += ['shared_attachment_protected', 'old_file_delete_protected', 'attachment_relation_restored']
        await act('recycle', 'chat', chat_id)
        await act('purge', 'chat', chat_id)
        async with get_async_db() as db:
            entry = await db.scalar(select(ResourceEntry).where(ResourceEntry.type == 'file', ResourceEntry.resource_id == file_id))
            assert entry and entry.state == 'recycled'
            entry_id = entry.id
        pre = await preview('purge', 'file', entry_id)
        with patch.object(Storage, 'delete_file', side_effect=OSError('synthetic storage fault')):
            failed = await call('POST', '/api/v1/resources/tasks', {
                'preview_id': pre['id'], 'idempotency_key': secrets.token_hex(12),
            }, token)
        assert failed['state'] == 'failed', failed
        assert Path(path).exists()
        with including_recycled():
            assert not get_vector_db_client().has_collection('file-' + file_id)
        retried = await call('POST', f"/api/v1/resources/tasks/{failed['id']}/retry", {}, token)
        assert retried['state'] == 'completed', retried
        assert not Path(path).exists()
        with including_recycled():
            async with get_async_db() as db:
                assert await db.get(File, file_id) is None
        passed += ['parent_then_attachment_purge', 'real_vector_removed', 'object_failure_retains_record', 'checkpoint_retry_removes_object']

        note = await call('POST', '/api/v1/notes/create', {
            'title': 'Synthetic note', 'data': {'content': {'md': 'Note body and **formatting**'}},
        }, token)
        await call('DELETE', f"/api/v1/notes/{note['id']}/delete", token=token)
        await act('restore', 'note', note['id'])
        restored_note = await call('GET', f"/api/v1/notes/{note['id']}", token=token)
        assert restored_note['data']['content']['md'] == 'Note body and **formatting**', restored_note
        knowledge = await call('POST', '/api/v1/knowledge/create', {'name': 'Synthetic knowledge', 'description': 'retain knowledge description'}, token)
        await call('DELETE', f"/api/v1/knowledge/{knowledge['id']}/delete", token=token)
        await act('restore', 'knowledge', knowledge['id'])
        restored_knowledge = await call('GET', f"/api/v1/knowledge/{knowledge['id']}", token=token)
        assert restored_knowledge['description'] == 'retain knowledge description', restored_knowledge
        passed += ['note_body_restore', 'knowledge_body_restore']

        from unittest.mock import AsyncMock
        from open_webui.socket import main as socket_main
        from open_webui.utils.resource_visibility import viewer
        # A socket authenticated as this user keeps the context for the whole
        # handler, and resets it before handling another user's request.
        socket_user = {'id': account['id'], 'role': 'admin', 'name': 'Synthetic'}
        async def accessible_chat(*args, **kwargs):
            assert viewer.get() == account['id']
            return None
        with patch.object(socket_main, 'get_socket_session_user', AsyncMock(return_value=socket_user)), \
             patch.object(socket_main.Users, 'get_user_by_id', AsyncMock(return_value=socket_user)), \
             patch.object(socket_main.Chats, 'get_accessible_chat_by_id', side_effect=accessible_chat) as check_chat:
            await socket_main.chat_events('synthetic-sid', {'chat_id': secrets.token_hex(16), 'data': {'type': 'join'}})
            assert check_chat.await_count == 1
            assert viewer.get() is None
        passed += ['socket_chat_viewer_scope']

        await act('recycle', 'note', note['id'])
        from open_webui.models.notes import Notes
        with patch.object(socket_main, 'get_socket_session_user', AsyncMock(return_value=socket_user)), \
             patch.object(socket_main.YDOC_MANAGER, 'get_updates', AsyncMock()) as updates, \
             patch.object(socket_main.YDOC_MANAGER, 'append_to_updates', AsyncMock()) as append, \
             patch.object(socket_main.sio, 'emit', AsyncMock()) as emit, \
             patch.object(Notes, 'update_note_by_id', AsyncMock()) as save:
            payload = {'document_id': 'note_' + note['id'], 'update': [1], 'data': {'content': 'stale edit'}}
            await socket_main.ydoc_document_join('synthetic-sid', payload)
            await socket_main.yjs_document_state('synthetic-sid', payload)
            await socket_main.yjs_document_update('synthetic-sid', payload)
            await socket_main.yjs_awareness_update('synthetic-sid', payload)
            await socket_main.document_save_handler(payload['document_id'], payload['data'], socket_user)
            updates.assert_not_awaited()
            append.assert_not_awaited()
            emit.assert_not_awaited()
            save.assert_not_awaited()
        await act('restore', 'note', note['id'])
        restored_note = await call('GET', f"/api/v1/notes/{note['id']}", token=token)
        assert restored_note['data']['content']['md'] == 'Note body and **formatting**'
        passed += ['recycled_note_socket_read_write_rejected', 'pending_note_save_preserves_body']

        # Real vector metadata and a current model reference survive recycle /
        # restore. A failed purge retains the row and can resume on retry.
        from open_webui.models.models import Model
        from open_webui.models.knowledge import Knowledge
        from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
        purge_kb = await call('POST', '/api/v1/knowledge/create', {'name': 'Purge metadata', 'description': 'synthetic'}, token)
        model_id = 'resource-test-model'
        async with get_async_db() as db:
            db.add(Model(id=model_id, user_id=account['id'], name='Synthetic model', params={},
                         meta={'knowledge': [{'id': purge_kb['id']}, {'id': knowledge['id']}], 'other': 'keep'},
                         created_at=service.now(), updated_at=service.now()))
            await db.commit()
        get_vector_db_client().insert('knowledge-bases', [VectorItem(
            id=purge_kb['id'], text='Synthetic metadata', vector=[0.1, 0.2, 0.3],
            metadata={'knowledge_base_id': purge_kb['id']},
        ).model_dump()])
        await act('recycle', 'knowledge', purge_kb['id'])
        assert not get_vector_db_client().get('knowledge-bases').ids[0]
        await act('restore', 'knowledge', purge_kb['id'])
        assert purge_kb['id'] in get_vector_db_client().get('knowledge-bases').ids[0]
        await act('recycle', 'knowledge', purge_kb['id'])
        pre = await preview('purge', 'knowledge', purge_kb['id'])
        with patch.object(ASYNC_VECTOR_DB_CLIENT, 'delete', AsyncMock(side_effect=OSError('synthetic metadata fault'))):
            failed = await call('POST', '/api/v1/resources/tasks', {
                'preview_id': pre['id'], 'idempotency_key': secrets.token_hex(12),
            }, token)
        assert failed['state'] == 'failed', failed
        with including_recycled():
            async with get_async_db() as db:
                assert await db.get(Knowledge, purge_kb['id'])
                assert (await db.get(Model, model_id)).meta['knowledge'][0]['id'] == purge_kb['id']
        retried = await call('POST', f"/api/v1/resources/tasks/{failed['id']}/retry", {}, token)
        assert retried['state'] == 'completed', retried
        async with get_async_db() as db:
            model = await db.get(Model, model_id)
            assert model.meta == {'knowledge': [{'id': knowledge['id']}], 'other': 'keep'}, model.meta
        with including_recycled():
            assert purge_kb['id'] not in get_vector_db_client().get('knowledge-bases').ids[0]
        passed += ['knowledge_metadata_hidden_and_restored', 'knowledge_purge_failure_retry', 'knowledge_model_reference_cleanup']


        # Removing a knowledge attachment or folder follows the same durable
        # lifecycle. Relation rows, original rows and nested locations survive
        # restore instead of being detached before the preview.
        from open_webui.models.knowledge import KnowledgeDirectory, KnowledgeFile
        from open_webui.models.files import File
        knowledge_file_id = secrets.token_hex(16)
        knowledge_file_content, knowledge_file_path = Storage.upload_file(
            io.BytesIO(b'knowledge relation body'), knowledge_file_id + '.txt', {}
        )
        knowledge_file = await Files.insert_new_file(account['id'], FileForm(
            id=knowledge_file_id, filename='Knowledge restore.txt', path=knowledge_file_path,
            data={'status': 'completed'}, meta={'size': len(knowledge_file_content)},
        ))
        root_dir = KnowledgeDirectory(
            id=str(secrets.token_hex(8)), knowledge_id=knowledge['id'], parent_id=None,
            name='Synthetic root', user_id=account['id'], created_at=service.now(), updated_at=service.now(),
        )
        child_dir = KnowledgeDirectory(
            id=str(secrets.token_hex(8)), knowledge_id=knowledge['id'], parent_id=root_dir.id,
            name='Synthetic child', user_id=account['id'], created_at=service.now(), updated_at=service.now(),
        )
        async with get_async_db() as db:
            db.add_all([root_dir, child_dir])
            await db.flush()
            link = KnowledgeFile(
                id=str(secrets.token_hex(16)), knowledge_id=knowledge['id'], file_id=knowledge_file.id,
                directory_id=child_dir.id, user_id=account['id'],
                created_at=service.now(), updated_at=service.now(),
            )
            db.add(link)
            await db.commit()
            link_id = link.id
        job = await act('recycle', 'knowledge_directory', root_dir.id)
        assert job['state'] == 'completed', job
        with including_recycled():
            async with get_async_db() as db:
                entry = await db.scalar(select(ResourceEntry).where(
                    ResourceEntry.type == 'knowledge_file', ResourceEntry.resource_id == link_id,
                ))
                assert entry and entry.state == 'recycled'
        job = await act('restore', 'knowledge_directory', root_dir.id)
        assert job['state'] == 'completed', job
        with including_recycled():
            async with get_async_db() as db:
                assert await db.get(KnowledgeDirectory, child_dir.id)
                restored_link = await db.get(KnowledgeFile, link_id)
                assert restored_link and restored_link.directory_id == child_dir.id
                assert await db.get(File, knowledge_file_id)
        passed += ['knowledge_folder_relation_restore']

        # Old workspace operations may use a write grant; personal management
        # cannot acquire that privilege through a forged request option.
        from copy import deepcopy
        from open_webui.models.access_grants import AccessGrant
        old_permissions = await Config.get('user.permissions')
        permissions = deepcopy(old_permissions or {})
        permissions.setdefault('workspace', {})['knowledge'] = True
        await Config.upsert({'user.permissions': permissions})
        collaborator = await Users.get_user_by_id(other['id'])
        grant_id = secrets.token_hex(16)
        async with get_async_db() as db:
            db.add(AccessGrant(id=grant_id, resource_type='knowledge', resource_id=knowledge['id'],
                               principal_type='user', principal_id=other['id'], permission='write', created_at=service.now()))
            await db.commit()
        denied = await preview('recycle', 'knowledge', knowledge['id'], other['token'], {'include_shared': True})
        assert denied['allowed'] == 0, denied
        await call('POST', '/api/v1/resources/preflight', {
            'action': 'recycle', 'items': [{'type': 'knowledge', 'id': knowledge['id']}],
            'options': {'knowledge_write': True},
        }, other['token'], 422)
        async with get_async_db() as db:
            granted = await service.make_preview(db, collaborator, 'recycle',
                [{'type': 'knowledge', 'id': knowledge['id']}], {'include_shared': True}, knowledge_write=True)
            assert granted['allowed'] == 1, granted
            from sqlalchemy import delete
            await db.execute(delete(AccessGrant).where(AccessGrant.id == grant_id))
            await db.commit()
        revoked = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': granted['id'], 'idempotency_key': secrets.token_hex(12),
        }, other['token'])
        assert revoked['state'] == 'failed', revoked
        async with get_async_db() as db:
            db.add(AccessGrant(id=grant_id, resource_type='knowledge', resource_id=knowledge['id'],
                               principal_type='user', principal_id=other['id'], permission='write', created_at=service.now()))
            await db.commit()
            granted = await service.make_preview(db, collaborator, 'recycle',
                [{'type': 'knowledge', 'id': knowledge['id']}], {'include_shared': True}, knowledge_write=True)
        completed = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': granted['id'], 'idempotency_key': secrets.token_hex(12),
        }, other['token'])
        assert completed['state'] == 'completed', completed
        await act('restore', 'knowledge', knowledge['id'], other['token'])
        restored = await call('GET', f"/api/v1/knowledge/{knowledge['id']}", token=token)
        assert restored['description'] == 'retain knowledge description', restored
        async with get_async_db() as db:
            await db.execute(delete(AccessGrant).where(AccessGrant.id == grant_id))
            await db.commit()
        passed += ['workspace_write_grant_recycle_restore']
        await Config.upsert({'user.permissions': old_permissions})
        passed += ['workspace_write_grant_preview', 'revoked_write_grant_rejected', 'personal_page_cannot_escalate_knowledge_access']


        # An external notification failure cannot turn a committed mutation
        # into a false failure; the durable outbox retries the event only.
        event_chat = await call('POST', '/api/v1/chats/new', {'chat': {
            'title': 'Synthetic event outbox',
            'history': {'messages': {}, 'currentId': None},
        }}, token)
        pre = await preview('recycle', 'chat', event_chat['id'])
        with patch.object(service, 'resource_changed', side_effect=RuntimeError('synthetic socket outage')):
            outbox_job = await call('POST', '/api/v1/resources/tasks', {
                'preview_id': pre['id'], 'idempotency_key': secrets.token_hex(12),
            }, token)
        assert outbox_job['state'] == 'completed' and outbox_job['results'][0]['event_pending'], outbox_job
        await service.maintenance_tick()
        jobs = await call('GET', '/api/v1/resources/tasks', token=token)
        emitted = next(item for item in jobs if item['id'] == outbox_job['id'])
        assert not emitted['results'][0].get('event_pending'), emitted
        passed += ['postcommit_event_failure_is_not_operation_failure']

        # Expired recycle entries become durable purge jobs exactly once.
        expiry_chat = await call('POST', '/api/v1/chats/new', {'chat': {
            'title': 'Synthetic expired body',
            'history': {'messages': {}, 'currentId': None},
        }}, token)
        await act('recycle', 'chat', expiry_chat['id'])
        async with get_async_db() as db:
            entry = await db.scalar(select(ResourceEntry).where(
                ResourceEntry.type == 'chat', ResourceEntry.resource_id == expiry_chat['id'],
            ))
            entry.expires_at = service.now() - 1
            await db.commit()
        await service.maintenance_tick()
        with including_recycled():
            async with get_async_db() as db:
                from open_webui.models.chats import Chat
                assert await db.get(Chat, expiry_chat['id']) is None
        passed += ['expiry_purge']

        # Personal boundaries are server-side, independent of browser sessions.
        from open_webui.models.config import Config
        await Config.upsert({'channels.enable': True})
        channel = await call('POST', '/api/v1/channels/create', {
            'type': 'group', 'name': 'Synthetic clear group', 'user_ids': [other['id']],
        }, token)
        cid = channel['id']
        old = await call('POST', f'/api/v1/channels/{cid}/messages/post', {'content': 'Original member message'}, token)
        await act('recycle', 'channel', cid)
        assert await call('GET', f'/api/v1/channels/{cid}/messages', token=token) == []
        theirs = await call('GET', f'/api/v1/channels/{cid}/messages', token=other['token'])
        assert any(m['id'] == old['id'] for m in theirs), theirs
        # Reusing another browser's access token still respects the same cutoff.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://second-device') as device:
            res = await device.get(f'/api/v1/channels/{cid}/messages', headers={'Authorization': f'Bearer {token}'})
            assert res.status_code == 200 and res.json() == [], res.text
        new = await call('POST', f'/api/v1/channels/{cid}/messages/post', {
            'content': 'New message after clear', 'reply_to_id': old['id'],
        }, other['token'])
        mine = await call('GET', f'/api/v1/channels/{cid}/messages', token=token)
        assert len(mine) == 1 and mine[0]['id'] == new['id'] and mine[0]['reply_to_message'] is None, mine
        pre_old_undo = await preview('restore', 'channel', cid)
        await act('recycle', 'channel', cid)
        stale = await call('POST', '/api/v1/resources/tasks', {
            'preview_id': pre_old_undo['id'], 'idempotency_key': secrets.token_hex(12),
        }, token)
        assert stale['state'] == 'failed', stale
        await act('restore', 'channel', cid)
        mine = await call('GET', f'/api/v1/channels/{cid}/messages', token=token)
        assert len(mine) == 1 and mine[0]['id'] == new['id'], mine
        await act('restore', 'channel', cid)
        mine = await call('GET', f'/api/v1/channels/{cid}/messages', token=token)
        assert len(mine) == 2, mine
        passed += ['personal_clear_other_member_unchanged', 'cross_device_boundary', 'new_messages_visible',
                   'reply_preview_redacted', 'stale_undo_rejected', 'consecutive_clear_undo']
        print(json.dumps({'passed': passed, 'count': len(passed)}, indent=2))
        from open_webui.internal.db import async_engine, engine
        await async_engine.dispose()
        engine.dispose()



def main():
    with tempfile.TemporaryDirectory(prefix='owui-resources-') as temporary:
        root = Path(temporary)
        env = configuration(REPO_ROOT, 8081, dict(os.environ))
        env.update({
            'DATA_DIR': str(root / 'data'), 'STATIC_DIR': str(root / 'static'),
            'DATABASE_URL': f'sqlite:///{root / "data" / "webui.db"}',
            'WEBUI_SECRET_KEY': secrets.token_urlsafe(48),
            'WEBUI_SECRET_KEY_FILE': str(root / 'secret'),
            'FRONTEND_BUILD_DIR': str(root / 'absent'),
        })
        (root / 'data').mkdir()
        (root / 'static').mkdir()
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(REPO_ROOT / 'backend'))
        asyncio.run(verify())


if __name__ == '__main__':
    main()
