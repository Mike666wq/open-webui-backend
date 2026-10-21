"""Persistent lifecycle state. Business rows remain intact until purge succeeds."""

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text, UniqueConstraint

from open_webui.internal.db import Base, JSONField


class ResourceEntry(Base):
    __tablename__ = 'resource_entry'
    id = Column(String, primary_key=True)
    type = Column(String, nullable=False)
    resource_id = Column(String, nullable=False)
    user_id = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    title = Column(Text, nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    cutoff_at = Column(BigInteger, nullable=False, default=0)
    deleted_at = Column(BigInteger, nullable=False)
    expires_at = Column(BigInteger, nullable=False, index=True)
    generation = Column(Integer, nullable=False, default=1)
    data = Column(JSONField, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint('type', 'resource_id', 'user_id', name='uq_resource_identity'),)


class ResourceTask(Base):
    __tablename__ = 'resource_task'
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False)
    action = Column(String, nullable=False)
    state = Column(String, nullable=False, index=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    lease_until = Column(BigInteger, nullable=False, default=0)
    lease_token = Column(String, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    payload = Column(JSONField, nullable=False, default=dict)
    results = Column(JSONField, nullable=False, default=list)
    __table_args__ = (UniqueConstraint('user_id', 'key', name='uq_resource_task_key'),)


class ResourceRule(Base):
    __tablename__ = 'resource_rule'
    id = Column(String, primary_key=True)
    temporary_uploads = Column(Boolean, nullable=False, default=False)
    updated_at = Column(BigInteger, nullable=False)
    updated_by = Column(String, nullable=False)
