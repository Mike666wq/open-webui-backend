"""Kimi Code device authorization and OpenAI-compatible OAuth transport."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from fastapi import HTTPException
from open_webui.internal.db import get_async_db_context
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.providers import (
    decrypt_credential,
    get_oauth_credentials,
    kimi_device_headers,
    provider_http_error,
    read_bounded_response,
    replace_oauth_credentials,
)
from sqlalchemy import select, update

log = logging.getLogger(__name__)

KIMI_OAUTH_HOST = os.getenv('KIMI_CODE_OAUTH_HOST', 'https://auth.kimi.com').rstrip('/')
KIMI_CLIENT_ID = '17e5f671-d194-4dfb-9706-5516cb48c098'
DEVICE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code'
_refresh_locks: dict[str, asyncio.Lock] = {}


def _refresh_lock(connection_id: str) -> asyncio.Lock:
    lock = _refresh_locks.get(connection_id)
    if lock is None:
        lock = _refresh_locks[connection_id] = asyncio.Lock()
    return lock


def _validate_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError()
        return value.rstrip('/')
    except ValueError:
        raise HTTPException(500, 'Kimi OAuth endpoint configuration is invalid') from None


async def _form_request(
    path: str,
    data: dict[str, str],
    *,
    device_id: str | None = None,
    retries: int = 1,
) -> tuple[int, dict[str, Any]]:
    endpoint = f'{_validate_https_url(KIMI_OAUTH_HOST)}{path}'
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                trust_env=False,
            ) as session:
                async with session.post(
                    endpoint,
                    data=data,
                    headers=kimi_device_headers(device_id),
                    allow_redirects=False,
                ) as response:
                    raw = await read_bounded_response(response, 256_000)
                    status = response.status
            try:
                payload = json.loads(raw) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(502, 'Kimi returned an invalid OAuth response') from None
            if not isinstance(payload, dict):
                raise HTTPException(502, 'Kimi returned an invalid OAuth response')
            if status not in {429, 500, 502, 503, 504} or attempt == retries - 1:
                return status, payload
        except HTTPException:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries - 1:
                break
        await asyncio.sleep(0.25 * (2**attempt))
    raise HTTPException(502, 'Could not reach Kimi authorization') from last_error


async def request_device_authorization(device_id: str | None = None) -> dict[str, Any]:
    status, payload = await _form_request(
        '/api/oauth/device_authorization',
        {'client_id': KIMI_CLIENT_ID},
        device_id=device_id,
    )
    if status != 200:
        if status in (401, 403):
            raise HTTPException(401, 'Kimi rejected the device authorization request')
        raise provider_http_error(status)
    required = ('user_code', 'device_code', 'verification_uri_complete')
    if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
        raise HTTPException(502, 'Kimi returned an incomplete device authorization response')
    expires_in = payload.get('expires_in')
    interval = payload.get('interval', 5)
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or not math.isfinite(expires_in)
        or expires_in < 1
    ):
        raise HTTPException(502, 'Kimi returned an invalid device authorization lifetime')
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or not 1 <= interval <= 300
    ):
        raise HTTPException(502, 'Kimi returned an invalid polling interval')
    verification_uri = payload.get('verification_uri') or payload['verification_uri_complete']
    for value in (verification_uri, payload['verification_uri_complete']):
        if not isinstance(value, str):
            raise HTTPException(502, 'Kimi returned an invalid verification URL')
        parsed = urlsplit(value)
        if (
            parsed.scheme != 'https'
            or parsed.hostname
            not in {'auth.kimi.com', 'www.kimi.com', 'kimi.com', 'auth.kimi.ai', 'www.kimi.ai', 'kimi.ai'}
            or parsed.username
            or parsed.password
        ):
            raise HTTPException(502, 'Kimi returned an invalid verification URL')
    return {
        'user_code': payload['user_code'],
        'device_code': payload['device_code'],
        'verification_uri': verification_uri,
        'verification_uri_complete': payload['verification_uri_complete'],
        'expires_in': int(expires_in),
        'interval': max(1, int(interval)),
    }


async def poll_device_authorization(device_code: str, device_id: str | None = None) -> tuple[int, dict[str, Any]]:
    return await _form_request(
        '/api/oauth/token',
        {
            'client_id': KIMI_CLIENT_ID,
            'device_code': device_code,
            'grant_type': DEVICE_GRANT_TYPE,
        },
        device_id=device_id,
        retries=1,
    )


def normalize_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    access_token = payload.get('access_token')
    refresh_token = payload.get('refresh_token')
    expires_in = payload.get('expires_in')
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(502, 'Kimi did not return an access token')
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(401, 'Kimi did not return a renewable authorized session')
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or not math.isfinite(expires_in)
        or expires_in < 1
    ):
        raise HTTPException(502, 'Kimi returned an invalid token lifetime')
    scope = payload.get('scope')
    if scope is not None and not isinstance(scope, str):
        raise HTTPException(502, 'Kimi returned invalid permission scopes')
    token_type = payload.get('token_type')
    if token_type is not None and not isinstance(token_type, str):
        raise HTTPException(502, 'Kimi returned an invalid token type')
    return {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': token_type or 'Bearer',
        'scopes': sorted(set((scope or '').split())),
        'expires_at': int(time.time() + expires_in),
    }


async def _refresh_credentials(connection: ProviderConnection, user, db) -> dict[str, Any]:
    credentials = await get_oauth_credentials(connection, user, db=db, provider_id='kimi')
    refresh_token = credentials.get('refresh_token')
    if not refresh_token:
        raise HTTPException(401, 'Reconnect this Kimi Code account')
    status, payload = await _form_request(
        '/api/oauth/token',
        {
            'client_id': KIMI_CLIENT_ID,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
        },
        device_id=(connection.config or {}).get('device_id'),
        retries=3,
    )
    if status in (401, 403):
        raise HTTPException(401, 'Reconnect this Kimi Code account')
    if status != 200:
        raise provider_http_error(status)
    if payload.get('refresh_token') is None:
        payload['refresh_token'] = refresh_token
    if payload.get('scope') is None:
        payload['scope'] = ' '.join(credentials.get('scopes') or [])
    refreshed = normalize_token_response(payload)
    await replace_oauth_credentials(connection, user, refreshed, db=db, provider_id='kimi')
    return refreshed


async def get_provider_access_token(connection: ProviderConnection, user) -> str:
    initial = await get_oauth_credentials(connection, user, provider_id='kimi')
    if not initial or not initial.get('access_token'):
        raise HTTPException(401, 'Reconnect this Kimi Code account')
    if int(initial.get('expires_at', 0)) > int(time.time()) + 90:
        return initial['access_token']
    lock = _refresh_lock(connection.id)
    async with lock:
        async with get_async_db_context() as db:
            await db.execute(
                update(ProviderConnection)
                .where(ProviderConnection.id == connection.id, ProviderConnection.owner_id == user.id)
                .values(updated_at=ProviderConnection.updated_at)
            )
            row = await db.scalar(
                select(ProviderConnection).where(ProviderConnection.id == connection.id).with_for_update()
            )
            if not row or row.owner_id != user.id or row.provider_id != 'kimi':
                raise HTTPException(404, 'Provider connection not found')
            credentials = await get_oauth_credentials(row, user, db=db, provider_id='kimi')
            if not credentials or not credentials.get('access_token'):
                raise HTTPException(401, 'Reconnect this Kimi Code account')
            if int(credentials.get('expires_at', 0)) <= int(time.time()) + 90:
                credentials = await _refresh_credentials(row, user, db)
            await db.commit()
            return credentials['access_token']


async def device_code_from_attempt(encrypted_value: str | None) -> str:
    value = decrypt_credential(encrypted_value)
    if not value:
        raise HTTPException(502, 'Kimi device authorization could not be read')
    return value
