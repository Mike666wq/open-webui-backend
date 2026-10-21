"""Resource lifecycle: overview, soft-delete to 7-day recycle bin, restore, purge."""

import json
import time
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from open_webui.internal.db import get_async_session
from open_webui.utils.auth import get_current_user, get_admin_user

router = APIRouter()

RECYCLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
RESOURCE_TYPES = ("chat", "file", "note", "knowledge")


class RecycleItem(BaseModel):
    id: str
    type: str
    title: str
    size_bytes: int = 0
    deleted_at: int
    expires_at: int
    meta: Optional[dict] = None


class ResourceStats(BaseModel):
    chats: int = 0
    files: int = 0
    notes: int = 0
    knowledge: int = 0
    total_bytes: int = 0
    recycled_count: int = 0
    recycled_bytes: int = 0


class DeleteRequest(BaseModel):
    ids: list[str]
    type: Literal["chat", "file", "note", "knowledge"]
    permanent: bool = False


class RestoreRequest(BaseModel):
    ids: list[str]
    type: Literal["chat", "file", "note", "knowledge"]


def _now() -> int:
    return int(time.time())


def _recycle_table(resource_type: str) -> str:
    return f"resource_recycle_{resource_type}"


async def _ensure_recycle_table(db: AsyncSession, resource_type: str) -> None:
    await db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {_recycle_table(resource_type)} (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            original_id TEXT NOT NULL,
            title TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            deleted_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            meta TEXT
        )
    """))
    await db.commit()


async def _move_to_recycle(
    db: AsyncSession, resource_type: str, original_id: str, user_id: str,
    title: str, size_bytes: int = 0, meta: Optional[dict] = None,
) -> None:
    await _ensure_recycle_table(db, resource_type)
    now = _now()
    await db.execute(
        text(f"""
            INSERT OR REPLACE INTO {_recycle_table(resource_type)}
            (id, user_id, original_id, title, size_bytes, deleted_at, expires_at, meta)
            VALUES (:id, :user_id, :original_id, :title, :size_bytes, :deleted_at, :expires_at, :meta)
        """),
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "original_id": original_id,
            "title": title,
            "size_bytes": size_bytes,
            "deleted_at": now,
            "expires_at": now + RECYCLE_RETENTION_SECONDS,
            "meta": json.dumps(meta or {}),
        },
    )
    await db.commit()


async def _list_recycled(user_id: str, db: AsyncSession) -> list[RecycleItem]:
    items: list[RecycleItem] = []
    for rtype in RESOURCE_TYPES:
        await _ensure_recycle_table(db, rtype)
        result = await db.execute(
            text(f"""
                SELECT id, original_id, title, size_bytes, deleted_at, expires_at, meta
                FROM {_recycle_table(rtype)} WHERE user_id = :uid ORDER BY deleted_at DESC
            """),
            {"uid": user_id},
        )
        for row in result.mappings():
            items.append(RecycleItem(
                id=row["id"],
                type=rtype,
                title=row["title"],
                size_bytes=row["size_bytes"],
                deleted_at=row["deleted_at"],
                expires_at=row["expires_at"],
                meta=json.loads(row["meta"]) if row["meta"] else None,
            ))
    return items


@router.get("/overview", response_model=ResourceStats)
async def resource_overview(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> ResourceStats:
    stats = ResourceStats()

    # Count chats
    result = await db.execute(
        text("SELECT COUNT(*) FROM chat WHERE user_id = :uid"),
        {"uid": user.id},
    )
    stats.chats = result.scalar() or 0

    # Count files and sum sizes
    result = await db.execute(
        text("SELECT COUNT(*), COALESCE(SUM(CAST(json_extract(meta, '$.size') AS INTEGER)), 0) FROM file WHERE user_id = :uid"),
        {"uid": user.id},
    )
    row = result.first()
    stats.files = row[0] or 0
    stats.total_bytes = row[1] or 0

    # Count notes
    result = await db.execute(
        text("SELECT COUNT(*) FROM note WHERE user_id = :uid"),
        {"uid": user.id},
    )
    stats.notes = result.scalar() or 0

    # Count knowledge
    result = await db.execute(
        text("SELECT COUNT(*) FROM knowledge WHERE user_id = :uid"),
        {"uid": user.id},
    )
    stats.knowledge = result.scalar() or 0

    recycled = await _list_recycled(user.id, db)
    stats.recycled_count = len(recycled)
    stats.recycled_bytes = sum(item.size_bytes for item in recycled)
    return stats


@router.get("/recycle-bin", response_model=list[RecycleItem])
async def list_recycle_bin(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> list[RecycleItem]:
    return await _list_recycled(user.id, db)


@router.post("/delete")
async def delete_resources(
    body: DeleteRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    deleted = 0
    for resource_id in body.ids:
        try:
            table_name = body.type
            if body.type == "chat":
                table_name = "chat"
                title_col = "title"
            elif body.type == "file":
                table_name = "file"
                title_col = "filename"
            elif body.type == "note":
                table_name = "note"
                title_col = "title"
            elif body.type == "knowledge":
                table_name = "knowledge"
                title_col = "name"

            # Fetch resource info
            result = await db.execute(
                text(f"SELECT {title_col}, user_id FROM {table_name} WHERE id = :rid"),
                {"rid": resource_id},
            )
            row = result.first()
            if not row or row[1] != user.id:
                continue

            title = row[0] or "Untitled"

            if not body.permanent:
                # Get size for files
                size_bytes = 0
                if body.type == "file":
                    size_result = await db.execute(
                        text("SELECT CAST(json_extract(meta, '$.size') AS INTEGER) FROM file WHERE id = :rid"),
                        {"rid": resource_id},
                    )
                    size_bytes = size_result.scalar() or 0

                await _move_to_recycle(
                    db, body.type, resource_id, user.id,
                    title=title, size_bytes=size_bytes,
                )

            # Delete from main table
            await db.execute(
                text(f"DELETE FROM {table_name} WHERE id = :rid AND user_id = :uid"),
                {"rid": resource_id, "uid": user.id},
            )
            await db.commit()
            deleted += 1
        except Exception:
            continue
    return {"deleted": deleted, "permanent": body.permanent}


@router.post("/restore")
async def restore_resources(
    body: RestoreRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    restored = 0
    for resource_id in body.ids:
        try:
            table = _recycle_table(body.type)
            await _ensure_recycle_table(db, body.type)
            result = await db.execute(
                text(f"SELECT id FROM {table} WHERE original_id = :oid AND user_id = :uid"),
                {"oid": resource_id, "uid": user.id},
            )
            if result.first():
                await db.execute(
                    text(f"DELETE FROM {table} WHERE original_id = :oid AND user_id = :uid"),
                    {"oid": resource_id, "uid": user.id},
                )
                await db.commit()
                restored += 1
        except Exception:
            continue
    return {"restored": restored}


@router.get("/recycle-bin/{resource_type}/{resource_id}", response_model=RecycleItem)
async def get_recycle_item(
    resource_type: str,
    resource_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
) -> RecycleItem:
    if resource_type not in RESOURCE_TYPES:
        raise HTTPException(status_code=400, detail="Invalid resource type")
    table = _recycle_table(resource_type)
    await _ensure_recycle_table(db, resource_type)
    result = await db.execute(
        text(f"""
            SELECT id, original_id, title, size_bytes, deleted_at, expires_at, meta
            FROM {table} WHERE original_id = :oid AND user_id = :uid
        """),
        {"oid": resource_id, "uid": user.id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Item not found in recycle bin")
    return RecycleItem(
        id=row["id"], type=resource_type, title=row["title"],
        size_bytes=row["size_bytes"], deleted_at=row["deleted_at"],
        expires_at=row["expires_at"],
        meta=json.loads(row["meta"]) if row["meta"] else None,
    )


@router.post("/recycle-bin/purge-expired")
async def purge_expired_recycle_bin(
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
) -> dict:
    purged: dict[str, int] = {}
    now = _now()
    for rtype in RESOURCE_TYPES:
        table = _recycle_table(rtype)
        await _ensure_recycle_table(db, rtype)
        result = await db.execute(
            text(f"DELETE FROM {table} WHERE expires_at < :now"),
            {"now": now},
        )
        await db.commit()
        purged[rtype] = result.rowcount
    return {"purged": purged}
