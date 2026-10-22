"""Offline contract tests for the Kimi Code device authorization adapter."""

import asyncio
import json
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from open_webui.internal.db import AsyncSessionLocal, async_engine
from open_webui.models.provider_connections import ProviderConnection
from open_webui.routers.kimi_auth import (
    KimiAuthAttempt,
    cancel_kimi_auth,
    get_kimi_auth_status,
    start_kimi_auth,
)
from open_webui.utils import kimi_auth, providers
from open_webui.routers import kimi_auth as router


class KimiAuthContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with async_engine.begin() as connection:
            await connection.run_sync(ProviderConnection.__table__.create, checkfirst=True)
            await connection.run_sync(KimiAuthAttempt.__table__.create, checkfirst=True)

    def test_device_headers_are_stable_and_identify_open_webui(self):
        first = providers.kimi_device_headers()
        second = providers.kimi_device_headers()
        self.assertEqual(first, second)
        self.assertEqual(first['X-Msh-Platform'], 'open_webui')
        self.assertTrue(first['X-Msh-Device-Id'])
        self.assertNotIn(providers.WEBUI_SECRET_KEY, first.values())

    def test_token_response_requires_renewable_session(self):
        token = kimi_auth.normalize_token_response(
            {
                'access_token': 'access',
                'refresh_token': 'refresh',
                'expires_in': 3600,
                'scope': 'profile coding',
                'token_type': 'Bearer',
            }
        )
        self.assertEqual(token['access_token'], 'access')
        self.assertEqual(token['scopes'], ['coding', 'profile'])
        with self.assertRaises(HTTPException):
            kimi_auth.normalize_token_response({'access_token': 'access', 'expires_in': 3600})

    async def test_start_persists_encrypted_device_code_and_owner_scoped_public_state(self):
        owner = SimpleNamespace(id='kimi-start-' + str(uuid.uuid4()), role='user')
        authorization = {
            'user_code': 'ABCD-EFGH',
            'device_code': 'device-secret-that-must-not-be-public',
            'verification_uri': 'https://example.test/device',
            'verification_uri_complete': 'https://example.test/device?code=ABCD-EFGH',
            'expires_in': 600,
            'interval': 5,
        }
        async with AsyncSessionLocal() as db:
            with patch.object(router, 'request_device_authorization', new=AsyncMock(return_value=authorization)):
                public = await start_kimi_auth(None, owner, db)
            self.assertEqual(public['status'], 'pending')
            self.assertEqual(public['user_code'], 'ABCD-EFGH')
            self.assertNotIn('device_code', public)
            row = await db.get(KimiAuthAttempt, public['attempt_id'])
            self.assertNotEqual(row.device_code, authorization['device_code'])
            self.assertNotIn(authorization['device_code'], row.device_code)

    async def test_pending_then_complete_consumes_device_code_once(self):
        owner = SimpleNamespace(id='kimi-complete-' + str(uuid.uuid4()), role='user')
        now = int(time.time())
        attempt_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(
                KimiAuthAttempt(
                    id=attempt_id,
                    owner_id=owner.id,
                    status='pending',
                    device_code=providers.encrypt_credential('device-code'),
                    user_code='ABCD-EFGH',
                    verification_uri='https://example.test/device',
                    verification_uri_complete='https://example.test/device?code=ABCD-EFGH',
                    device_id='test-device',
                    interval=5,
                    next_poll_at=now,
                    connection_id=None,
                    error_code=None,
                    expires_at=now + 600,
                    created_at=now,
                )
            )
            await db.commit()
            with patch.object(
                router,
                'poll_device_authorization',
                new=AsyncMock(return_value=(400, {'error': 'authorization_pending'})),
            ):
                pending = await get_kimi_auth_status(attempt_id, owner, db)
            self.assertEqual(pending['status'], 'pending')
            row = await db.get(KimiAuthAttempt, attempt_id)
            row.next_poll_at = 0
            await db.commit()
            token = {
                'access_token': 'access',
                'refresh_token': 'refresh',
                'expires_in': 3600,
                'scope': 'coding',
                'token_type': 'Bearer',
            }
            with patch.object(
                router,
                'poll_device_authorization',
                new=AsyncMock(return_value=(200, token)),
            ) as poll:
                complete = await get_kimi_auth_status(attempt_id, owner, db)
            self.assertEqual(complete['status'], 'complete')
            self.assertTrue(complete['connection_id'])
            poll.assert_awaited_once()
            row = await db.get(KimiAuthAttempt, attempt_id)
            self.assertEqual(row.device_code, '')
            self.assertEqual(row.user_code, '')
            with patch.object(router, 'poll_device_authorization', new=AsyncMock()) as replay:
                again = await get_kimi_auth_status(attempt_id, owner, db)
            self.assertEqual(again['status'], 'complete')
            replay.assert_not_awaited()

    async def test_cancel_makes_pending_code_unusable(self):
        owner = SimpleNamespace(id='kimi-cancel-' + str(uuid.uuid4()), role='user')
        now = int(time.time())
        attempt_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(
                KimiAuthAttempt(
                    id=attempt_id,
                    owner_id=owner.id,
                    status='pending',
                    device_code=providers.encrypt_credential('device-code'),
                    user_code='ABCD-EFGH',
                    verification_uri='https://example.test/device',
                    verification_uri_complete='https://example.test/device?code=ABCD-EFGH',
                    device_id='test-device',
                    interval=5,
                    next_poll_at=0,
                    connection_id=None,
                    error_code=None,
                    expires_at=now + 600,
                    created_at=now,
                )
            )
            await db.commit()
            result = await cancel_kimi_auth(attempt_id, owner, db)
            self.assertEqual(result['status'], 'cancelled')
            row = await db.get(KimiAuthAttempt, attempt_id)
            self.assertEqual(row.device_code, '')
            self.assertEqual(row.user_code, '')

    async def test_rotating_refresh_is_serialized_and_persisted(self):
        owner = SimpleNamespace(id='kimi-refresh-' + str(uuid.uuid4()), role='user')
        connection_id = str(uuid.uuid4())
        initial = {
            'access_token': 'expired',
            'refresh_token': 'refresh-old',
            'expires_at': int(time.time()) - 1,
            'scopes': ['coding'],
            'token_type': 'Bearer',
        }
        async with AsyncSessionLocal() as db:
            db.add(
                ProviderConnection(
                    id=connection_id,
                    provider_id='kimi',
                    name='Kimi test',
                    auth_type='oauth',
                    visibility='private',
                    owner_id=owner.id,
                    config={'device_id': 'device-test'},
                    credential=providers.encrypt_credential(json.dumps(initial)),
                    status='connected',
                    models=[],
                    is_active=True,
                    created_at=int(time.time()),
                    updated_at=int(time.time()),
                )
            )
            await db.commit()
            connection = await db.get(ProviderConnection, connection_id)

        response = {
            'access_token': 'fresh',
            'refresh_token': 'refresh-rotated',
            'expires_in': 3600,
            'scope': 'coding',
            'token_type': 'Bearer',
        }
        with patch.object(
            kimi_auth,
            '_form_request',
            new=AsyncMock(return_value=(200, response)),
        ) as request:
            tokens = await asyncio.gather(
                kimi_auth.get_provider_access_token(connection, owner),
                kimi_auth.get_provider_access_token(connection, owner),
            )
        self.assertEqual(tokens, ['fresh', 'fresh'])
        request.assert_awaited_once()
        async with AsyncSessionLocal() as db:
            row = await db.get(ProviderConnection, connection_id)
            stored = await providers.get_oauth_credentials(row, owner, db=db, provider_id='kimi')
        self.assertEqual(stored['refresh_token'], 'refresh-rotated')

    async def pending_attempt(self):
        owner = SimpleNamespace(id='kimi-race-' + str(uuid.uuid4()), role='user')
        attempt_id = str(uuid.uuid4())
        now = int(time.time())
        async with AsyncSessionLocal() as db:
            db.add(
                KimiAuthAttempt(
                    id=attempt_id,
                    owner_id=owner.id,
                    status='pending',
                    device_code=providers.encrypt_credential('device-secret'),
                    user_code='ABCD',
                    verification_uri='https://www.kimi.com/device',
                    verification_uri_complete='https://www.kimi.com/device?code=ABCD',
                    device_id='device-test',
                    interval=5,
                    next_poll_at=0,
                    expires_at=now + 600,
                    created_at=now,
                )
            )
            await db.commit()
        return owner, attempt_id

    async def test_cancel_during_upstream_request_discards_late_authorization(self):
        owner, attempt_id = await self.pending_attempt()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def upstream(*args):
            entered.set()
            await resume.wait()
            return 200, {'access_token': 'late', 'refresh_token': 'late-refresh', 'expires_in': 3600}

        async def poll():
            async with AsyncSessionLocal() as db:
                return await get_kimi_auth_status(attempt_id, owner, db)

        with patch.object(router, 'poll_device_authorization', new=upstream):
            task = asyncio.create_task(poll())
            await asyncio.wait_for(entered.wait(), 2)
            async with AsyncSessionLocal() as db:
                await cancel_kimi_auth(attempt_id, owner, db)
            resume.set()
            result = await task
        self.assertEqual(result['status'], 'cancelled')
        async with AsyncSessionLocal() as db:
            row = await db.get(KimiAuthAttempt, attempt_id)
            self.assertIsNone(row.connection_id)
            self.assertEqual(row.device_code, '')

    async def test_poll_lease_prevents_duplicate_upstream_requests(self):
        owner, attempt_id = await self.pending_attempt()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def upstream(*args):
            entered.set()
            await resume.wait()
            return 400, {'error': 'authorization_pending'}

        async def poll():
            async with AsyncSessionLocal() as db:
                return await get_kimi_auth_status(attempt_id, owner, db)

        with patch.object(router, 'poll_device_authorization', new=AsyncMock(side_effect=upstream)) as request:
            first = asyncio.create_task(poll())
            await asyncio.wait_for(entered.wait(), 2)
            second = await poll()
            self.assertEqual(second['status'], 'pending')
            resume.set()
            await first
        request.assert_awaited_once()

    async def test_attempt_expiry_and_owner_checks_prevent_upstream_use(self):
        owner, attempt_id = await self.pending_attempt()
        async with AsyncSessionLocal() as db:
            with self.assertRaises(HTTPException) as denied:
                await get_kimi_auth_status(attempt_id, SimpleNamespace(id='other'), db)
            self.assertEqual(denied.exception.status_code, 404)
            row = await db.get(KimiAuthAttempt, attempt_id)
            row.expires_at = 1
            await db.commit()
            with patch.object(router, 'poll_device_authorization', new=AsyncMock()) as request:
                result = await get_kimi_auth_status(attempt_id, owner, db)
            request.assert_not_awaited()
            self.assertEqual(result['status'], 'expired')
            self.assertEqual(row.device_code, '')

    async def test_slow_down_extends_poll_interval(self):
        owner, attempt_id = await self.pending_attempt()
        async with AsyncSessionLocal() as db:
            with patch.object(
                router, 'poll_device_authorization', new=AsyncMock(return_value=(400, {'error': 'slow_down'}))
            ):
                result = await get_kimi_auth_status(attempt_id, owner, db)
            self.assertEqual(result['interval'], 10)
            with patch.object(router, 'poll_device_authorization', new=AsyncMock()) as request:
                await get_kimi_auth_status(attempt_id, owner, db)
            request.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
