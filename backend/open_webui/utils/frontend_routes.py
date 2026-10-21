"""Read page routes from the pinned DIY frontend without importing app state."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


class FrontendRoutePolicy:
    """Only genuine frontend pages may fall back to the SPA entry point."""

    BACKEND_ROOTS = frozenset({'api', 'ws', 'oauth', 'ollama', 'openai', 'cache', 'health'})

    def __init__(self, directory: str | Path, lock_file: str | Path | None = None):
        root = Path(directory)
        raw = (root / 'frontend-manifest.json').read_bytes()
        manifest = json.loads(raw)
        if manifest.get('schema') != 1 or manifest.get('entry') != 'index.html':
            raise ValueError('Unsupported DIY frontend manifest')
        if lock_file is not None:
            lock = json.loads(Path(lock_file).read_text())
            if hashlib.sha256(raw).hexdigest() != lock.get('manifestSha256'):
                raise ValueError('DIY frontend manifest digest mismatch')
            for name in ('sourceRepository', 'sourceCommit', 'upstreamVersion', 'upstreamCommit'):
                if manifest.get(name) != lock.get(name):
                    raise ValueError('DIY frontend identity mismatch: ' + name)
        routes = manifest.get('pageRoutes')
        if not isinstance(routes, list) or not 0 < len(routes) <= 1000:
            raise ValueError('DIY frontend page routes are missing')
        self.patterns = []
        for route in routes:
            if (
                not isinstance(route, str) or len(route) > 1024
                or not route.startswith('^') or not route.endswith('$')
            ):
                raise ValueError('DIY frontend routes must be bounded patterns')
            self.patterns.append(re.compile(route))
        if not (root / 'index.html').is_file():
            raise ValueError('DIY frontend entry is missing')

    def is_page(self, path: str) -> bool:
        if '\\' in path or '\0' in path or any(part in ('.', '..') for part in path.split('/')):
            return False
        normalized = '/' + path.lstrip('/')
        if normalized.lstrip('/').split('/', 1)[0] in self.BACKEND_ROOTS:
            return False
        return any(pattern.fullmatch(normalized) for pattern in self.patterns)

