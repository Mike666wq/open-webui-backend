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


def track_indexing(task: asyncio.Task) -> asyncio.Task:
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
        await _mark_indexed(file_id)
    except HTTPException as exc:
        log.warning('Background indexing failed for %s: %s', file_id, exc.detail)
        await _mark_failed(file_id, str(exc.detail))
    except Exception as exc:
        log.exception('Background indexing failed for %s', file_id)
        await _mark_failed(file_id, str(exc))


async def _mark_indexed(file_id: str) -> None:
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import Files

    async with get_async_db_context() as db:
        file = await Files.get_file_by_id(file_id, db=db)
        if file is None:
            return
        meta = dict(file.meta or {})
        meta['status'] = 'completed'
        file.meta = meta
        file.status = 'completed'
        await db.commit()


async def _mark_failed(file_id: str, reason: str) -> None:
    from open_webui.internal.db import get_async_db_context
    from open_webui.models.files import Files

    try:
        async with get_async_db_context() as db:
            file = await Files.get_file_by_id(file_id, db=db)
            if file is None:
                return
            meta = dict(file.meta or {})
            meta['status'] = 'failed'
            meta['error'] = reason[:500]
            file.meta = meta
            file.status = 'failed'
            await db.commit()
    except Exception:
        log.exception('Could not record the failed index state of %s', file_id)
