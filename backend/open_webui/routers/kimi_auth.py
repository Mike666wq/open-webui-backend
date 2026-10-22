"""Owner-scoped Kimi Code OAuth device authorization."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from open_webui.internal.db import Base, get_async_session
from open_webui.models.provider_connections import ProviderConnection
from open_webui.utils.auth import get_verified_user
from open_webui.utils.kimi_auth import (
    device_code_from_attempt,
    normalize_token_response,
    poll_device_authorization,
    request_device_authorization,
)
from open_webui.utils.providers import (
    create_provider_connection,
    delete_provider_connection,
    encrypt_credential,
    get_oauth_credentials,
    get_provider_connection,
    kimi_device_headers,
    replace_oauth_credentials,
)
from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, Index, String, Text, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

ATTEMPT_TTL_SECONDS = 15 * 60


class KimiAuthAttempt(Base):
    __tablename__ = 'kimi_auth_attempt'
    id = Column(String, primary_key=True)
    owner_id = Column(Text, nullable=False)
    status = Column(String, nullable=False, default='pending')
    device_code = Column(Text, nullable=False)
    user_code = Column(Text, nullable=False)
    verification_uri = Column(Text, nullable=False)
    verification_uri_complete = Column(Text, nullable=False)
    device_id = Column(String, nullable=False)
    poll_token = Column(String, nullable=True)
    interval = Column(BigInteger, nullable=False, default=5)
    next_poll_at = Column(BigInteger, nullable=False, default=0)
    connection_id = Column(String, nullable=True)
    error_code = Column(String, nullable=True)
    expires_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        Index('idx_kimi_auth_attempt_owner', 'owner_id', 'created_at'),
        Index('idx_kimi_auth_attempt_expiry', 'expires_at'),
    )


class StartKimiForm(BaseModel):
    connection_id: str | None = None


def _public_attempt(attempt: KimiAuthAttempt, *, include_code: bool = False) -> dict:
    result = {
        'attempt_id': attempt.id,
        'status': 'pending' if attempt.status == 'processing' else attempt.status,
        'verification_uri': attempt.verification_uri,
        'verification_uri_complete': attempt.verification_uri_complete,
        'interval': attempt.interval,
        'expires_at': attempt.expires_at,
        'connection_id': attempt.connection_id if attempt.status == 'complete' else None,
        'error': attempt.error_code,
    }
    if include_code:
        result['user_code'] = attempt.user_code
    return result


@router.post('/start')
async def start_kimi_auth(
    form: StartKimiForm | None = None,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    connection = None
    if form and form.connection_id:
        connection = await get_provider_connection(form.connection_id, user, db=db, provider_id='kimi')
    device_id = (connection.config or {}).get('device_id') if connection else None
    if not device_id:
        device_id = kimi_device_headers()['X-Msh-Device-Id']
    authorization = await request_device_authorization(device_id)
    now = int(time.time())
    attempt = KimiAuthAttempt(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        status='pending',
        device_code=encrypt_credential(authorization['device_code']),
        user_code=authorization['user_code'],
        verification_uri=authorization['verification_uri'],
        verification_uri_complete=authorization['verification_uri_complete'],
        device_id=device_id,
        interval=authorization['interval'],
        next_poll_at=now + authorization['interval'],
        connection_id=connection.id if connection else None,
        expires_at=now + min(ATTEMPT_TTL_SECONDS, authorization['expires_in']),
        created_at=now,
    )
    db.add(attempt)
    await db.execute(delete(KimiAuthAttempt).where(KimiAuthAttempt.expires_at < now - 24 * 60 * 60))
    await db.commit()
    return _public_attempt(attempt, include_code=True)


async def _complete_attempt(
    db: AsyncSession,
    attempt: KimiAuthAttempt,
    user,
    token_payload: dict,
):
    credentials = normalize_token_response(token_payload)
    config = {
        'device_id': attempt.device_id,
        'model_ids': [],
    }
    if attempt.connection_id:
        connection = await get_provider_connection(attempt.connection_id, user, db=db, provider_id='kimi')
        existing_config = dict(connection.config or {})
        existing_config.update(
            {
                'device_id': existing_config.get('device_id') or config['device_id'],
                'model_ids': existing_config.get('model_ids') or [],
            }
        )
        connection.config = existing_config
        await replace_oauth_credentials(connection, user, credentials, db=db, provider_id='kimi')
    else:
        connection = await create_provider_connection(
            user,
            config=config,
            db=db,
            credentials=credentials,
            provider_id='kimi',
            name='Kimi Code',
        )
    connection.models = []
    connection.status = 'connected'
    attempt.connection_id = connection.id
    attempt.status = 'complete'
    attempt.device_code = ''
    attempt.user_code = ''
    attempt.error_code = None
    attempt.poll_token = None
    await db.commit()


@router.get('/{attempt_id}')
async def get_kimi_auth_status(
    attempt_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    attempt = await db.scalar(
        select(KimiAuthAttempt).where(
            KimiAuthAttempt.id == attempt_id,
            KimiAuthAttempt.owner_id == user.id,
        )
    )
    if not attempt:
        raise HTTPException(404, 'Authorization attempt not found')
    now = int(time.time())
    if attempt.status in ('pending', 'processing') and attempt.expires_at <= now:
        await db.execute(
            update(KimiAuthAttempt)
            .where(KimiAuthAttempt.id == attempt.id, KimiAuthAttempt.status.in_(('pending', 'processing')))
            .values(status='expired', device_code='', user_code='', poll_token=None)
        )
        await db.commit()
        await db.refresh(attempt)
        return _public_attempt(attempt)
    if attempt.status not in ('pending', 'processing') or attempt.next_poll_at > now:
        return _public_attempt(attempt)

    # Claim a bounded lease. A crashed worker can be retried after it expires;
    # only the worker holding this unique token may persist the OAuth result.
    poll_token = str(uuid.uuid4())
    claimed = await db.execute(
        update(KimiAuthAttempt)
        .where(
            KimiAuthAttempt.id == attempt.id,
            KimiAuthAttempt.owner_id == user.id,
            KimiAuthAttempt.status.in_(('pending', 'processing')),
            KimiAuthAttempt.next_poll_at <= now,
            KimiAuthAttempt.expires_at > now,
        )
        .values(status='processing', poll_token=poll_token, next_poll_at=now + 45)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        await db.refresh(attempt)
        return _public_attempt(attempt)
    await db.commit()
    await db.refresh(attempt)
    device_code = await device_code_from_attempt(attempt.device_code)
    device_id = attempt.device_id
    await db.commit()
    try:
        status, payload = await poll_device_authorization(device_code, device_id)
    except HTTPException:
        # Transient network failures leave the attempt retryable and never leak
        # provider data into the browser. Do not overwrite a concurrent cancel.
        status, payload = 503, {'error': 'temporarily_unavailable'}

    finished_at = int(time.time())
    accepted = await db.execute(
        update(KimiAuthAttempt)
        .where(
            KimiAuthAttempt.id == attempt_id,
            KimiAuthAttempt.owner_id == user.id,
            KimiAuthAttempt.status == 'processing',
            KimiAuthAttempt.poll_token == poll_token,
        )
        .values(status='pending', poll_token=None, next_poll_at=finished_at + attempt.interval)
    )
    if accepted.rowcount != 1:
        await db.rollback()
        await db.refresh(attempt)
        return _public_attempt(attempt)
    await db.refresh(attempt)
    if attempt.expires_at <= finished_at:
        attempt.status = 'expired'
        attempt.device_code = attempt.user_code = ''
        await db.commit()
        return _public_attempt(attempt)
    if status == 200 and payload.get('access_token'):
        try:
            await _complete_attempt(db, attempt, user, payload)
        except HTTPException:
            attempt.status = 'failed'
            attempt.device_code = attempt.user_code = ''
            attempt.error_code = 'invalid_token_response'
            await db.commit()
        return _public_attempt(attempt)

    error = str(payload.get('error') or 'invalid_response')
    attempt.error_code = error[:128]
    if error == 'authorization_pending':
        attempt.status = 'pending'
    elif error == 'slow_down':
        attempt.interval += 5
        attempt.next_poll_at = finished_at + attempt.interval
    elif error in ('access_denied', 'authorization_denied'):
        attempt.status = 'denied'
        attempt.device_code = ''
        attempt.user_code = ''
    elif error == 'expired_token':
        attempt.status = 'expired'
        attempt.device_code = ''
        attempt.user_code = ''
    elif status >= 500 or status == 429:
        attempt.status = 'pending'
    else:
        attempt.status = 'failed'
        attempt.device_code = ''
        attempt.user_code = ''
    await db.commit()
    return _public_attempt(attempt)


@router.delete('/{attempt_id}')
async def cancel_kimi_auth(
    attempt_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await db.execute(
        update(KimiAuthAttempt)
        .where(KimiAuthAttempt.id == attempt_id, KimiAuthAttempt.owner_id == user.id)
        .values(next_poll_at=KimiAuthAttempt.next_poll_at)
    )
    attempt = await db.scalar(
        select(KimiAuthAttempt)
        .where(
            KimiAuthAttempt.id == attempt_id,
            KimiAuthAttempt.owner_id == user.id,
        )
        .with_for_update()
    )
    if not attempt:
        raise HTTPException(404, 'Authorization attempt not found')
    if attempt.status == 'complete':
        return {'status': 'complete', 'connection_id': attempt.connection_id}
    attempt.status = 'cancelled'
    attempt.poll_token = None
    attempt.device_code = ''
    attempt.user_code = ''
    await db.commit()
    return {'status': 'cancelled'}


@router.delete('/connection/{connection_id}')
async def disconnect_kimi(
    connection_id: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    await db.execute(
        update(KimiAuthAttempt)
        .where(
            KimiAuthAttempt.connection_id == connection_id,
            KimiAuthAttempt.owner_id == user.id,
            KimiAuthAttempt.status.in_(('pending', 'processing')),
        )
        .values(status='cancelled', device_code='', user_code='', poll_token=None)
    )
    await db.execute(
        update(ProviderConnection)
        .where(ProviderConnection.id == connection_id, ProviderConnection.owner_id == user.id)
        .values(updated_at=ProviderConnection.updated_at)
    )
    connection = await get_provider_connection(connection_id, user, db=db, provider_id='kimi')
    credentials = await get_oauth_credentials(connection, user, db=db, provider_id='kimi')
    has_refresh_token = bool(credentials.get('refresh_token'))
    await delete_provider_connection(connection_id, user, db=db, provider_id='kimi')
    return {
        'status': 'disconnected',
        # Kimi does not document a public refresh-token revocation endpoint.
        'revocation_confirmed': False,
        'remote_session_may_remain': has_refresh_token,
    }
