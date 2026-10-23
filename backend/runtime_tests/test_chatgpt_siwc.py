"""Offline contract tests for the ChatGPT SIWC adapter.

These tests require the full backend runtime dependencies. Run them through
``scripts/test_provider_runtime.py``, which supplies disposable storage before
the backend modules are imported.
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import unittest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from urllib.parse import parse_qs, urlsplit

from open_webui.utils.chatgpt_auth import (
    AUTHORIZATION_ENDPOINT,
    build_authorize_url,
    build_chatgpt_responses_payload,
    normalize_token_response,
    pkce_challenge,
)
from open_webui.routers.provider_auth import (
    ChatGPTAuthAttempt,
    PairForm,
    _hash,
    _safe_redirect_uri,
    _valid_issued_client_id,
    pair_local_helper,
)
from fastapi import HTTPException
from sqlalchemy import select
from open_webui.internal.db import AsyncSessionLocal, async_engine
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.providers import encrypt_credential, get_oauth_credentials


class ChatGPTSIWCTests(unittest.TestCase):
    def test_authorize_url_uses_dynamic_public_client_and_loopback_pkce(self):
        url = build_authorize_url(
            state='state-value',
            nonce='nonce-value',
            code_challenge='challenge-value',
            host_id='urn:uuid:11111111-1111-4111-8111-111111111111',
            redirect_uri='http://127.0.0.1:1455/auth/callback',
        )
        parsed = urlsplit(url)
        values = parse_qs(parsed.query)
        self.assertEqual(f'{parsed.scheme}://{parsed.netloc}{parsed.path}', AUTHORIZATION_ENDPOINT)
        self.assertEqual(values['client_id'], ['dynamic_agent_client'])
        self.assertEqual(values['ext_agent_host_id'], ['urn:uuid:11111111-1111-4111-8111-111111111111'])
        self.assertEqual(values['code_challenge_method'], ['S256'])
        self.assertIn('chatgpt.tokens.use.direct', values['scope'][0])
        self.assertEqual(values['redirect_uri'], ['http://127.0.0.1:1455/auth/callback'])

    def test_pkce_challenge_is_unpadded_base64url_sha256(self):
        self.assertEqual(pkce_challenge('verifier'), 'iMnq5o6zALKXGivsnlom_0F5_WYda32GHkxlV7mq7hQ')

    def test_callback_requires_exact_loopback_host_and_path(self):
        self.assertTrue(_safe_redirect_uri('http://127.0.0.1:54321/auth/callback'))
        self.assertFalse(_safe_redirect_uri('http://localhost:54321/auth/callback'))
        self.assertFalse(_safe_redirect_uri('http://127.0.0.1:54321/callback'))
        self.assertFalse(_safe_redirect_uri('https://127.0.0.1:54321/auth/callback'))

    def test_dynamic_registration_exchanges_against_issued_client_id(self):
        attempt = ChatGPTAuthAttempt(client_id='dynamic_agent_client', expected_client_id=None)
        self.assertTrue(_valid_issued_client_id(attempt, 'oaiapp_registration'))
        self.assertFalse(_valid_issued_client_id(attempt, 'dynamic_agent_client'))
        self.assertFalse(_valid_issued_client_id(attempt, 'other-client'))
        returning = ChatGPTAuthAttempt(client_id='oaiapp_saved', expected_client_id='oaiapp_saved')
        self.assertTrue(_valid_issued_client_id(returning, 'oaiapp_saved'))
        self.assertFalse(_valid_issued_client_id(returning, 'oaiapp_replaced'))

    def test_credentials_require_direct_usage_and_refresh_scopes(self):
        token = {
            'access_token': 'access',
            'refresh_token': 'refresh',
            'id_token': 'id-token',
            'scope': 'openid offline_access resource.invoke chatgpt.tokens.use.direct',
            'expires_in': 3600,
        }
        claims = {'iss': 'https://auth.openai.com', 'sub': 'subject', 'email': 'a@example.com'}
        result = normalize_token_response(
            token, claims, client_id='oaiapp_example', host_id='urn:uuid:example'
        )
        self.assertEqual(result['client_id'], 'oaiapp_example')
        self.assertEqual(result['subject'], 'subject')
        token['scope'] = 'openid offline_access resource.invoke'
        with self.assertRaises(HTTPException):
            normalize_token_response(token, claims, client_id='oaiapp_example', host_id='urn:uuid:example')

    def test_responses_payload_drops_preview_unsupported_fields(self):
        payload = {
            'messages': [
                {'role': 'system', 'content': 'Be concise.'},
                {'role': 'user', 'content': 'Hello'},
            ],
            'temperature': 0.2,
            'top_p': 0.5,
            'max_tokens': 100,
            'previous_response_id': 'resp_previous',
            'store': True,
            'stream': False,
        }
        result = build_chatgpt_responses_payload(payload, 'gpt-test')
        self.assertEqual(result['model'], 'gpt-test')
        self.assertEqual(result['store'], False)
        self.assertEqual(result['stream'], True)
        self.assertEqual(result['instructions'], 'Be concise.')
        self.assertEqual(result['input'][0]['role'], 'user')
        for key in ('temperature', 'top_p', 'max_output_tokens', 'previous_response_id'):
            self.assertNotIn(key, result)

    def test_responses_payload_preserves_function_call_history(self):
        result = build_chatgpt_responses_payload({
            'messages': [
                {'role': 'assistant', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'lookup', 'arguments': '{"q":"x"}'}}]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found'},
            ],
            'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}],
        }, 'gpt-test')
        self.assertEqual(result['input'][0]['type'], 'function_call')
        self.assertEqual(result['input'][1]['type'], 'function_call_output')
        self.assertEqual(result['tools'][0]['type'], 'function')


class ChatGPTRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with async_engine.begin() as connection:
            await connection.run_sync(ProviderConnection.__table__.create, checkfirst=True)
            await connection.run_sync(ChatGPTAuthAttempt.__table__.create, checkfirst=True)

    async def test_rotating_refresh_is_serialized_across_db_sessions(self):
        from open_webui.utils import chatgpt_auth

        owner = SimpleNamespace(id='refresh-test-owner')
        connection_id = 'refresh-test-connection'
        initial = {
            'access_token': 'expired-access',
            'refresh_token': 'refresh-old',
            'id_token': 'already-validated-id-token',
            'client_id': 'oaiapp_test',
            'subject': 'subject-test',
            'host_id': 'urn:uuid:refresh-test',
            'scopes': ['offline_access', 'chatgpt.tokens.use.direct'],
            'expires_at': int(time.time()) - 1,
        }
        async with AsyncSessionLocal() as db:
            await db.merge(ProviderConnection(
                id=connection_id,
                provider_id='chatgpt',
                name='ChatGPT test',
                auth_type='oauth',
                visibility='private',
                owner_id=owner.id,
                config={'host_id': initial['host_id']},
                credential=encrypt_credential(json.dumps(initial)),
                status='connected',
                models=[],
                is_active=True,
                created_at=int(time.time()),
                updated_at=int(time.time()),
            ))
            await db.commit()
            connection = await db.get(ProviderConnection, connection_id)

        calls = 0

        async def fake_oauth_form(_values):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return {
                'access_token': 'fresh-access',
                'refresh_token': 'refresh-rotated',
                'expires_in': 3600,
                'scope': 'offline_access resource.invoke chatgpt.tokens.use.direct',
            }

        with patch.object(chatgpt_auth, 'oauth_form', new=fake_oauth_form):
            tokens = await asyncio.gather(
                chatgpt_auth.get_provider_access_token(connection, owner),
                chatgpt_auth.get_provider_access_token(connection, owner),
            )

        self.assertEqual(tokens, ['fresh-access', 'fresh-access'])
        self.assertEqual(calls, 1)
        async with AsyncSessionLocal() as db:
            row = await db.scalar(select(ProviderConnection).where(ProviderConnection.id == connection_id))
            stored = await get_oauth_credentials(row, owner, db=db)
            self.assertEqual(stored['refresh_token'], 'refresh-rotated')

    async def test_pair_code_can_only_be_claimed_once_across_sessions(self):
        from open_webui.routers.provider_auth import pairing_rate_limiter

        attempt_id = 'pair-race-attempt'
        pairing_code = 'PAIRCODE2345'
        now = int(time.time())
        async with AsyncSessionLocal() as db:
            await db.merge(ChatGPTAuthAttempt(
                id=attempt_id,
                owner_id='pair-race-owner',
                status='pending',
                pairing_code_hash=_hash(pairing_code),
                helper_token_hash=None,
                host_id='urn:uuid:pair-race',
                connection_id=None,
                expected_subject=None,
                expected_client_id=None,
                state=None,
                nonce=None,
                code_challenge=None,
                redirect_uri=None,
                client_id=None,
                expires_at=now + 600,
                created_at=now,
            ))
            await db.commit()

        request = SimpleNamespace(
            client=SimpleNamespace(host='127.0.0.1'),
            app=SimpleNamespace(state=SimpleNamespace(redis=None)),
        )
        form = PairForm(
            pairing_code=pairing_code,
            state='state-value-' * 4,
            nonce='nonce-value-' * 4,
            code_challenge='A' * 43,
            redirect_uri='http://127.0.0.1:54321/auth/callback',
        )

        async def claim():
            async with AsyncSessionLocal() as db:
                try:
                    return await pair_local_helper(form, request, db)
                except HTTPException as error:
                    return error.status_code

        results = await asyncio.gather(claim(), claim())
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertIn(404, results)
        self.assertEqual(await pairing_rate_limiter.get_count(None, '127.0.0.1'), 2)


class ChatGPTBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from open_webui.routers.provider_auth import ChatGPTAuthHost
        async with async_engine.begin() as connection:
            for table in (ProviderConnection.__table__, ChatGPTAuthAttempt.__table__, ChatGPTAuthHost.__table__):
                await connection.run_sync(table.create, checkfirst=True)
        self.owner = SimpleNamespace(id='browser-owner', role='user')

    async def start(self):
        from open_webui.routers.provider_auth import StartAuthForm, start_chatgpt_auth
        async with AsyncSessionLocal() as db:
            attempt = await start_chatgpt_auth(None, StartAuthForm(mode='browser'), self.owner, db)
            row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
            return attempt, parse_qs(urlsplit(attempt['authorization_url']).query), row.pkce_verifier

    def callback(self, query, **changes):
        from urllib.parse import urlencode
        values = {'state': query['state'][0], 'code': 'synthetic-code', 'client_id': 'oaiapp_browser'}
        values.update(changes)
        return 'http://127.0.0.1:1455/auth/callback?' + urlencode(values)

    async def complete(self, attempt, value, owner=None):
        from open_webui.routers.provider_auth import BrowserCallbackForm, complete_browser_auth
        async with AsyncSessionLocal() as db:
            return await complete_browser_auth(attempt['attempt_id'], BrowserCallbackForm(callback_url=value), owner or self.owner, db)

    async def test_browser_callback_is_owner_bound_encrypted_and_one_time(self):
        from open_webui.routers import provider_auth
        from open_webui.models.users import Users
        from open_webui.utils.providers import decrypt_credential
        attempt, query, encrypted = await self.start()
        verifier = decrypt_credential(encrypted)
        self.assertNotEqual(encrypted, verifier)
        self.assertEqual(pkce_challenge(verifier), query['code_challenge'][0])
        self.assertNotIn('command', attempt)
        self.assertNotIn('pairing_code', attempt)
        self.assertNotIn(verifier, json.dumps(attempt))
        with self.assertRaises(HTTPException) as denied:
            await self.complete(attempt, self.callback(query), SimpleNamespace(id='another-owner'))
        self.assertEqual(denied.exception.status_code, 404)
        token = {'access_token': 'browser-access', 'refresh_token': 'browser-refresh', 'id_token': 'browser-id',
                 'expires_in': 3600, 'scope': 'openid offline_access resource.invoke chatgpt.tokens.use.direct'}
        claims = {'iss': 'https://auth.openai.com', 'sub': 'browser-subject'}
        with (patch.object(provider_auth, 'exchange_authorization_code', new=AsyncMock(return_value=token)) as exchange,
              patch.object(provider_auth, 'validate_id_token', new=AsyncMock(return_value=claims)) as validate,
              patch.object(Users, 'get_user_by_id', new=AsyncMock(return_value=self.owner))):
            result = await self.complete(attempt, self.callback(query))
            self.assertEqual(result['status'], 'complete')
            exchange.assert_awaited_once_with(code='synthetic-code', client_id='oaiapp_browser', verifier=verifier,
                                             redirect_uri='http://127.0.0.1:1455/auth/callback')
            validate.assert_awaited_once_with('browser-id', client_id='oaiapp_browser', nonce=query['nonce'][0])
        with self.assertRaises(HTTPException):
            await self.complete(attempt, self.callback(query))
        async with AsyncSessionLocal() as db:
            row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
            self.assertIsNone(row.pkce_verifier)
            connection = await db.get(ProviderConnection, result['connection_id'])
            self.assertEqual(connection.visibility, 'private')
            self.assertEqual(connection.owner_id, self.owner.id)
            self.assertNotIn('browser-access', connection.credential)

    async def test_invalid_state_origin_and_duplicate_params_do_not_consume_login(self):
        attempt, query, _ = await self.start()
        good = self.callback(query)
        for bad in (self.callback(query, state='wrong'), good.replace('127.0.0.1', 'example.com'),
                    good + '&code=duplicate', good + '#fragment', self.callback(query, client_id='wrong')):
            with self.assertRaises(HTTPException) as error:
                await self.complete(attempt, bad)
            self.assertEqual(error.exception.status_code, 400)
        async with AsyncSessionLocal() as db:
            row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
            self.assertEqual(row.status, 'pending')
            self.assertIsNotNone(row.pkce_verifier)

    async def test_decline_cancel_and_expiry_cannot_exchange(self):
        from open_webui.routers import provider_auth
        with patch.object(provider_auth, 'exchange_authorization_code', new=AsyncMock()) as exchange:
            attempt, query, _ = await self.start()
            with self.assertRaises(HTTPException):
                await self.complete(attempt, self.callback(query, error='access_denied'))
            async with AsyncSessionLocal() as db:
                row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
                self.assertEqual(row.status, 'failed')
                self.assertIsNone(row.pkce_verifier)
            for expired in (False, True):
                attempt, query, _ = await self.start()
                async with AsyncSessionLocal() as db:
                    if expired:
                        row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
                        row.expires_at = int(time.time()) - 1
                        await db.commit()
                    else:
                        await provider_auth.cancel_chatgpt_auth(attempt['attempt_id'], self.owner, db)
                with self.assertRaises(HTTPException):
                    await self.complete(attempt, self.callback(query))
            exchange.assert_not_awaited()

    async def test_concurrent_callback_submission_exchanges_only_once(self):
        from open_webui.routers import provider_auth
        from open_webui.models.users import Users
        attempt, query, _ = await self.start()
        token = {'access_token': 'once-access', 'refresh_token': 'once-refresh', 'id_token': 'once-id',
                 'expires_in': 3600, 'scope': 'offline_access resource.invoke chatgpt.tokens.use.direct'}
        async def exchange(**kwargs):
            await asyncio.sleep(0.03)
            return token
        async def submit():
            try:
                return await self.complete(attempt, self.callback(query))
            except HTTPException as error:
                return error.status_code
        with (patch.object(provider_auth, 'exchange_authorization_code', new=AsyncMock(side_effect=exchange)) as exchange_mock,
              patch.object(provider_auth, 'validate_id_token', new=AsyncMock(return_value={
                  'iss': 'https://auth.openai.com', 'sub': 'once-subject'})),
              patch.object(Users, 'get_user_by_id', new=AsyncMock(return_value=self.owner))):
            results = await asyncio.gather(submit(), submit())
            self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
            self.assertIn(404, results)
            self.assertEqual(exchange_mock.await_count, 1)

    async def test_failed_exchange_is_not_reusable_and_erases_verifier(self):
        from open_webui.routers import provider_auth
        attempt, query, _ = await self.start()
        with patch.object(provider_auth, 'exchange_authorization_code', new=AsyncMock(side_effect=HTTPException(401, 'Start a new sign-in'))):
            with self.assertRaises(HTTPException):
                await self.complete(attempt, self.callback(query))
        async with AsyncSessionLocal() as db:
            row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
            self.assertEqual(row.status, 'failed')
            self.assertIsNone(row.pkce_verifier)
        with self.assertRaises(HTTPException):
            await self.complete(attempt, self.callback(query))

    async def test_cancel_during_exchange_discards_late_tokens(self):
        from open_webui.routers import provider_auth
        attempt, query, _ = await self.start()
        async def exchange(**kwargs):
            async with AsyncSessionLocal() as db:
                await provider_auth.cancel_chatgpt_auth(attempt['attempt_id'], self.owner, db)
            return {'access_token': 'late-access', 'refresh_token': 'late-refresh', 'id_token': 'late-id',
                    'expires_in': 3600, 'scope': 'offline_access resource.invoke chatgpt.tokens.use.direct'}
        with (patch.object(provider_auth, 'exchange_authorization_code', new=exchange),
              patch.object(provider_auth, 'validate_id_token', new=AsyncMock(return_value={
                  'iss': 'https://auth.openai.com', 'sub': 'late-subject'}))):
            with self.assertRaises(HTTPException) as error:
                await self.complete(attempt, self.callback(query))
            self.assertEqual(error.exception.status_code, 409)
        async with AsyncSessionLocal() as db:
            row = await db.get(ChatGPTAuthAttempt, attempt['attempt_id'])
            self.assertNotEqual(row.status, 'complete')
            self.assertIsNone(row.connection_id)


class ChatGPTIDTokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_id_token_signature_issuer_audience_and_nonce_are_checked(self):
        from open_webui.utils import chatgpt_auth

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        public_jwk['kid'] = 'test-signing-key'
        now = int(time.time())
        claims = {
            'iss': 'https://auth.openai.com',
            'aud': 'oaiapp_test',
            'sub': 'subject-123',
            'nonce': 'nonce-123',
            'iat': now,
            'exp': now + 300,
        }
        valid_token = jwt.encode(claims, private_key, algorithm='RS256', headers={'kid': 'test-signing-key'})

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {'keys': [public_jwk]}

        class FakeSession:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        with (
            patch.object(chatgpt_auth, '_oidc_metadata', new=AsyncMock(return_value={
                'issuer': 'https://auth.openai.com',
                'jwks_uri': 'https://auth.openai.com/.well-known/jwks.json',
            })),
            patch.object(chatgpt_auth.aiohttp, 'ClientSession', FakeSession),
        ):
            decoded = await chatgpt_auth.validate_id_token(valid_token, client_id='oaiapp_test', nonce='nonce-123')
            self.assertEqual(decoded['sub'], 'subject-123')
            with self.assertRaises(HTTPException):
                await chatgpt_auth.validate_id_token(valid_token, client_id='oaiapp_test', nonce='wrong-nonce')
            with self.assertRaises(HTTPException):
                await chatgpt_auth.validate_id_token(valid_token, client_id='different-client', nonce='nonce-123')


if __name__ == '__main__':
    unittest.main()
