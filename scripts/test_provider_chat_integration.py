#!/usr/bin/env python3
"""Exercise provider chat routing and account isolation on a disposable database."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from unittest.mock import AsyncMock, patch

from dev_backend import REPO_ROOT, configuration


async def verify():
    import httpx
    from aiohttp import web
    from open_webui.main import app
    from open_webui.models.config import Config
    from open_webui.models.users import Users
    from open_webui.utils.models import get_request_models, load_provider_models
    from open_webui.utils.providers import stable_model_id
    from starlette.requests import Request

    received = []
    responses_received = []

    async def models(request):
        assert request.headers['Authorization'].startswith('Bearer synthetic-')
        return web.json_response(
            {
                'data': [{'id': 'same-model'}],
                'models': [{'slug': 'same-model', 'display_name': 'Synthetic ChatGPT', 'visibility': 'list'}],
            }
        )

    async def responses(request):
        assert request.headers['Authorization'] == 'Bearer synthetic-oauth-access'
        body = await request.json()
        responses_received.append(body)
        assert body['model'] == 'same-model' and body['stream'] is True and body['store'] is False
        if len(responses_received) == 2:
            events = [
                {
                    'type': 'response.failed',
                    'response': {
                        'error': {
                            'code': 'subscription_sharing_usage_limit_exceeded',
                            'message': 'Synthetic plan limit',
                        }
                    },
                }
            ]
        else:
            events = [
                {
                    'type': 'response.output_item.added',
                    'output_index': 0,
                    'item': {
                        'id': 'synthetic-message',
                        'type': 'message',
                        'role': 'assistant',
                        'content': [],
                    },
                },
                {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '你好🌏'},
                {'type': 'response.completed', 'response': {'id': 'synthetic-response', 'output': []}},
            ]
        # A Responses SSE body normally includes event: fields and several data
        # records in one network read. This used to silently become a blank chat.
        wire = ''.join(
            'event: ' + event['type'] + '\n' + 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
            for event in events
        )
        return web.Response(text=wire, content_type='text/event-stream')

    async def completion(request):
        body = await request.json()
        received.append((request.headers['Authorization'], body))
        assert body['model'] == 'same-model'
        assert not any(key in body for key in ('metadata', 'chat_id', 'session_id', 'files', 'background_tasks'))
        result = {
            'id': 'test-completion',
            'object': 'chat.completion',
            'model': 'same-model',
            'choices': [
                {'index': 0, 'message': {'role': 'assistant', 'content': 'synthetic reply'}, 'finish_reason': 'stop'}
            ],
        }
        if body.get('stream'):
            chunks = {
                'id': 'test-completion',
                'object': 'chat.completion.chunk',
                'model': 'same-model',
                'choices': [{'index': 0, 'delta': {'content': 'synthetic reply'}, 'finish_reason': None}],
            }
            return web.Response(
                text='data: ' + json.dumps(chunks) + '\n\ndata: [DONE]\n\n', content_type='text/event-stream'
            )
        return web.json_response(result)

    upstream = web.Application()
    upstream.router.add_get('/v1/models', models)
    upstream.router.add_post('/v1/chat/completions', completion)
    upstream.router.add_post('/v1/responses', responses)
    runner = web.AppRunner(upstream)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{port}/v1'
    os.environ['APP_ALLOWED_PRIVATE_ORIGINS'] = f'http://127.0.0.1:{port}'

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://provider-test') as client:

            async def call(method, path, body=None, token=None, expected=200):
                response = await client.request(
                    method, path, json=body, headers={'Authorization': f'Bearer {token}'} if token else {}
                )
                assert response.status_code == expected, (method, path, response.status_code, response.text[:1000])
                return response.json()

            async def signup(name):
                return await call(
                    'POST',
                    '/api/v1/auths/signup',
                    {'name': name, 'email': name + '@example.invalid', 'password': secrets.token_urlsafe(24)},
                )

            admin = await signup('provider-admin')
            config = await call('GET', '/api/config', token=admin['token'])
            assert config['features']['enable_provider_connections'] is True
            await Config.upsert({'ui.enable_signup': True})
            alice, bob = await signup('provider-alice'), await signup('provider-bob')
            for account in (alice, bob):
                await Users.update_user_by_id(account['id'], {'role': 'user'})

            # OAuth control plane is exercised without contacting OpenAI or using real credentials.
            auth_prefix = '/api/v1/providers/auth/chatgpt'
            attempt = await call('POST', auth_prefix + '/start', {}, alice['token'])
            assert 'pairing_code' in attempt and 'access_token' not in attempt
            helper = await client.get(auth_prefix + '/helper')
            assert helper.status_code == 200 and 'Python' in helper.text
            await call('GET', auth_prefix + '/' + attempt['attempt_id'], token=bob['token'], expected=404)
            pair_body = {
                'pairing_code': attempt['pairing_code'],
                'state': 's' * 43,
                'nonce': 'n' * 43,
                'code_challenge': 'c' * 43,
                'redirect_uri': 'http://127.0.0.1:1455/auth/callback',
            }
            paired = await call('POST', auth_prefix + '/pair', pair_body)
            assert paired['client_id'] == 'dynamic_agent_client' and paired['host_id'].startswith('urn:uuid:')
            await call('POST', auth_prefix + '/pair', pair_body, expected=404)
            token_body = {
                'state': pair_body['state'],
                'client_id': 'oaiapp_synthetic',
                'token': {
                    'access_token': 'synthetic-oauth-access',
                    'refresh_token': 'synthetic-oauth-refresh',
                    'id_token': 'synthetic-oidc-token',
                    'scope': 'openid offline_access resource.invoke chatgpt.tokens.use.direct',
                    'expires_in': 3600,
                },
            }
            from open_webui.routers import provider_auth

            claims = {'iss': 'https://auth.openai.com', 'sub': 'synthetic-subject', 'email': 'alice@example.invalid'}
            with patch.object(provider_auth, 'validate_id_token', new=AsyncMock(return_value=claims)) as validate:
                await call('POST', auth_prefix + '/complete', token_body, paired['helper_token'])
                validate.assert_awaited_once_with(
                    'synthetic-oidc-token', client_id='oaiapp_synthetic', nonce=pair_body['nonce']
                )
            await call('POST', auth_prefix + '/complete', token_body, paired['helper_token'], expected=404)
            poll = await call('GET', auth_prefix + '/' + attempt['attempt_id'], token=alice['token'])
            assert poll['status'] == 'complete', poll
            rows = await call('GET', '/api/v1/providers/connections', token=alice['token'])
            oauth = next(row for row in rows if row['provider_id'] == 'chatgpt')
            assert oauth['visibility'] == 'private' and 'synthetic-oauth' not in json.dumps(oauth)
            await call('GET', '/api/v1/providers/connections/' + oauth['id'], token=admin['token'], expected=404)
            with patch.object(provider_auth, 'revoke_chatgpt_session', new=AsyncMock(return_value=False)):
                disconnected = await call('DELETE', auth_prefix + '/connection/' + oauth['id'], token=alice['token'])
            assert disconnected['revocation_confirmed'] is False
            # A disconnected registration remains available for explicit reconnect but contributes no models.

            # Browser/manual callback requires no local helper or token import.
            from urllib.parse import parse_qs, urlsplit, urlencode

            browser = await call('POST', auth_prefix + '/start', {'mode': 'browser'}, alice['token'])
            assert 'authorization_url' in browser and 'command' not in browser and 'pairing_code' not in browser
            query = parse_qs(urlsplit(browser['authorization_url']).query)
            callback_url = (
                browser['redirect_uri']
                + '?'
                + urlencode(
                    {
                        'code': 'synthetic-browser-code',
                        'state': query['state'][0],
                        'client_id': 'oaiapp_browser_http',
                    }
                )
            )
            callback_path = auth_prefix + '/' + browser['attempt_id'] + '/callback'
            await call('POST', callback_path, {'callback_url': callback_url}, bob['token'], expected=404)
            await call(
                'POST',
                callback_path,
                {'callback_url': callback_url.replace(query['state'][0], 'wrong')},
                alice['token'],
                expected=400,
            )
            with (
                patch.object(
                    provider_auth, 'exchange_authorization_code', new=AsyncMock(return_value=token_body['token'])
                ) as exchange,
                patch.object(provider_auth, 'validate_id_token', new=AsyncMock(return_value=claims)),
            ):
                connected = await call('POST', callback_path, {'callback_url': callback_url}, alice['token'])
                assert connected['status'] == 'complete'
                assert exchange.await_count == 1
            await call('POST', callback_path, {'callback_url': callback_url}, alice['token'], expected=404)
            await call(
                'GET', '/api/v1/providers/connections/' + connected['connection_id'], token=bob['token'], expected=404
            )

            ids = {}
            for account in (alice, bob):
                connection = await call(
                    'POST',
                    '/api/v1/providers/connections',
                    {
                        'provider_id': 'custom',
                        'name': account['name'],
                        'auth_type': 'api_key',
                        'visibility': 'private',
                        'api_key': 'synthetic-' + account['id'],
                        'config': {'base_url': base},
                    },
                    account['token'],
                )
                ids[account['id']] = stable_model_id(connection['id'], 'same-model')
                assert 'credential' not in connection and 'api_key' not in connection
                await call('POST', f"/api/v1/providers/connections/{connection['id']}/verify", {}, account['token'])
                # Even administrator privileges must not expose other people's private connections.
                await call(
                    'GET', f"/api/v1/providers/connections/{connection['id']}", token=admin['token'], expected=404
                )

            # Keep legacy model discovery offline; provider traffic still reaches the loopback upstream.
            from open_webui.utils import models as registry

            with patch.object(registry, 'get_all_base_models', return_value=[]):
                for account, other in ((alice, bob), (bob, alice)):
                    listing = await call('GET', '/api/models', token=account['token'])
                    visible = {model['id'] for model in listing['data']}
                    assert ids[account['id']] in visible and ids[other['id']] not in visible, visible
                    assert not any(key.startswith('provider:') for key in app.state.MODELS)
                    payload = {
                        'model': ids[account['id']],
                        'messages': [{'role': 'user', 'content': 'Hello'}],
                        'stream': False,
                    }
                    reply = await call('POST', '/api/chat/completions', payload, account['token'])
                    assert reply['choices'][0]['message']['content'] == 'synthetic reply', reply
                    stream = await client.post(
                        '/api/chat/completions',
                        json={**payload, 'stream': True},
                        headers={'Authorization': 'Bearer ' + account['token']},
                    )
                    assert (
                        stream.status_code == 200 and 'synthetic reply' in stream.text and '[DONE]' in stream.text
                    ), stream.text
                    denied = await client.post(
                        '/api/chat/completions',
                        json={**payload, 'model': ids[other['id']]},
                        headers={'Authorization': 'Bearer ' + account['token']},
                    )
                    assert denied.status_code in (400, 403, 404), denied.text

                # Separate requests get separate overlays; loading Bob cannot overwrite Alice or global state.
                def req():
                    return Request({'type': 'http', 'app': app, 'headers': [], 'state': {}})

                ar, br = req(), req()
                au, bu = await Users.get_user_by_id(alice['id']), await Users.get_user_by_id(bob['id'])
                await asyncio.gather(load_provider_models(ar, au), load_provider_models(br, bu))
                assert ids[alice['id']] in get_request_models(ar) and ids[bob['id']] not in get_request_models(ar)
                assert ids[bob['id']] in get_request_models(br) and ids[alice['id']] not in get_request_models(br)

                shared = await call(
                    'POST',
                    '/api/v1/providers/connections',
                    {
                        'provider_id': 'custom',
                        'name': 'Shared API',
                        'auth_type': 'api_key',
                        'visibility': 'shared',
                        'api_key': 'synthetic-shared',
                        'config': {'base_url': base},
                        'access_control': [
                            {'principal_type': 'user', 'principal_id': alice['id'], 'permission': 'read'}
                        ],
                    },
                    admin['token'],
                )
                shared_id = stable_model_id(shared['id'], 'same-model')
                for account, expected in ((alice, True), (bob, False)):
                    listing = await call('GET', '/api/models', token=account['token'])
                    assert (shared_id in {model['id'] for model in listing['data']}) == expected
                await call(
                    'PATCH', f"/api/v1/providers/connections/{shared['id']}", {'access_control': []}, admin['token']
                )
                payload = {
                    'model': shared_id,
                    'messages': [{'role': 'user', 'content': 'Revoked access'}],
                    'stream': False,
                }
                denied = await client.post(
                    '/api/chat/completions', json=payload, headers={'Authorization': 'Bearer ' + alice['token']}
                )
                assert denied.status_code in (400, 403, 404), denied.text

            # Route a connected OAuth account through real HTTP and the saved
            # chat/socket middleware, not just a raw SSE passthrough API call.
            from open_webui.utils import chatgpt_auth, middleware

            oauth_model = stable_model_id(connected['connection_id'], 'same-model')
            with (
                patch.object(registry, 'get_all_base_models', return_value=[]),
                patch.object(chatgpt_auth, 'OPENAI_API', base),
            ):
                for should_fail in (False, True):
                    chat = await call(
                        'POST',
                        '/api/v1/chats/new',
                        {
                            'chat': {
                                'title': 'Synthetic OAuth streaming',
                                'history': {'messages': {}, 'currentId': None},
                            }
                        },
                        alice['token'],
                    )
                    emitter = AsyncMock()
                    with patch.object(
                        middleware, 'get_event_emitter_and_caller', new=AsyncMock(return_value=(emitter, None))
                    ):
                        await call(
                            'POST',
                            '/api/chat/completions',
                            {
                                'model': oauth_model,
                                'stream': True,
                                'chat_id': chat['id'],
                                'id': 'synthetic-reply',
                                'messages': [{'role': 'user', 'content': 'Hello'}],
                            },
                            alice['token'],
                        )
                    events = [c.args[0] for c in emitter.await_args_list]
                    from open_webui.models.chats import Chats

                    saved = await Chats.get_message_by_id_and_message_id(chat['id'], 'synthetic-reply')
                    if should_fail:
                        assert any(e['data'].get('error') for e in events), events
                        assert 'subscription_sharing_usage_limit_exceeded' in json.dumps(saved), saved
                    else:
                        final = next(e['data'] for e in reversed(events) if e['data'].get('done'))
                        assert final['output'][0]['content'][0]['text'] == '你好🌏', final
                        assert saved['output'][0]['content'][0]['text'] == '你好🌏', saved
            assert len(responses_received) == 2
            assert len(received) == 4, received
            assert received[0][0] != received[2][0]
            print(
                json.dumps(
                    {
                        'provider_chat_integration': 'passed',
                        'checks': [
                            'real_http_forwarding',
                            'stable_model_routing',
                            'private_admin_isolation',
                            'cross_user_invoke_denied',
                            'request_cache_isolation',
                            'internal_payload_redaction',
                            'sse_chat',
                            'oauth_responses_sse_real_http_and_saved_chat',
                            'oauth_in_stream_plan_limit_visible_and_persisted',
                            'shared_grants_revocation',
                            'oauth_helper_download',
                            'oauth_pair_replay_denied',
                            'oauth_complete_replay_denied',
                            'oauth_private_owner_binding',
                            'oauth_browser_callback_without_helper',
                            'oauth_browser_callback_owner_and_replay_checks',
                        ],
                    }
                )
            )
    finally:
        await runner.cleanup()
        from open_webui.internal.db import async_engine, engine

        await async_engine.dispose()
        engine.dispose()


def main():
    with tempfile.TemporaryDirectory(prefix='owui-provider-chat-') as temporary:
        root = Path(temporary)
        env = configuration(REPO_ROOT, 8081, dict(os.environ))
        env.update(
            {
                'DATA_DIR': str(root / 'data'),
                'STATIC_DIR': str(root / 'static'),
                'DATABASE_URL': f'sqlite:///{root / "data" / "webui.db"}',
                'WEBUI_SECRET_KEY': secrets.token_urlsafe(48),
                'WEBUI_SECRET_KEY_FILE': str(root / 'secret'),
                'FRONTEND_BUILD_DIR': str(root / 'absent'),
            }
        )
        (root / 'data').mkdir()
        (root / 'static').mkdir()
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(REPO_ROOT / 'backend'))
        asyncio.run(verify())


if __name__ == '__main__':
    main()
