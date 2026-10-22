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
    def run_sync(self, current=None, runs=None, release_missing=False, marker='', workflow='backend.yml', force=False, check=False, git_failure=False, consumer_head=HEAD, frontend_heads=None):
        calls = []
        resolved_release = dict(draft=False, prerelease=False, tag_name='frontend-' + SHA[:12], assets=[dict(name=name, browser_download_url=f'https://github.com/{sync.FRONTEND}/releases/download/frontend-{SHA[:12]}/'+name) for name in ['open-webui-frontend.zip', 'frontend-manifest.json', 'SHA256SUMS']])
        heads = iter(frontend_heads or [SHA, SHA])
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
                return {'workflow_runs': [{**run, 'head_sha': HEAD} for run in (runs or [])]}
            if route.endswith('/dispatches'):
                return None
            raise AssertionError(route)
        def git(*args):
            calls.append(('git', args))
            if args[0] == 'push' and git_failure:
                raise RuntimeError('non-fast-forward')
            if args[:2] == ('ls-remote', f'https://github.com/{sync.FRONTEND}.git'):
                return next(heads) + '\trefs/heads/main'
            if args[:2] == ('ls-remote', 'origin'):
                return consumer_head + '\trefs/heads/main'
            if args[0] == 'diff':
                return ''
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
                     patch.object(sync, 'latest_frontend', side_effect=lambda: None if release_missing else (next(heads), resolved_release)), patch.object(sync, 'git', side_effect=git), patch.object(sys, 'argv', ['sync_frontend.py'] + (['--check'] if check else [])), \
                     contextlib.redirect_stdout(io.StringIO()):
                    sync.main()
                result = json.loads(Path('frontend.lock.json').read_text())
            finally:
                os.chdir(old)
        return calls, result

    def test_read_after_push_uses_git_refs_instead_of_cached_commit_api(self):
        calls, _ = self.run_sync()
        self.assertFalse(any(call[0].endswith('/commits/main') for call in calls))
        self.assertIn(('git', ('ls-remote', 'origin', 'refs/heads/main')), calls)

    def test_frontend_advancing_during_resolution_waits(self):
        calls, result = self.run_sync(frontend_heads=[SHA, 'f' * 40])
        self.assertEqual(result, CURRENT)
        self.assertFalse(any(call[0].endswith('/dispatches') for call in calls))

    def test_consumer_advancing_does_not_dispatch_unchecked_source(self):
        calls, _ = self.run_sync(current=LOCK, consumer_head='f' * 40)
        self.assertFalse(any(call[0].endswith('/dispatches') for call in calls))

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
        self.assertFalse(any(call[0] == 'git' and call[1][0] != 'ls-remote' or call[0] != 'git' and call[1] is not None for call in calls))

    def test_duplicate_schedule_does_not_build_again(self):
        calls, _ = self.run_sync(current=LOCK, runs=[{'html_url':'https://example.com/run'}])
        self.assertFalse(any(call[0].endswith('/dispatches') for call in calls))

    def test_missing_dispatch_is_recovered_without_another_commit(self):
        calls, _ = self.run_sync(current=LOCK)
        self.assertTrue(any(call[0].endswith('/dispatches') for call in calls))
        self.assertFalse(any(call[0] == 'git' and call[1][0] == 'commit' for call in calls))

    def test_manual_rebuild(self):
        calls, _ = self.run_sync(current=LOCK, runs=[{'html_url':'https://example.com/run'}], force=True)
        self.assertTrue(any(call[0].endswith('/dispatches') for call in calls))

    def test_stale_manual_build_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.run_sync(check=True)
        self.run_sync(current=LOCK, check=True)

    def test_pending_frontend_rejects_direct_build(self):
        with self.assertRaisesRegex(ValueError, 'published'):
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




class ProductResolutionTests(unittest.TestCase):
    def test_docs_and_native_commits_do_not_hide_published_web(self):
        release = dict(tag_name='frontend-' + SHA[:12], draft=False, prerelease=False)
        apk = dict(tag_name='v0.2.33', draft=False, prerelease=False)
        def api(route, data=None):
            if '/releases?' in route: return [apk, release]
            if '/commits?' in route: return [{'sha': HEAD}, {'sha': SHA}]
            raise AssertionError(route)
        with patch.object(sync, 'api', side_effect=api):
            self.assertEqual(sync.latest_frontend(), (SHA, release))

    def test_source_history_wins_over_release_creation_order(self):
        old = dict(tag_name='frontend-' + HEAD[:12], draft=False, prerelease=False)
        new = dict(tag_name='frontend-' + SHA[:12], draft=False, prerelease=False)
        with patch.object(sync, 'api', side_effect=[[old, new], [{'sha': SHA}, {'sha': HEAD}]]):
            self.assertEqual(sync.latest_frontend(), (SHA, new))

    def test_docs_only_backend_does_not_need_image(self):
        with patch.object(sync, 'git', return_value='README.md\ndocs/CI.md\n.devcontainer/devcontainer.json'):
            self.assertFalse(sync.product_changed(SHA, HEAD))
        with patch.object(sync, 'git', return_value='backend/open_webui/main.py'):
            self.assertTrue(sync.product_changed(SHA, HEAD))

if __name__ == '__main__':
    unittest.main()
