"""One-time confirmation tokens for agent-initiated deletions.

Agent tools must never delete user content on a model's word alone. Before any
destructive builtin tool runs, it has to present a token that was issued by a
read-only preview call for the *same* user and the *same* resource. Tokens are
single-use, short lived and never persisted, so a leaked or replayed token
cannot delete anything twice.

The store is deliberately in-process: this deployment runs a single backend
instance. See ``docs/AGENT_TOOL_CONTRACT.md`` section 7 and 12.3 for the
multi-instance boundary.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

#: A token stays valid for five minutes, long enough for a user to read the
#: impact summary and answer, short enough that a stale token expires.
TOKEN_TTL_SECONDS = 300

#: Bounded so a long-lived process cannot accumulate tokens without limit.
MAX_TOKENS = 1000

_lock = threading.Lock()

# token -> {'user_id': str, 'resource_key': str, 'expires_at': float}
_tokens: dict[str, dict] = {}


def make_resource_key(resource_type: str, resource_id: str, knowledge_id: Optional[str] = None) -> str:
    """Build the canonical key a token is bound to.

    The key must be stable across the preview call and the delete call, which
    is why it is derived from arguments rather than from database rows.
    """
    resource_type = (resource_type or '').strip()
    resource_id = (resource_id or '').strip()

    if resource_type == 'knowledge_file':
        return f'knowledge_file:{(knowledge_id or "").strip()}:{resource_id}'

    return f'{resource_type}:{resource_id}'


def _purge_expired(now: float) -> None:
    expired = [key for key, entry in _tokens.items() if entry['expires_at'] <= now]
    for key in expired:
        _tokens.pop(key, None)


def _evict_overflow() -> None:
    # dicts preserve insertion order, so the first keys are the oldest.
    while len(_tokens) > MAX_TOKENS:
        _tokens.pop(next(iter(_tokens)))


def issue_token(user_id: str, resource_key: str) -> str:
    """Issue a fresh single-use token for ``(user_id, resource_key)``."""
    token = secrets.token_urlsafe(32)
    now = time.monotonic()

    with _lock:
        _purge_expired(now)
        _tokens[token] = {
            'user_id': user_id,
            'resource_key': resource_key,
            'expires_at': now + TOKEN_TTL_SECONDS,
        }
        _evict_overflow()

    return token


def consume_token(token: str, user_id: str, resource_key: str) -> bool:
    """Validate ``token`` and invalidate it in the same step.

    Returns ``True`` only when the token exists, belongs to this user, is bound
    to this resource and has not expired. A successful consume burns the token
    even if the delete that follows fails, so a retry needs a new preview.
    """
    if not token:
        return False

    now = time.monotonic()

    with _lock:
        entry = _tokens.get(token)
        if entry is None:
            return False

        if entry['expires_at'] <= now:
            _tokens.pop(token, None)
            return False

        if entry['user_id'] != user_id or entry['resource_key'] != resource_key:
            # Do not burn someone else's token on a mismatched attempt: a stale
            # request for the wrong resource must not invalidate a valid one.
            return False

        _tokens.pop(token, None)
        return True


def pending_count() -> int:
    """Number of live tokens; used by tests and diagnostics."""
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        return len(_tokens)


def reset_tokens() -> None:
    """Drop every token. Tests only; never called from request paths."""
    with _lock:
        _tokens.clear()
