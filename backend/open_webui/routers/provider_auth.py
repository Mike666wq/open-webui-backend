"""Private ChatGPT browser authorization and legacy local-helper pairing."""

from __future__ import annotations

import hashlib
import secrets
import shlex
import time
import uuid
from typing import Literal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from open_webui.internal.db import Base, get_async_db_context, get_async_session
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.auth import get_verified_user
from open_webui.utils.provider_errors import is_provider_error, provider_error_response
from open_webui.utils.chatgpt_auth import (
    REQUIRED_SCOPES,
    build_authorize_url,
    exchange_authorization_code,
    pkce_challenge,
    normalize_token_response,
    revoke_chatgpt_session,
    validate_id_token,
)
from open_webui.utils.providers import (
    create_provider_connection,
    encrypt_credential,
    decrypt_credential,
    delete_provider_connection,
    get_oauth_credentials,
    get_provider_connection,
    list_provider_connections,
    replace_oauth_credentials,
)
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Index, String, Text, delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from open_webui.utils.rate_limit import RateLimiter

router = APIRouter()

ATTEMPT_TTL_SECONDS = 10 * 60
_CODE_ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
pairing_rate_limiter = RateLimiter(limit=12, window=60)


class ChatGPTAuthAttempt(Base):
    __tablename__ = 'chatgpt_auth_attempt'
    id = Column(String, primary_key=True)
    owner_id = Column(Text, nullable=False)
    status = Column(String, nullable=False, default='pending')
    pairing_code_hash = Column(String, nullable=False)
    helper_token_hash = Column(String, nullable=True)
    host_id = Column(String, nullable=False)
    connection_id = Column(String, nullable=True)
    expected_subject = Column(String, nullable=True)
    expected_client_id = Column(String, nullable=True)
    state = Column(String, nullable=True)
    nonce = Column(String, nullable=True)
    code_challenge = Column(String, nullable=True)
    pkce_verifier = Column(Text, nullable=True)
    redirect_uri = Column(String, nullable=True)
    client_id = Column(String, nullable=True)
    expires_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        Index('idx_chatgpt_auth_attempt_owner', 'owner_id', 'created_at'),
        Index('idx_chatgpt_auth_attempt_expiry', 'expires_at'),
    )


class ChatGPTAuthHost(Base):
    __tablename__ = 'chatgpt_auth_host'
    id = Column(String, primary_key=True)
    host_id = Column(String, nullable=False, unique=True)


async def _stable_host_id(db: AsyncSession) -> str:
    row = await db.scalar(select(ChatGPTAuthHost).where(ChatGPTAuthHost.id == 'singleton').with_for_update())
    if row:
        return row.host_id
    host_id = f'urn:uuid:{uuid.uuid4()}'
    db.add(ChatGPTAuthHost(id='singleton', host_id=host_id))
    try:
        await db.commit()
        return host_id
    except IntegrityError:
        await db.rollback()
        row = await db.scalar(select(ChatGPTAuthHost).where(ChatGPTAuthHost.id == 'singleton'))
        if not row:
            raise
        return row.host_id


class StartAuthForm(BaseModel):
    connection_id: str | None = None
    mode: Literal['helper', 'browser'] = 'helper'


class BrowserCallbackForm(BaseModel):
    callback_url: str = Field(min_length=30, max_length=8192)


class PairForm(BaseModel):
    pairing_code: str = Field(min_length=8, max_length=32)
    state: str = Field(min_length=24, max_length=256)
    nonce: str = Field(min_length=24, max_length=256)
    code_challenge: str = Field(min_length=40, max_length=128)
    redirect_uri: str = Field(min_length=20, max_length=256)


class CompleteForm(BaseModel):
    state: str = Field(min_length=24, max_length=256)
    client_id: str = Field(min_length=3, max_length=256)
    token: dict
    granted_scope: str | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pairing_code() -> str:
    return ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(12))


def _safe_redirect_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == 'http'
            and parsed.hostname == '127.0.0.1'
            and parsed.port is not None
            and parsed.path == '/auth/callback'
            and not parsed.query
            and not parsed.fragment
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _valid_issued_client_id(attempt: ChatGPTAuthAttempt, client_id: str) -> bool:
    if attempt.expected_client_id:
        return client_id == attempt.expected_client_id
    if attempt.client_id == 'dynamic_agent_client':
        return client_id.startswith('oaiapp_') and len(client_id) <= 256
    return client_id == attempt.client_id


def _public_attempt(row: ChatGPTAuthAttempt) -> dict:
    status = row.status
    if status == 'processing':
        status = 'pending'
    if status in ('pending', 'processing') and row.expires_at <= int(time.time()):
        status = 'expired'
    return {
        'status': status,
        'expires_at': row.expires_at,
        'connection_id': row.connection_id if row.status == 'complete' else None,
    }


@router.post('/start')
async def start_chatgpt_auth(
    request: Request,
    form: StartAuthForm | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """Create an owner-bound browser login; retain helper support for older clients."""
    connection = None
    host_id = await _stable_host_id(db)
    if form and form.connection_id:
        connection = await get_provider_connection(form.connection_id, user, db=db)
        if not connection or connection.provider_id != 'chatgpt' or connection.auth_type != 'oauth':
            raise HTTPException(404, 'ChatGPT connection not found')
        creds = await get_oauth_credentials(connection, user, db=db)
        registration = connection.config or {}
        expected_subject = (creds.get('subject') if creds else None) or registration.get('subject')
        expected_client_id = (creds.get('client_id') if creds else None) or registration.get('client_id')
        host_id = registration.get('host_id') or host_id
    else:
        expected_subject = expected_client_id = None

    attempt_id = str(uuid.uuid4())
    pairing_code = _pairing_code()
    now = int(time.time())
    attempt = ChatGPTAuthAttempt(
        id=attempt_id,
        owner_id=user.id,
        status='pending',
        pairing_code_hash=_hash(pairing_code),
        host_id=host_id,
        connection_id=connection.id if connection else None,
        expected_subject=expected_subject,
        expected_client_id=expected_client_id,
        expires_at=now + ATTEMPT_TTL_SECONDS,
        created_at=now,
    )
    browser = form and form.mode == 'browser'
    if browser:
        verifier = secrets.token_urlsafe(48)
        attempt.state = secrets.token_urlsafe(32)
        attempt.nonce = secrets.token_urlsafe(32)
        attempt.pkce_verifier = encrypt_credential(verifier)
        attempt.code_challenge = pkce_challenge(verifier)
        attempt.redirect_uri = 'http://127.0.0.1:1455/auth/callback'
        attempt.client_id = expected_client_id or 'dynamic_agent_client'
        attempt.pairing_code_hash = ''
    db.add(attempt)
    await db.execute(delete(ChatGPTAuthAttempt).where(ChatGPTAuthAttempt.expires_at < now - 24 * 60 * 60))
    await db.commit()
    if browser:
        return {
            'attempt_id': attempt_id,
            'expires_at': attempt.expires_at,
            'authorization_url': build_authorize_url(
                state=attempt.state, nonce=attempt.nonce,
                code_challenge=attempt.code_challenge, host_id=attempt.host_id,
                client_id=attempt.client_id, redirect_uri=attempt.redirect_uri,
            ),
            'redirect_uri': attempt.redirect_uri,
        }
    backend_url = str(request.base_url).rstrip('/') if request else ''
    helper_url = f'{backend_url}/api/v1/providers/auth/chatgpt/helper'
    command = (
        f'curl --fail --silent --show-error --output chatgpt-authorize.py {shlex.quote(helper_url)} '
        f'&& python3 chatgpt-authorize.py --server-url {shlex.quote(backend_url)}'
    )
    return {
        'attempt_id': attempt_id,
        'pairing_code': pairing_code,
        'expires_at': attempt.expires_at,
        'helper_url': helper_url,
        'command': command,
    }


@router.post('/pair')
async def pair_local_helper(form: PairForm, request: Request, db: AsyncSession = Depends(get_async_session)):
    """Claim a pairing code and bind the local PKCE attempt to its owner."""
    if not _safe_redirect_uri(form.redirect_uri):
        raise HTTPException(400, 'The helper callback must use 127.0.0.1/auth/callback')
    caller = request.client.host if request.client else 'unknown'
    if await pairing_rate_limiter.is_limited(request.app.state.redis, caller):
        raise HTTPException(429, 'Too many pairing attempts. Wait a minute and try again.')
    now = int(time.time())
    pairing_hash = _hash(form.pairing_code.upper())
    attempt = await db.scalar(
        select(ChatGPTAuthAttempt)
        .where(
            ChatGPTAuthAttempt.pairing_code_hash == pairing_hash,
            ChatGPTAuthAttempt.status == 'pending',
            ChatGPTAuthAttempt.expires_at > now,
        )
        .with_for_update()
    )
    if not attempt or attempt.state:
        raise HTTPException(404, 'Pairing code is invalid, expired, or already used')
    helper_token = secrets.token_urlsafe(32)
    client_id = attempt.expected_client_id or 'dynamic_agent_client'
    try:
        claimed = await db.execute(
            update(ChatGPTAuthAttempt)
            .where(
                ChatGPTAuthAttempt.id == attempt.id,
                ChatGPTAuthAttempt.pairing_code_hash == pairing_hash,
                ChatGPTAuthAttempt.status == 'pending',
                ChatGPTAuthAttempt.expires_at > now,
                ChatGPTAuthAttempt.state.is_(None),
            )
            .values(
                helper_token_hash=_hash(helper_token),
                state=form.state,
                nonce=form.nonce,
                code_challenge=form.code_challenge,
                redirect_uri=form.redirect_uri,
                client_id=client_id,
                pairing_code_hash='',
            )
        )
    except OperationalError:
        await db.rollback()
        raise HTTPException(404, 'Pairing code is invalid, expired, or already used') from None
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(404, 'Pairing code is invalid, expired, or already used')
    await db.commit()
    id_token_hint = None
    if attempt.connection_id:
        owner = SimpleNamespace(id=attempt.owner_id)
        existing_connection = await get_provider_connection(attempt.connection_id, owner, db=db)
        existing_credentials = await get_oauth_credentials(existing_connection, owner, db=db)
        id_token_hint = existing_credentials.get('id_token')
    return {
        'attempt_id': attempt.id,
        'helper_token': helper_token,
        'host_id': attempt.host_id,
        'client_id': client_id,
        'agent_name': 'Open WebUI',
        'id_token_hint': id_token_hint,
    }


@router.get('/helper')
async def download_chatgpt_helper():
    """Serve the stdlib-only local helper used by the pairing flow."""
    from pathlib import Path

    helper = Path(__file__).resolve().parents[3] / 'scripts' / 'chatgpt-authorize.py'
    if not helper.is_file():
        raise HTTPException(404, 'The local ChatGPT helper is not installed')
    return FileResponse(helper, media_type='text/x-python', filename='chatgpt-authorize.py')


@router.post('/complete')
async def complete_chatgpt_auth(
    form: CompleteForm,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_session),
):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Local helper authorization is required')
    helper_token = authorization[7:]
    helper_hash = _hash(helper_token)
    now = int(time.time())
    attempt = await db.scalar(select(ChatGPTAuthAttempt).where(ChatGPTAuthAttempt.helper_token_hash == helper_hash))
    if not attempt or attempt.status != 'pending' or attempt.expires_at <= now:
        raise HTTPException(404, 'Authorization attempt is invalid or expired')
    consumed = await db.execute(
        update(ChatGPTAuthAttempt)
        .where(
            ChatGPTAuthAttempt.id == attempt.id,
            ChatGPTAuthAttempt.helper_token_hash == helper_hash,
            ChatGPTAuthAttempt.status == 'pending',
            ChatGPTAuthAttempt.expires_at > now,
        )
        .values(helper_token_hash='', status='processing')
    )
    if consumed.rowcount != 1:
        await db.rollback()
        raise HTTPException(404, 'Authorization attempt is invalid or expired')
    await db.commit()
    try:
        return await _finish_chatgpt_auth(attempt, form, db)
    except HTTPException as exc:
        if not is_provider_error(exc):
            raise
        return provider_error_response(exc)


async def _finish_chatgpt_auth(attempt: ChatGPTAuthAttempt, form: CompleteForm, db: AsyncSession):
    if not attempt.state or not secrets.compare_digest(attempt.state.encode(), form.state.encode()):
        attempt.status = 'failed'
        await db.commit()
        raise HTTPException(400, 'Authorization state did not match')
    if not _valid_issued_client_id(attempt, form.client_id):
        attempt.status = 'failed'
        await db.commit()
        raise HTTPException(400, 'ChatGPT returned an unexpected client registration')
    token = form.token
    if not isinstance(token, dict) or not isinstance(token.get('id_token'), str):
        attempt.status = 'failed'
        await db.commit()
        raise HTTPException(400, 'The local helper did not provide a valid token response')
    try:
        claims = await validate_id_token(token['id_token'], client_id=form.client_id, nonce=attempt.nonce)
        if attempt.expected_subject and claims['sub'] != attempt.expected_subject:
            raise HTTPException(400, 'The selected ChatGPT account changed during reauthorization')
        credentials = normalize_token_response(
            token,
            claims,
            client_id=form.client_id,
            host_id=attempt.host_id,
            granted_scope=form.granted_scope,
        )
        if not REQUIRED_SCOPES.issubset(set(credentials['scopes'])):
            raise HTTPException(403, 'ChatGPT did not grant all required permissions')
    except Exception:
        attempt.status = 'failed'
        await db.commit()
        raise
    # Recheck under a database writer lock after the network exchange/validation.
    # Cancellation or expiry must never save a late token response.
    active = await db.execute(
        update(ChatGPTAuthAttempt)
        .where(ChatGPTAuthAttempt.id == attempt.id,
               ChatGPTAuthAttempt.status == 'processing',
               ChatGPTAuthAttempt.expires_at > int(time.time()))
        .values(pkce_verifier=None)
    )
    if active.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, 'Authorization was cancelled or expired. Start a new sign-in.')
    from open_webui.models.users import Users

    owner = await Users.get_user_by_id(attempt.owner_id, db=db)
    if not owner:
        attempt.status = 'failed'
        await db.commit()
        raise HTTPException(404, 'Owner account no longer exists')
    connection = None
    config = {
        'host_id': attempt.host_id,
        'client_id': form.client_id,
        'subject': credentials['subject'],
        'model_ids': [],
    }
    if attempt.connection_id:
        connection = await get_provider_connection(attempt.connection_id, owner, db=db)
        if not connection or connection.owner_id != attempt.owner_id:
            raise HTTPException(404, 'ChatGPT connection not found')
        connection.config = {**(connection.config or {}), **config}
        await replace_oauth_credentials(connection, owner, credentials, db=db)
    else:
        # Use the authenticated owner captured when the pairing attempt began;
        # helper possession alone never grants account ownership.
        label = credentials.get('email') or credentials.get('subject', 'account')[:12]
        connection = await create_provider_connection(
            owner,
            config=config,
            db=db,
            credentials=credentials,
        )
        connection.name = f'ChatGPT ({label})'
        connection.provider_id = 'chatgpt'
        connection.auth_type = 'oauth'
        connection.visibility = 'private'
        connection.owner_id = attempt.owner_id
    attempt.connection_id = connection.id
    attempt.status = 'complete'
    attempt.helper_token_hash = None
    await db.commit()
    return {'status': 'complete', 'connection_id': connection.id}


def _parse_browser_callback(value: str, attempt: ChatGPTAuthAttempt) -> dict[str, str]:
    try:
        parsed = urlsplit(value.strip())
        expected = urlsplit(attempt.redirect_uri)
        if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
            raise ValueError()
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=32)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError()
        result = {key: values[0] for key, values in query.items()}
    except (ValueError, TypeError):
        raise HTTPException(400, 'Paste the complete callback URL shown after ChatGPT sign-in.') from None
    if not secrets.compare_digest(result.get('state', '').encode(), (attempt.state or '').encode()):
        raise HTTPException(400, 'This callback belongs to a different sign-in. Use the current authorization page.')
    if result.get('error'):
        return result
    client_id = result.get('client_id') or attempt.expected_client_id
    if not result.get('code') or not client_id or not _valid_issued_client_id(attempt, client_id):
        raise HTTPException(400, 'The callback is missing its code or has an unexpected client registration.')
    result['client_id'] = client_id
    return result


@router.post('/{attempt_id}/callback')
async def complete_browser_auth(
    attempt_id: str,
    form: BrowserCallbackForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    attempt = await db.scalar(select(ChatGPTAuthAttempt).where(
        ChatGPTAuthAttempt.id == attempt_id, ChatGPTAuthAttempt.owner_id == user.id,
    ))
    if not attempt or attempt.status != 'pending' or attempt.expires_at <= int(time.time()) or not attempt.pkce_verifier:
        raise HTTPException(404, 'Authorization attempt is invalid, expired, or already used')
    callback = _parse_browser_callback(form.callback_url, attempt)
    verifier = decrypt_credential(attempt.pkce_verifier)
    consumed = await db.execute(update(ChatGPTAuthAttempt).where(
        ChatGPTAuthAttempt.id == attempt.id, ChatGPTAuthAttempt.owner_id == user.id,
        ChatGPTAuthAttempt.status == 'pending', ChatGPTAuthAttempt.pkce_verifier.is_not(None),
        ChatGPTAuthAttempt.expires_at > int(time.time()),
    ).values(status='failed' if callback.get('error') else 'processing', pkce_verifier=None))
    if consumed.rowcount != 1:
        await db.rollback()
        raise HTTPException(404, 'Authorization attempt is invalid, expired, or already used')
    await db.commit()
    if callback.get('error'):
        raise HTTPException(400, 'ChatGPT authorization was declined. Start a new sign-in to try again.')
    try:
        token = await exchange_authorization_code(
            code=callback['code'], client_id=callback['client_id'],
            verifier=verifier, redirect_uri=attempt.redirect_uri,
        )
        return await _finish_chatgpt_auth(attempt, CompleteForm(
            state=callback['state'], client_id=callback['client_id'], token=token,
            granted_scope=callback.get('scope'),
        ), db)
    except Exception as exc:
        await db.rollback()
        await db.execute(update(ChatGPTAuthAttempt).where(
            ChatGPTAuthAttempt.id == attempt_id, ChatGPTAuthAttempt.status == 'processing',
        ).values(status='failed', pkce_verifier=None))
        await db.commit()
        # A rejected or expired token keeps the taxonomy: the browser needs to
        # tell "sign in again" apart from "the provider is unreachable".
        if isinstance(exc, HTTPException) and is_provider_error(exc):
            return provider_error_response(exc)
        raise


@router.get('/{attempt_id}')
async def get_chatgpt_auth_status(
    attempt_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    attempt = await db.scalar(
        select(ChatGPTAuthAttempt).where(ChatGPTAuthAttempt.id == attempt_id, ChatGPTAuthAttempt.owner_id == user.id)
    )
    if not attempt:
        raise HTTPException(404, 'Authorization attempt not found')
    if attempt.status in ('pending', 'processing') and attempt.expires_at <= int(time.time()):
        attempt.status = 'expired'
        attempt.helper_token_hash = None
        attempt.pkce_verifier = None
        await db.commit()
    return _public_attempt(attempt)


@router.delete('/{attempt_id}')
async def cancel_chatgpt_auth(
    attempt_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    attempt = await db.scalar(
        select(ChatGPTAuthAttempt)
        .where(ChatGPTAuthAttempt.id == attempt_id, ChatGPTAuthAttempt.owner_id == user.id)
        .with_for_update()
    )
    if not attempt:
        raise HTTPException(404, 'Authorization attempt not found')
    if attempt.status == 'complete':
        return {'status': 'complete', 'connection_id': attempt.connection_id}
    attempt.status = 'failed'
    attempt.helper_token_hash = None
    attempt.pairing_code_hash = ''
    attempt.pkce_verifier = None
    await db.commit()
    return {'status': 'cancelled'}


@router.delete('/connection/{connection_id}')
async def disconnect_chatgpt(
    connection_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    connection = await get_provider_connection(connection_id, user, db=db)
    # Serialize disconnect with refresh so revocation targets the latest rotated
    # refresh token rather than a token superseded by another worker.
    await db.execute(
        update(ProviderConnection)
        .where(ProviderConnection.id == connection_id, ProviderConnection.owner_id == user.id)
        .values(updated_at=ProviderConnection.updated_at)
    )
    await db.refresh(connection)
    credentials = await get_oauth_credentials(connection, user, db=db)
    revocation_confirmed = await revoke_chatgpt_session(credentials)
    connection.config = {
        **(connection.config or {}),
        'client_id': credentials.get('client_id') or (connection.config or {}).get('client_id'),
        'subject': credentials.get('subject') or (connection.config or {}).get('subject'),
        'host_id': credentials.get('host_id') or (connection.config or {}).get('host_id'),
    }
    await db.commit()
    await delete_provider_connection(connection_id, user, db=db)
    return {'status': 'disconnected', 'revocation_confirmed': revocation_confirmed}
