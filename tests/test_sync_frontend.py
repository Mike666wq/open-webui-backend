"""Regression tests for cross-repository ordering, deduplication and retry safety."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/sync_frontend.py'
spec = importlib.util.spec_from_file_location('sync_frontend', SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
SHA = 'a' * 40
HEAD = 'b' * 40
CURRENT = dict(schema=1, sourceRepository=sync.FRONTEND, sourceCommit='c' * 40,
               upstreamVersion='0.12.2', upstreamCommit='d' * 40)
MANIFEST = json.dumps({**CURRENT, 'sourceCommit': SHA}).encode()
CHECKSUMS = ('e' * 64 + '  open-webui-frontend.zip\n' + hashlib.sha256(MANIFEST).hexdigest() + '  frontend-manifest.json\n').encode()
LOCK = sync.make_lock(CURRENT, SHA, MANIFEST, CHECKSUMS)


class SyncTests(unittest.TestCase):
    def run_sync(self, current=None, runs=None, release_missing=False, marker='', workflow='backend.yml', force=False, check=False, git_failure=False):
        calls = []
        prefix = f'https://github.com/{sync.FRONTEND}/releases/download/frontend-{SHA[:12]}/'
        def api(route, data=None):
            calls.append((route, data))
            if route == f'/repos/{sync.FRONTEND}/commits/main':
                return {'sha': SHA}
            if '/releases/tags/' in route:
                if release_missing:
                    raise HTTPError('https://api.github.com', 404, 'Not Found', {}, None)
                return dict(draft=False, prerelease=False, tag_name='frontend-' + SHA[:12],
                            assets=[dict(name=name, browser_download_url=prefix+name) for name in ['open-webui-frontend.zip', 'frontend-manifest.json', 'SHA256SUMS']])
            if route.endswith('/commits/main'):
                return {'sha': HEAD}
            if '/runs?' in route:
                return {'workflow_runs': runs or []}
            if route.endswith('/dispatches'):
                return None
            raise AssertionError(route)
        def git(*args):
            calls.append(('git', args))
            if args[0] == 'push' and git_failure:
                raise RuntimeError('non-fast-forward')
            if args[0] == 'log':
                return marker
            return HEAD
        with tempfile.TemporaryDirectory() as directory:
            old = os.getcwd()
            try:
                os.chdir(directory)
                Path('frontend.lock.json').write_text(json.dumps(CURRENT if current is None else current))
                env = dict(GITHUB_REF='refs/heads/main', GITHUB_REPOSITORY='owner/consumer',
                           DOWNSTREAM_WORKFLOW=workflow, GITHUB_STEP_SUMMARY=str(Path(directory)/'summary'),
                           PIPELINE_FORCE='true' if force else 'false')
                with patch.dict(os.environ, env), patch.object(sync, 'api', side_effect=api), \
                     patch.object(sync, 'request', side_effect=lambda url: MANIFEST if url.endswith('.json') else CHECKSUMS), \
                     patch.object(sync, 'git', side_effect=git), patch.object(sys, 'argv', ['sync_frontend.py'] + (['--check'] if check else [])), \
                     contextlib.redirect_stdout(io.StringIO()):
                    sync.main()
                result = json.loads(Path('frontend.lock.json').read_text())
            finally:
                os.chdir(old)
        return calls, result

    def test_new_release_commits_only_lock_before_dispatch(self):
        calls, result = self.run_sync()
        self.assertEqual(result, LOCK)
        push = next(i for i, call in enumerate(calls) if call == ('git', ('push', 'origin', 'HEAD:main')))
        dispatch = next(i for i, call in enumerate(calls) if call[0].endswith('/dispatches'))
        self.assertLess(push, dispatch)
        self.assertIn(('git', ('add', 'frontend.lock.json')), calls)

    def test_pending_frontend_prevents_all_downstream_mutation(self):
        calls, result = self.run_sync(release_missing=True)
        self.assertEqual(result, CURRENT)
        self.assertFalse(any(call[0] == 'git' or call[1] is not None for call in calls))

    def test_duplicate_schedule_does_not_build_again(self):
        calls, _ = self.run_sync(current=LOCK, runs=[{'html_url':'https://example.com/run'}])
        self.assertFalse(any(call[0].endswith('/dispatches') for call in calls))

    def test_missing_dispatch_is_recovered_without_another_commit(self):
        calls, _ = self.run_sync(current=LOCK)
        self.assertTrue(any(call[0].endswith('/dispatches') for call in calls))
        self.assertFalse(any(call[0] == 'git' and call[1][0] == 'commit' for call in calls))

    def test_version_bump_does_not_cause_infinite_android_releases(self):
        calls, _ = self.run_sync(current=LOCK, workflow='android.yml', marker='chore(release): v1.0.0\n\nopenwebui-release-run:123')
        self.assertFalse(any(call[0].endswith('/dispatches') for call in calls))

    def test_new_frontend_can_follow_an_android_version_bump(self):
        calls, _ = self.run_sync(workflow='android.yml', marker='openwebui-release-run:123')
        self.assertTrue(any(call[0].endswith('/dispatches') for call in calls))

    def test_manual_rebuild(self):
        calls, _ = self.run_sync(current=LOCK, runs=[{'html_url':'https://example.com/run'}], force=True)
        self.assertTrue(any(call[0].endswith('/dispatches') for call in calls))

    def test_stale_manual_build_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.run_sync(check=True)
        self.run_sync(current=LOCK, check=True)

    def test_pending_frontend_rejects_direct_build(self):
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.run_sync(check=True, release_missing=True)

    def test_push_conflict_stops_pipeline(self):
        with self.assertRaisesRegex(RuntimeError, 'non-fast-forward'):
            self.run_sync(git_failure=True)

    def test_corrupt_manifest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            sync.make_lock(CURRENT, SHA, MANIFEST + b' ', CHECKSUMS)

    def test_incompatible_upstream_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            sync.make_lock({**CURRENT, 'upstreamVersion':'99'}, SHA, MANIFEST, CHECKSUMS)

    def test_duplicate_checksums_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            sync.make_lock(CURRENT, SHA, MANIFEST, CHECKSUMS + CHECKSUMS)


if __name__ == '__main__':
    unittest.main()
