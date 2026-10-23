#!/usr/bin/env python3
"""Run provider runtime tests with disposable storage and synthetic credentials."""

import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

from dev_backend import REPO_ROOT, configuration


def main():
    with tempfile.TemporaryDirectory(prefix='owui-provider-runtime-') as temporary:
        root = Path(temporary)
        env = configuration(REPO_ROOT, 8081, dict(os.environ))
        env.update(
            {
                'DATA_DIR': str(root / 'data'),
                'STATIC_DIR': str(root / 'static'),
                'DATABASE_URL': f'sqlite:///{root / "data" / "webui.db"}',
                'WEBUI_SECRET_KEY': secrets.token_urlsafe(48),
                'WEBUI_SECRET_KEY_FILE': str(root / 'secret'),
                'FRONTEND_BUILD_DIR': str(root / 'absent'),
                'PROVIDER_TEST_RUNTIME': '1',
            }
        )
        (root / 'data').mkdir()
        (root / 'static').mkdir()
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(REPO_ROOT / 'backend'))
        suite = unittest.defaultTestLoader.discover(str(REPO_ROOT / 'backend' / 'runtime_tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise SystemExit(1)


if __name__ == '__main__':
    main()
