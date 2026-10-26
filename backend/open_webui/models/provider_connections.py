"""User and administrator configured model provider connections."""

import time
import uuid
from typing import Literal

from open_webui.internal.db import Base
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Text


class ProviderConnection(Base):
    __tablename__ = 'provider_connection'
    id = Column(Text, primary_key=True)
    provider_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    auth_type = Column(Text, nullable=False, default='api_key')
    visibility = Column(Text, nullable=False, default='private')
    owner_id = Column(Text, nullable=False)
    config = Column(JSON, nullable=False, default=dict)
    credential = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default='configured')
    models = Column(JSON, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        Index('idx_provider_connection_owner', 'owner_id'),
        Index('idx_provider_connection_visibility', 'visibility'),
    )


class ProviderConnectionCreate(BaseModel):
    provider_id: Literal['openai', 'deepseek', 'bailian', 'custom', 'chatgpt', 'kimi', 'siliconflow']
    name: str = Field(min_length=1, max_length=100)
    auth_type: Literal['api_key', 'oauth'] = 'api_key'
    visibility: Literal['private', 'shared'] = 'private'
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    config: dict = Field(default_factory=dict)
    access_control: list[dict] = Field(default_factory=list)


class ProviderConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=100)
    visibility: Literal['private', 'shared'] | None = None
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    config: dict | None = None
    access_control: list[dict] | None = None


class ProviderConnectionResponse(BaseModel):
    id: str
    provider_id: str
    name: str
    auth_type: str
    visibility: str
    owner_id: str
    config: dict
    status: str
    models: list[dict]
    configured: bool
    created_at: int
    updated_at: int


def new_provider_connection(**values):
    now = int(time.time())
    return ProviderConnection(id=str(uuid.uuid4()), created_at=now, updated_at=now, **values)


class ProviderCatalogLease(Base):
    __tablename__ = 'provider_catalog_lease'
    id = Column(Text, primary_key=True)
    token = Column(Text, nullable=False)
    expires_at = Column(BigInteger, nullable=False)
