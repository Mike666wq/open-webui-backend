"""User and administrator configured model provider connections."""

import time
import uuid
from typing import Literal

from open_webui.internal.db import Base
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import JSON, BigInteger, Boolean, Column, Index, Integer, Text, UniqueConstraint


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
    # Protocol family that decides how a call is made. See docs/PROVIDER_CONTRACT.md.
    protocol = Column(Text, nullable=False, default='openai-compatible')
    # Provider level switch: when off, none of its models are offered.
    is_enabled = Column(Boolean, nullable=False, default=True)
    # Identifier of the provider template this connection was created from.
    provider_template = Column(Text, nullable=True)
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


class ProviderModel(Base):
    """A model the user curated for one provider connection.

    This is the whitelist: a model is offered to users only after it was added
    here. Rows are never created implicitly by a catalog refresh.
    """

    __tablename__ = 'provider_model'
    id = Column(Text, primary_key=True)
    connection_id = Column(Text, nullable=False)
    upstream_id = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False, default='')
    uses = Column(JSON, nullable=False, default=dict)
    capabilities = Column(JSON, nullable=False, default=list)
    features = Column(JSON, nullable=False, default=list)
    model_info = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    source = Column(Text, nullable=False, default='provider')
    verification = Column(JSON, nullable=False, default=dict)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint('connection_id', 'upstream_id', name='uq_provider_model_connection_upstream'),
        Index('idx_provider_model_connection', 'connection_id'),
    )


class ProviderCatalogSnapshot(Base):
    """The last model list fetched from the provider, per user.

    This is a candidate pool only: it never changes the whitelist.
    """

    __tablename__ = 'provider_catalog_snapshot'
    id = Column(Text, primary_key=True)
    connection_id = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False)
    fingerprint = Column(Text, nullable=False, default='')
    models = Column(JSON, nullable=False, default=list)
    error = Column(JSON, nullable=True)
    generation = Column(Text, nullable=False, default='')
    updated_at = Column(BigInteger, nullable=False)
    __table_args__ = (
        UniqueConstraint('connection_id', 'user_id', name='uq_provider_catalog_snapshot_connection_user'),
        Index('idx_provider_catalog_snapshot_connection', 'connection_id'),
        Index('idx_provider_catalog_snapshot_user', 'user_id'),
    )
