"""Routes that take a model upstream id must accept ids containing '/'."""
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class UpstreamIdRouteTests(unittest.TestCase):
    def test_path_converter_accepts_slash(self):
        from open_webui.routers import providers

        # Inspect the declared routes rather than boot the whole app.
        paths = {(getattr(r, 'path', ''), tuple(sorted(getattr(r, 'methods', []) or [])))
                 for r in providers.router.routes}
        put = [p for p, m in paths if p.endswith('models/{upstream_id:path}') and 'PUT' in m]
        delete = [p for p, m in paths if p.endswith('models/{upstream_id:path}') and 'DELETE' in m]
        self.assertEqual(len(put), 1, paths)
        self.assertEqual(len(delete), 1, paths)

    def test_single_segment_still_matches(self):
        from open_webui.routers import providers
        p = [r.path for r in providers.router.routes if 'upstream_id:path' in getattr(r, 'path', '')]
        self.assertTrue(p)
        # No plain {upstream_id} route may remain: it cannot match a/b ids.
        plain = [r.path for r in providers.router.routes
                 if '{upstream_id}' in getattr(r, 'path', '')]
        self.assertEqual(plain, [], plain)


if __name__ == '__main__':
    unittest.main()
