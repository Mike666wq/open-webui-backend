"""Minimal invocation receipts. No prompts, credentials or upstream bodies."""

import time
import uuid

from open_webui.internal.db import Base, get_async_db_context
from sqlalchemy import JSON, BigInteger, Column, Index, Text, delete


class ProviderCall(Base):
    __tablename__ = 'provider_call'
    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    connection_id = Column(Text, nullable=True)
    model_id = Column(Text, nullable=False)
    chat_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)
    purpose = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    created_at = Column(BigInteger, nullable=False)
    elapsed_ms = Column(BigInteger, nullable=False)
    usage = Column(JSON, nullable=True)
    __table_args__ = (
        Index('idx_provider_call_owner_time', 'user_id', 'created_at'),
        Index('idx_provider_call_time', 'created_at'),
    )


def reported_usage(value):
    if not isinstance(value, dict):
        return None
    result = {}
    for key, alternate in [
        ('input_tokens', 'prompt_tokens'),
        ('output_tokens', 'completion_tokens'),
        ('total_tokens', 'total_tokens'),
    ]:
        count = value.get(key, value.get(alternate))
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    for target, sources, key in [
        ('reasoning_tokens', ('output_tokens_details', 'completion_tokens_details'), 'reasoning_tokens'),
        ('cached_tokens', ('input_tokens_details', 'prompt_tokens_details'), 'cached_tokens'),
    ]:
        for source in sources:
            count = (value.get(source) or {}).get(key)
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                result[target] = count
                break
    # Media services are metered in their own units. A missing measurement
    # stays missing: a fabricated token count or fee would be a lie.
    for key in ('duration_seconds', 'characters', 'images'):
        count = value.get(key)
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0:
            result[key] = count
    return result or None


async def save_call(**values):
    async with get_async_db_context() as db:
        db.add(ProviderCall(id=str(uuid.uuid4()), **values))
        await db.execute(delete(ProviderCall).where(ProviderCall.created_at < int(time.time()) - 30 * 86400))
        await db.commit()


async def cleanup_calls():
    from open_webui.models.config import Config

    async with get_async_db_context() as db:
        cutoff = int(time.time()) - 30 * 86400
        await db.execute(delete(ProviderCall).where(ProviderCall.created_at < cutoff))
        await db.execute(
            delete(Config).where(Config.key.startswith('providers.legacy_catalog.'), Config.updated_at < cutoff)
        )
        await db.commit()


async def call_retention_worker():
    import asyncio
    import logging

    while True:
        try:
            await cleanup_calls()
        except Exception:
            logging.getLogger(__name__).warning('Invocation receipt retention cleanup failed')
        await asyncio.sleep(3600)
