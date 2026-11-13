"""Index a knowledge file in the background.

Adding a file to a knowledge base used to vectorize it inside the request:
`add_file_to_knowledge_by_id` awaited `process_file`, and the agent tool
`add_text_to_knowledge` did the same while returning "accepted — processing
happens in the background". It did not happen in the background. The comment
described the intent, the code did the opposite, and the cost landed on
whoever was waiting: a small note took under a second, a long one took
minutes, and a long recording took longer still.

Embedding is a network call per chunk. Doing it inline also holds the request
open, which ties up a worker for the whole vectorization.

So the file is linked first and indexed afterwards. The caller gets an answer
immediately; the file reports its own progress, and a failed index is visible
instead of being swallowed inside a timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Coroutine
from typing import Optional, Union

from fastapi import HTTPException

from open_webui.routers.retrieval import ProcessFileForm

log = logging.getLogger('open_webui')

# When set, the background indexer runs inline instead of being queued. The
# tests use it to assert the indexing actually happened without racing a
# detached task against a shared sqlite file.
INLINE_INDEXING = False

# The runtime suite shares one sqlite file across every test. A detached
# indexing task would still be writing when the next test opens its
# connection, and the file would be locked out from under it. The suite sets
# this flag, so the indexing runs inline there and the tests can assert on it.
if os.getenv('PROVIDER_TEST_RUNTIME') == '1':
    INLINE_INDEXING = True


def set_inline_indexing(enabled: bool) -> None:
    global INLINE_INDEXING
    INLINE_INDEXING = enabled


# Indexing tasks in flight. They are tracked so the application can wait for
# them on shutdown instead of losing work mid-run, and so a test can assert
# that nothing is left running.
_PENDING: set[asyncio.Task] = set()


def track_indexing(task: Union[asyncio.Task, Coroutine]) -> asyncio.Task:
    """Register an indexing job so it can be drained on shutdown.

    Accepts either an already-scheduled `asyncio.Task` or a bare coroutine.
    A coroutine is scheduled here — calling `add_done_callback()` on it
    directly would raise AttributeError, and awaiting it twice is not an
    option since callers hand it over fire-and-forget.
    """
    if not isinstance(task, asyncio.Task):
        task = asyncio.create_task(task)
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


async def drain_indexing(timeout: float = 30.0) -> None:
    """Wait for the indexing tasks in flight, so nothing is lost on shutdown."""
    if not _PENDING:
        return
    await asyncio.wait(set(_PENDING), timeout=timeout)


async def index_file_in_background(
    request,
    *,
    file_id: str,
    collection_name: str,
    user,
    db_factory,
    knowledge_id: str | None = None,
    directory_id: str | None = None,
    indexer=None,
) -> None:
    """Vectorize one file without holding the caller's request open.

    `db_factory` must return a fresh async session context manager; the
    indexing work runs on its own session so it cannot collide with the
    request that queued it.
    """
    # The caller passes the indexer in: the router imports `process_file` into
    # its own module namespace, and the tests patch that name. Reaching for
    # `retrieval.process_file` here would bypass the patch.
    if indexer is None:
        from open_webui.routers import retrieval

        indexer = retrieval.process_file

    try:
        async with db_factory() as session:
            await indexer(
                request,
                ProcessFileForm(file_id=file_id, collection_name=collection_name),
                user=user,
                db=session,
            )
        await _mark_indexed(file_id, knowledge_id=knowledge_id, collection_name=collection_name)
    except HTTPException as exc:
        log.warning('Background indexing failed for %s: %s', file_id, exc.detail)
        await _mark_failed(file_id, str(exc.detail), knowledge_id=knowledge_id, collection_name=collection_name)
    except Exception as exc:
        log.exception('Background indexing failed for %s', file_id)
        await _mark_failed(file_id, str(exc), knowledge_id=knowledge_id, collection_name=collection_name)


async def _mark_indexed(file_id: str, knowledge_id: str | None = None, collection_name: str | None = None) -> None:
    await _write_index_state(
        file_id,
        'completed',
        knowledge_id=knowledge_id,
        collection_name=collection_name,
    )


async def _write_index_state(
    file_id: str,
    state: str,
    error: str | None = None,
    knowledge_id: Optional[str] = None,
    collection_name: Optional[str] = None,
) -> None:
    """Record the indexing outcome on the file itself.

    Both status surfaces are written so every reader agrees:
    - `file.data.status` / `file.data.error` — what file previews and the
      process/status endpoint report;
    - `file.meta.status` / `file.meta.error` — the legacy location some
      views still read;
    - `file.meta.index_state[knowledge_id]` — the per-knowledge-base record
      used by knowledge/note readiness, only when `knowledge_id` is given.

    The update goes through an explicit statement: the row may have been loaded
    in another session, and assigning to a detached JSON column would then be
    written back nowhere.
    """
    from sqlalchemy import update

    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import File

    async with get_async_db_context() as db:
        row = await db.get(File, file_id)
        if row is None:
            return
        meta = dict(row.meta or {})
        meta['status'] = state
        meta['error'] = error[:500] if error else None
        if knowledge_id:
            index_state = dict(meta.get('index_state') or {})
            if not isinstance(index_state.get(knowledge_id), dict):
                index_state[knowledge_id] = {}
            index_state[knowledge_id] = {
                'state': state,
                'error': error if error else None,
                'updated_at': int(time.time() * 1000),
                'collection_name': collection_name,
            }
            meta['index_state'] = index_state

        data = {**(row.data or {}), 'status': state, 'error': error[:500] if error else None}
        await db.execute(update(File).where(File.id == file_id).values(data=data, meta=meta))
        await db.commit()


async def _mark_failed(
    file_id: str,
    reason: str,
    knowledge_id: str | None = None,
    collection_name: str | None = None,
) -> None:
    try:
        await _write_index_state(
            file_id,
            'failed',
            reason,
            knowledge_id=knowledge_id,
            collection_name=collection_name,
        )
    except Exception:
        log.exception('Could not record the failed index state of %s', file_id)
