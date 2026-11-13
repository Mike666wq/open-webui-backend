"""Pure helpers for the workspace closed loop (chat → note → knowledge → chat).

Everything here is import-safe: no database, no settings, no application
state. The frontend keeps a byte-compatible JS port of
`canonical_note_title` / `canonical_note_content` /
`note_snapshot_fingerprint`, so a change here must be mirrored there or
snapshots will flip to `changed` on every readiness check.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# The canonical fallback title. It is deliberately a literal, not a
# localization key: a translated 'Untitled' would change the fingerprint and
# make every snapshot look modified after a language switch.
UNTITLED_TITLE = 'Untitled'


def canonical_note_title(title: Any) -> str:
    """Return the title used for fingerprints and filenames.

    Blank (None, non-string, whitespace-only) titles collapse to the literal
    'Untitled' so the fingerprint stays stable.
    """
    if isinstance(title, str) and title.strip():
        return title
    return UNTITLED_TITLE


def canonical_note_content(note_data: Any) -> str:
    """Return `note_data?.content?.md ?? ''` with Python semantics."""
    if not isinstance(note_data, dict):
        return ''
    content = note_data.get('content')
    if not isinstance(content, dict):
        return ''
    md = content.get('md')
    return md if isinstance(md, str) else ''


def note_snapshot_fingerprint(title: Any, content: Any) -> str:
    """SHA256 over a JSON.stringify-compatible payload.

    Mirrors the JS helper exactly: `sha256(JSON.stringify([title, content]))`.
    `ensure_ascii=False` keeps non-ASCII characters unescaped and the compact
    separators match JSON.stringify's default output.
    """
    payload = json.dumps(
        [canonical_note_title(title), content if isinstance(content, str) else ''],
        ensure_ascii=False,
        separators=(',', ':'),
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


_FILENAME_SANITIZE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_FILENAME_MAX_STEM = 100


def safe_markdown_filename(title: Any) -> str:
    """Turn a note title into a filesystem-safe `<stem>.md` name."""
    stem = canonical_note_title(title)
    stem = _FILENAME_SANITIZE_RE.sub(' ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip().strip('.')
    if len(stem) > _FILENAME_MAX_STEM:
        stem = stem[:_FILENAME_MAX_STEM].strip().strip('.')
    return f'{stem or UNTITLED_TITLE}.md'


def structured_loop_error(
    code: str,
    stage: str,
    message: str,
    retryable: bool = True,
    file_id: str | None = None,
    status_code: int = 400,
) -> Any:
    """Build an HTTPException whose detail follows the loop error contract.

    Errors surfaced to the workspace loop UI always carry
    `{code, stage, message, retryable}` (plus an optional `file_id`) so the
    client can render a specific message and a retry affordance instead of
    `[object Object]`.
    """
    from fastapi import HTTPException

    detail: dict[str, Any] = {
        'code': code,
        'stage': stage,
        'message': message,
        'retryable': retryable,
    }
    if file_id:
        detail['file_id'] = file_id
    return HTTPException(status_code=status_code, detail=detail)


def _message_text_from_parts(parts: Any) -> str:
    """Flatten a chat message content that is a list of typed parts."""
    if not isinstance(parts, list):
        return ''
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            for key in ('text', 'content'):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
                    break
                if isinstance(value, list):
                    nested = _message_text_from_parts(value)
                    if nested:
                        texts.append(nested)
                        break
    return '\n'.join(text for text in texts if text).strip()


def _single_message_text(message: Any) -> str:
    """Extract visible text from one chat message (string / parts / dict)."""
    if message is None:
        return ''
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return _message_text_from_parts(content)
    if isinstance(content, dict):
        for key in ('md', 'text'):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''
    return ''


def extract_chat_message_content(chat_data: Any, message_id: str) -> str:  # noqa: C901
    """Return the visible text of one chat message, or '' when not found.

    Accepts a chat payload in any of the shapes that exist in the wild:
    - a chat object with a `.chat` attribute (ChatModel) or a plain dict,
    - messages as a top-level list (`chat.messages`),
    - messages nested under `chat.chat.messages`,
    - the history map `chat.history.messages` keyed by message id,
    - message content as a plain string, a list of typed parts, or a dict.
    """
    if chat_data is None:
        return ''
    chat = getattr(chat_data, 'chat', chat_data)
    if not isinstance(chat, dict):
        return ''

    candidates: list[Any] = []
    for container_key in ('messages',):
        container = chat.get(container_key)
        if isinstance(container, list):
            candidates.append({index: message for index, message in enumerate(container)})
        elif isinstance(container, dict):
            candidates.append(container)

    history = chat.get('history')
    if isinstance(history, dict):
        history_messages = history.get('messages')
        if isinstance(history_messages, dict):
            candidates.append(history_messages)

    # A message id can name a position in the top-level list, a key in the
    # history map, or live in a `messageId`/`id` field — check all of them.
    for container in candidates:
        message = container.get(message_id)
        text = _single_message_text(message)
        if text:
            return text

    for container in candidates:
        for message in container.values():
            if isinstance(message, dict) and message_id in (message.get('id'), message.get('messageId')):
                text = _single_message_text(message)
                if text:
                    return text

    return ''


def merge_note_knowledge_snapshot(meta: Any, kb_id: str, snapshot: dict) -> dict:
    """Merge one per-KB snapshot into `meta.note_knowledge` without clobbering.

    Returns a new meta dict; unrelated top-level keys and the snapshots of
    other knowledge bases are preserved untouched. A legacy non-dict
    `note_knowledge` value is discarded rather than crashing the write.
    """
    merged_meta = dict(meta) if isinstance(meta, dict) else {}
    existing = merged_meta.get('note_knowledge')
    note_knowledge = dict(existing) if isinstance(existing, dict) else {}

    current = note_knowledge.get(kb_id)
    if not isinstance(current, dict):
        current = {}
    note_knowledge[kb_id] = {**current, **snapshot}
    merged_meta['note_knowledge'] = note_knowledge
    return merged_meta


def classify_snapshot_state(flags: dict) -> str:
    """Classify a note→knowledge snapshot into one of the loop states.

    Expected flag keys (missing keys default to False):
    - kb_exists: the knowledge base row still exists
    - can_write: the current user may write to the KB
    - external: the KB is an external (read-only) source
    - file_exists: the mapped File row still exists
    - linked: the (knowledge_id, file_id) relation still exists
    - fingerprint_matches: snapshot fingerprint == current fingerprint
    - index_failed / index_pending: reported indexing state

    Precedence: unavailable → failed → indexing → changed → synced.
    """

    def flag(name: str) -> bool:
        return bool(flags.get(name))

    if (
        not flag('kb_exists')
        or not flag('file_exists')
        or not flag('can_write')
        or flag('external')
        or not flag('linked')
    ):
        return 'unavailable'
    if flag('index_failed'):
        return 'failed'
    if flag('index_pending'):
        return 'indexing'
    if not flag('fingerprint_matches'):
        return 'changed'
    return 'synced'
