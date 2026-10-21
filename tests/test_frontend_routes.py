"""Exercise route fallback without starting a database or importing the app."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'backend/open_webui/utils/frontend_routes.py'
SPEC = importlib.util.spec_from_file_location('frontend_route_policy', SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FrontendRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {
            'schema': 1,
            'entry': 'index.html',
            'sourceCommit': 'a' * 40,
            'sourceRepository': 'Mike666wq/open-webui-frontend',
            'upstreamVersion': '0.12.2',
            'upstreamCommit': 'b' * 40,
            'pageRoutes': ['^/$', '^/c/[^/]+/?$', '^/channels/[^/]+/?$', '^/auth/?$'],
        }
        (self.root / 'index.html').write_text('<html>DIY frontend</html>')
        self.write_manifest()

    def write_manifest(self):
        raw = json.dumps(self.manifest).encode()
        (self.root / 'frontend-manifest.json').write_bytes(raw)
        lock = {name: self.manifest[name] for name in (
            'sourceCommit', 'sourceRepository', 'upstreamVersion', 'upstreamCommit'
        )}
        lock['manifestSha256'] = hashlib.sha256(raw).hexdigest()
        (self.root / 'frontend.lock.json').write_text(json.dumps(lock))

    def policy(self):
        return MODULE.FrontendRoutePolicy(self.root, self.root / 'frontend.lock.json')

    def test_actual_chat_deep_links_use_spa_entry(self):
        policy = self.policy()
        for path in ('', '/', 'auth', 'c/chat-1', '/channels/dm-1/'):
            with self.subTest(path=path):
                self.assertTrue(policy.is_page(path))

    def test_missing_assets_and_unknown_pages_do_not_return_html(self):
        policy = self.policy()
        for path in ('missing.css', '_app/immutable/chunk.wasm', 'assets/a.png', 'audio/a.mp3', 'unknown'):
            with self.subTest(path=path):
                self.assertFalse(policy.is_page(path))

    def test_backend_paths_are_never_spa_pages_even_if_pattern_is_broad(self):
        self.manifest['pageRoutes'] = ['^/.*$']
        self.write_manifest()
        for path in ('api/v1/missing', 'ws/socket.io', 'oauth/login', 'cache/image.png', 'health'):
            with self.subTest(path=path):
                self.assertFalse(self.policy().is_page(path))

    def test_traversal_and_backslashes_are_rejected(self):
        self.manifest['pageRoutes'] = ['^/.*$']
        self.write_manifest()
        for path in ('c/../auth', 'c/./auth', 'c\\auth', 'c/\0'):
            self.assertFalse(self.policy().is_page(path))

    def test_changed_manifest_is_rejected(self):
        (self.root / 'frontend-manifest.json').write_text(json.dumps({**self.manifest, 'sourceCommit': 'c' * 40}))
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.policy()

    def test_missing_manifest_and_entry_are_not_silently_accepted(self):
        (self.root / 'index.html').unlink()
        with self.assertRaisesRegex(ValueError, 'entry is missing'):
            self.policy()
        (self.root / 'frontend-manifest.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.policy()


if __name__ == '__main__':
    unittest.main()

