"""Persist legacy server directories across origins; retain last success on errors."""

import asyncio
import copy
import hashlib
import hmac
import json
import time
from urllib.parse import urlsplit

from open_webui.env import WEBUI_SECRET_KEY
from open_webui.models.config import Config

_tasks = {}


def catalog_key(url, key, user, config):
    signature = hmac.new(
        WEBUI_SECRET_KEY.encode(),
        json.dumps(
            {'url': url, 'key': key, 'user': getattr(user, 'id', ''), 'config': config}, sort_keys=True
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    return 'providers.legacy_catalog.' + signature


def _track_task(storage, task):
    """Attach cleanup callbacks exactly once, when the task is created.

    Older code re-registered both callbacks on every request, so a long-lived
    task accumulated an unbounded callback list.
    """
    task.add_done_callback(lambda done: _tasks.pop(storage, None) if _tasks.get(storage) is done else None)
    # Retrieve background exceptions so failures do not become noisy unhandled tasks.
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def cached_legacy_catalog(request, url, key, user, config, fetch):
    storage = catalog_key(url, key, user, config)
    entry = await Config.get(storage) or {}

    async def refresh():
        try:
            value = await fetch()
        except Exception:
            value = None
        records = value if isinstance(value, list) else value.get('data') if isinstance(value, dict) else None
        if isinstance(records, list) and not (isinstance(value, dict) and value.get('error')):
            updated = {'models': value, 'updated_at': int(time.time()), 'error': None}
        else:
            status = value.get('catalog_error_status', 502) if isinstance(value, dict) else 502
            category = (
                'authentication'
                if status in (401, 403)
                else 'rate_limit'
                if status == 429
                else 'connection'
                if status == 502
                else 'upstream'
            )
            # Record the attempt time as updated_at as well: `stale` is
            # (bool(error) or age>=900), so leaving the old timestamp makes
            # it true forever after a single failure.
            attempt_at = int(time.time())
            updated = {
                **entry,
                'error': {'category': category, 'status': status, 'at': attempt_at},
                'updated_at': attempt_at,
            }
        if updated.get('error') and entry.get('models'):
            updated['models'] = entry['models']
        await Config.upsert({storage: updated})
        return updated

    stale = bool(entry.get('error')) or time.time() - entry.get('updated_at', 0) >= 900
    force = getattr(request, 'query_params', {}).get('refresh') == 'true'
    if force or not entry.get('models'):
        task = _tasks.get(storage)
        if task is None or task.done():
            task = asyncio.create_task(refresh())
            _tasks[storage] = task
            _track_task(storage, task)
        entry = await task
        stale = bool(entry.get('error')) or time.time() - entry.get('updated_at', 0) >= 900
    elif stale and time.time() - (entry.get('error') or {}).get('at', 0) >= 900:
        task = _tasks.get(storage)
        if task is None or task.done():
            task = asyncio.create_task(refresh())
            _tasks[storage] = task
            _track_task(storage, task)
    if request:
        statuses = dict(getattr(request.state, 'legacy_catalog_statuses', {}))
        statuses[storage] = {
            'connection_id': 'legacy:' + storage.rsplit('.', 1)[-1],
            'connection_name': f'旧版 OpenAI ({urlsplit(str(url)).hostname or "未知来源"})',
            'updated_at': entry.get('updated_at'),
            'stale': stale,
            'error': entry.get('error'),
            'request_path': 'server',
        }
        request.state.legacy_catalog_statuses = statuses
    return copy.deepcopy(entry.get('models')) or []
