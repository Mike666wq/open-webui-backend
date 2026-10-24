"""Durable private recording jobs. Credentials are resolved at execution time."""

from open_webui.internal.db import Base, JSONField
from sqlalchemy import BigInteger, Column, Index, Integer, String, Text, UniqueConstraint, text


class Recording(Base):
    __tablename__ = 'recording'
    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    request_key = Column(String, nullable=False)
    title = Column(Text, nullable=False)
    state = Column(String, nullable=False, index=True)
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
    duration = Column(Integer, nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    source_present = Column(Integer, nullable=False, default=1)
    lease_token = Column(String, nullable=True)
    lease_until = Column(BigInteger, nullable=False, default=0)
    summary_token = Column(String, nullable=True)
    summary_until = Column(BigInteger, nullable=False, default=0)
    note_id = Column(String, nullable=False)
    data = Column(JSONField, nullable=False, default=dict)
    __table_args__ = (
        UniqueConstraint('user_id', 'request_key', name='uq_recording_request'),
        Index(
            'uq_recording_active_user',
            'user_id',
            unique=True,
            sqlite_where=text('lease_token IS NOT NULL'),
            postgresql_where=text('lease_token IS NOT NULL'),
        ),
    )
