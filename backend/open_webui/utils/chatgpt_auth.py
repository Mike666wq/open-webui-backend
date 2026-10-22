"""Sign in with ChatGPT helpers and the Responses API adapter.

This implements OpenAI's documented Sign in with ChatGPT public-client flow.
It deliberately does not use Codex's private app-server or ChatGPT backend APIs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
import jwt
from fastapi import HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.providers import (
    get_oauth_credentials,
    replace_oauth_credentials,
    read_bounded_response,
    provider_http_error,
)
from sqlalchemy import select, update

log = logging.getLogger(__name__)

AUTHORIZATION_ENDPOINT = 'https://auth.openai.com/api/accounts/authorize'
TOKEN_ENDPOINT = 'https://auth.openai.com/api/accounts/oauth/token'
RESOURCE = 'https://api.openai.com/v1'
OPENAI_API = 'https://api.openai.com/v1'
REQUIRED_SCOPES = {'offline_access', 'resource.invoke', 'chatgpt.tokens.use.direct'}
ISSUER = 'https://auth.openai.com'
_metadata: dict[str, Any] | None = None
_metadata_expiry = 0.0
_metadata_lock = asyncio.Lock()
_refresh_locks: dict[str, asyncio.Lock] = {}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode('ascii')).digest())


def build_authorize_url(
    *,
    state: str,
    nonce: str,
    code_challenge: str,
    host_id: str,
    client_id: str = 'dynamic_agent_client',
    agent_name: str = 'Open WebUI',
    redirect_uri: str,
) -> str:
    params = {
        'client_id': client_id,
        'ext_agent_host_id': host_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
        'resource': RESOURCE,
        'state': state,
        'nonce': nonce,
        'code_challenge_method': 'S256',
        'code_challenge': code_challenge,
    }
    if client_id == 'dynamic_agent_client':
        params['agent_name_hint'] = agent_name
    return f'{AUTHORIZATION_ENDPOINT}?{urlencode(params)}'


async def oauth_form(values: dict[str, str]) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.post(TOKEN_ENDPOINT, data=values, allow_redirects=False) as response:
                raw = await read_bounded_response(response, 256_000)
                if len(raw) > 256_000:
                    raise HTTPException(502, 'OpenAI token response is too large')
                if response.status < 200 or response.status >= 300:
                    log.info('ChatGPT OAuth token endpoint returned HTTP %s', response.status)
                    raise HTTPException(401, 'ChatGPT authorization could not be completed')
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise HTTPException(502, 'OpenAI returned an invalid token response') from None
                if not isinstance(data, dict):
                    raise HTTPException(502, 'OpenAI returned an invalid token response')
                return data
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError):
        raise HTTPException(502, 'Could not reach OpenAI authentication') from None


async def exchange_authorization_code(*, code: str, client_id: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    return await oauth_form(
        {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': client_id,
            'code_verifier': verifier,
            'redirect_uri': redirect_uri,
            'resource': RESOURCE,
        }
    )


async def revoke_chatgpt_session(credentials: dict[str, Any]) -> bool:
    """Best-effort SIWC refresh-token revocation before local disconnect."""
    refresh_token = credentials.get('refresh_token')
    client_id = credentials.get('client_id')
    if not refresh_token or not client_id:
        return True
    try:
        metadata = await _oidc_metadata()
        endpoint = metadata.get('revocation_endpoint')
        if not isinstance(endpoint, str) or not endpoint.startswith(ISSUER + '/'):
            return False
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
            for attempt in range(3):
                try:
                    async with session.post(
                        endpoint,
                        data={
                            'token': refresh_token,
                            'token_type_hint': 'refresh_token',
                            'client_id': client_id,
                        },
                        allow_redirects=False,
                    ) as response:
                        if response.status == 200:
                            return True
                        if response.status < 500:
                            return False
                except (aiohttp.ClientError, TimeoutError):
                    pass
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        return False
    except Exception:
        log.info('ChatGPT session revocation was not confirmed')
        return False


async def _oidc_metadata() -> dict[str, Any]:
    global _metadata, _metadata_expiry
    if _metadata and _metadata_expiry > time.monotonic():
        return _metadata
    async with _metadata_lock:
        if _metadata and _metadata_expiry > time.monotonic():
            return _metadata
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
                async with session.get(f'{ISSUER}/.well-known/openid-configuration', allow_redirects=False) as response:
                    if response.status != 200:
                        raise RuntimeError('OpenID metadata unavailable')
                    payload = await response.json(content_type=None)
            if payload.get('issuer') != ISSUER or not payload.get('jwks_uri', '').startswith('https://'):
                raise RuntimeError('Unexpected OpenID metadata')
            _metadata = payload
            _metadata_expiry = time.monotonic() + 3600
            return payload
        except Exception:
            raise HTTPException(502, 'Could not validate ChatGPT identity') from None


async def validate_id_token(id_token: str, *, client_id: str, nonce: str | None = None) -> dict[str, Any]:
    """Validate signature and required OIDC claims against OpenAI's published JWKS."""
    try:
        metadata = await _oidc_metadata()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
            async with session.get(metadata['jwks_uri'], allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError('JWKS unavailable')
                jwks = await response.json(content_type=None)
        header = jwt.get_unverified_header(id_token)
        jwk = next((key for key in jwks.get('keys', []) if key.get('kid') == header.get('kid')), None)
        if not jwk:
            raise ValueError('unknown signing key')
        key = jwt.PyJWK.from_dict(jwk, algorithm=header.get('alg')).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=['RS256'],
            audience=client_id,
            issuer=ISSUER,
            options={'require': ['exp', 'iat', 'sub', 'iss', 'aud'] + (['nonce'] if nonce else [])},
        )
        if (nonce and claims.get('nonce') != nonce) or not claims.get('sub'):
            raise ValueError('invalid nonce or subject')
        return claims
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, 'ChatGPT identity token could not be validated') from None


def normalize_token_response(
    token: dict[str, Any], claims: dict[str, Any], *, client_id: str, host_id: str, granted_scope: str | None = None
) -> dict[str, Any]:
    access_token = token.get('access_token')
    refresh_token = token.get('refresh_token')
    id_token = token.get('id_token')
    if not all(isinstance(x, str) and x for x in (access_token, refresh_token, id_token)):
        raise HTTPException(401, 'ChatGPT did not return a renewable authorized session')
    scope_value = token.get('scope') or granted_scope or ''
    if not isinstance(scope_value, str):
        raise HTTPException(502, 'OpenAI returned invalid permission scopes')
    scopes = sorted(set(scope_value.split()))
    if 'chatgpt.tokens.use.direct' not in scopes:
        raise HTTPException(403, 'ChatGPT plan usage permission was not granted')
    expires_in = token.get('expires_in')
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise HTTPException(502, 'OpenAI returned an invalid token lifetime')
    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'id_token': id_token,
        'client_id': client_id,
        'host_id': host_id,
        'subject': claims['sub'],
        'email': claims.get('email'),
        'name': claims.get('name'),
        'issuer': claims['iss'],
        'scopes': scopes,
        'expires_at': int(time.time() + expires_in),
        'token_type': token.get('token_type', 'Bearer'),
    }


def _refresh_lock(connection_id: str) -> asyncio.Lock:
    lock = _refresh_locks.get(connection_id)
    if lock is None:
        lock = _refresh_locks[connection_id] = asyncio.Lock()
    return lock


async def _refresh_credentials(connection: ProviderConnection, user, db) -> dict[str, Any]:
    credentials = await get_oauth_credentials(connection, user, db=db)
    if not credentials or not credentials.get('refresh_token'):
        raise HTTPException(401, 'Reconnect this ChatGPT account')
    token = await oauth_form(
        {
            'grant_type': 'refresh_token',
            'client_id': credentials['client_id'],
            'refresh_token': credentials['refresh_token'],
            'resource': RESOURCE,
        }
    )
    old_id_token = credentials.get('id_token')
    expires_in = token.get('expires_in')
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise HTTPException(502, 'OpenAI returned an invalid token lifetime')
    if not isinstance(token.get('access_token'), str) or not token['access_token']:
        raise HTTPException(401, 'ChatGPT session must be reauthorized')
    if token.get('refresh_token') is not None and (
        not isinstance(token['refresh_token'], str) or not token['refresh_token']
    ):
        raise HTTPException(502, 'OpenAI returned an invalid rotating refresh token')
    if token.get('id_token') is not None and (not isinstance(token['id_token'], str) or not token['id_token']):
        raise HTTPException(502, 'OpenAI returned an invalid identity token')
    new_id_token = token.get('id_token') or old_id_token
    if token.get('id_token'):
        refreshed_claims = await validate_id_token(token['id_token'], client_id=credentials['client_id'])
        if refreshed_claims.get('sub') != credentials.get('subject'):
            raise HTTPException(401, 'ChatGPT account identity changed; reconnect this provider')
    # Refresh responses may omit identity tokens. Preserve the already validated
    # identity and nonce is not applicable to a refresh grant.
    new_creds = dict(credentials)
    scope_value = token.get('scope')
    if scope_value is None:
        scope_value = ' '.join(credentials.get('scopes') or [])
    if not isinstance(scope_value, str):
        raise HTTPException(502, 'OpenAI returned invalid permission scopes')
    new_creds.update(
        {
            'access_token': token.get('access_token'),
            'refresh_token': token.get('refresh_token') or credentials['refresh_token'],
            'id_token': new_id_token,
            'expires_at': int(time.time() + expires_in),
            'scopes': sorted(set(scope_value.split())),
        }
    )
    if not new_creds['access_token'] or not REQUIRED_SCOPES.issubset(set(new_creds['scopes'])):
        raise HTTPException(401, 'ChatGPT session must be reauthorized')
    await replace_oauth_credentials(connection, user, new_creds, db=db)
    return new_creds


async def get_provider_access_token(connection: ProviderConnection, user) -> str:
    """Return a live OAuth token, serializing rotating refreshes by connection."""
    initial = await get_oauth_credentials(connection, user)
    if not initial or not initial.get('access_token'):
        raise HTTPException(401, 'Reconnect this ChatGPT account')
    if int(initial.get('expires_at', 0)) > int(time.time()) + 90:
        return initial['access_token']
    lock = _refresh_lock(connection.id)
    async with lock:
        async with get_async_db_context() as db:
            # A no-op UPDATE acquires a row/write lock on both PostgreSQL and
            # SQLite. PostgreSQL's FOR UPDATE is still used on the following
            # read; SQLite uses its database writer lock across worker processes.
            await db.execute(
                update(ProviderConnection)
                .where(ProviderConnection.id == connection.id, ProviderConnection.owner_id == user.id)
                .values(updated_at=ProviderConnection.updated_at)
            )
            row = await db.scalar(
                select(ProviderConnection).where(ProviderConnection.id == connection.id).with_for_update()
            )
            if not row or row.owner_id != user.id or row.provider_id != 'chatgpt':
                raise HTTPException(404, 'Provider connection not found')
            credentials = await get_oauth_credentials(row, user, db=db)
            if not credentials or not credentials.get('access_token'):
                raise HTTPException(401, 'Reconnect this ChatGPT account')
            if int(credentials.get('expires_at', 0)) <= int(time.time()) + 90:
                credentials = await _refresh_credentials(row, user, db)
            await db.commit()
            return credentials['access_token']


async def _api_json(url: str, token: str, *, method='GET', payload=None):
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
            async with session.request(method, url, json=payload, headers=headers, allow_redirects=False) as response:
                raw = await read_bounded_response(response)
                if len(raw) > 2_000_000:
                    raise HTTPException(502, 'ChatGPT response is too large')
                if response.status in (401, 403):
                    raise HTTPException(401, 'ChatGPT rejected the connected account')
                if response.status == 402:
                    raise HTTPException(402, 'ChatGPT plan usage is unavailable. Review your plan and usage.')
                if response.status == 429:
                    raise HTTPException(429, 'ChatGPT usage limit reached. Review your plan usage and retry later.')
                if response.status < 200 or response.status >= 300:
                    raise HTTPException(502, f'ChatGPT request failed with HTTP {response.status}')
                return json.loads(raw) if raw else {}
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(502, 'Could not reach ChatGPT') from None


async def discover_chatgpt_models(connection: ProviderConnection, user) -> list[dict[str, str]]:
    token = await get_provider_access_token(connection, user)
    payload = await _api_json(f'{OPENAI_API}/models', token)
    records = payload.get('models', []) if isinstance(payload, dict) else []
    return [
        {'id': item['slug'], 'name': item.get('display_name') or item['slug']}
        for item in records
        if isinstance(item, dict)
        and isinstance(item.get('slug'), str)
        and item.get('slug')
        and item.get('visibility') == 'list'
    ]


def build_chatgpt_responses_payload(payload: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Translate supported chat input to SIWC's current Responses contract."""
    from open_webui.routers.openai import convert_to_responses_payload

    body = convert_to_responses_payload(dict(payload))
    body['model'] = model_id
    body['store'] = False
    body['stream'] = True
    # SIWC preview currently rejects these fields, including normal Chat
    # Completions sampling and output-token parameters.
    for unsupported in (
        'background',
        'conversation',
        'max_output_tokens',
        'max_tool_calls',
        'metadata',
        'moderation',
        'multi_agent',
        'prompt',
        'prompt_cache_retention',
        'safety_identifier',
        'temperature',
        'top_logprobs',
        'top_p',
        'truncation',
        'user',
        'previous_response_id',
        'max_tokens',
        'max_completion_tokens',
        'stream_options',
        'logit_bias',
        'frequency_penalty',
        'presence_penalty',
        'stop',
    ):
        body.pop(unsupported, None)
    if isinstance(body.get('tools'), list):
        supported_types = {'function', 'web_search', 'web_search_preview'}
        body['tools'] = [
            tool for tool in body['tools'] if isinstance(tool, dict) and tool.get('type') in supported_types
        ]
    return body


async def chatgpt_chat_completion(connection: ProviderConnection, model_id: str, payload: dict[str, Any], user):
    """Create a non-stored, streamed Responses API request.

    Callers consume the returned ``aiohttp.ClientResponse`` and close its session.
    ``store=false`` and ``stream=true`` are enforced regardless of incoming fields.
    """
    token = await get_provider_access_token(connection, user)
    body = build_chatgpt_responses_payload(payload, model_id)
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=300), trust_env=False
    )
    try:
        response = await session.post(f'{OPENAI_API}/responses', json=body, headers=headers, allow_redirects=False)
    except (aiohttp.ClientError, TimeoutError):
        await session.close()
        raise HTTPException(502, 'Could not reach ChatGPT') from None
    if response.status < 200 or response.status >= 300:
        status_code = response.status
        await response.release()
        await session.close()
        if status_code in (401, 403):
            raise HTTPException(401, 'ChatGPT rejected the connected account')
        if status_code == 402:
            raise HTTPException(402, 'ChatGPT plan usage is unavailable. Review your plan and usage.')
        if status_code == 429:
            raise HTTPException(429, 'ChatGPT usage limit reached. Review your plan usage and retry later.')
        raise HTTPException(502, f'ChatGPT request failed with HTTP {status_code}')
    return response, session
