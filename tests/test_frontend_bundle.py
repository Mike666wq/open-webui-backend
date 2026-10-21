"""Self-contained frontend supply-chain checks; never contact a backend or GitHub."""

import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import frontend_archive
import prepare_frontend


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FrontendBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='backend-frontend-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'frontend.zip'
        self.destination = self.root / '.build/frontend'
        self.files = {
            'index.html': b'<html><script src="/_app/chunk.js"></script></html>',
            '_app/chunk.js': b'console.log("pinned frontend")',
        }
        self.manifest = {
            'schema': 1,
            'sourceRepository': prepare_frontend.REPOSITORY,
            'sourceCommit': 'a' * 40,
            'upstreamVersion': prepare_frontend.UPSTREAM_VERSION,
            'upstreamCommit': prepare_frontend.UPSTREAM_COMMIT,
            'entry': 'index.html',
            'pageRoutes': ['^/$', '^/channels/[^/]+/?$'],
            'files': [
                {
                    'path': name,
                    'sha256': digest(data),
                    'size': len(data),
                    'mime': 'text/html' if name.endswith('.html') else 'text/javascript',
                }
                for name, data in self.files.items()
            ],
        }
        self.lock = {
            name: self.manifest[name]
            for name in ('schema', 'sourceRepository', 'sourceCommit', 'upstreamVersion', 'upstreamCommit')
        }
        self.lock['archiveUrl'] = f'https://github.com/{prepare_frontend.REPOSITORY}/releases/download/frontend-{"a" * 12}/open-webui-frontend.zip'
        self.write_bundle()

    def write_lock(self):
        (self.root / 'frontend.lock.json').write_text(json.dumps(self.lock))

    def write_bundle(self, files=None, extra=None):
        metadata = (json.dumps(self.manifest, indent=2) + '\n').encode()
        with zipfile.ZipFile(self.archive, 'w') as zipped:
            zipped.writestr(frontend_archive.MANIFEST, metadata)
            for name, data in (self.files if files is None else files).items():
                zipped.writestr(name, data)
            for name, data in (extra or {}).items():
                zipped.writestr(name, data)
        self.lock['manifestSha256'] = digest(metadata)
        self.lock['archiveSha256'] = digest(self.archive.read_bytes())
        self.write_lock()

    def prepare(self):
        return prepare_frontend.prepare_frontend(self.root, self.archive)

    def assert_no_install_or_staging(self):
        self.assertFalse(self.destination.exists())
        parent = self.root / '.build'
        if parent.exists():
            self.assertEqual(list(parent.iterdir()), [])

    def test_valid_extraction_and_verified_cache(self):
        result = self.prepare()
        self.assertFalse(result['cached'])
        self.assertEqual(result['sourceCommit'], 'a' * 40)
        self.assertEqual(result['files'], 2)
        for name, data in self.files.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        self.assertEqual(list((self.root / '.build').iterdir()), [self.destination])
        with patch.object(prepare_frontend, 'download_archive', side_effect=AssertionError('No download for cache')):
            cached = prepare_frontend.prepare_frontend(self.root, self.root / 'missing.zip')
        self.assertTrue(cached['cached'])

    def test_empty_output_directory_can_be_installed(self):
        self.destination.mkdir(parents=True)
        self.prepare()
        self.assertTrue((self.destination / 'index.html').is_file())

    def test_tampered_archive_hash_is_rejected(self):
        self.archive.write_bytes(self.archive.read_bytes() + b'tampered')
        with self.assertRaisesRegex(ValueError, 'archive SHA256'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_pinned_archive_with_corrupt_file_is_rejected(self):
        self.write_bundle(files={**self.files, '_app/chunk.js': b'wrong bytes'})
        with self.assertRaisesRegex(ValueError, 'asset size or SHA256'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_manifest_digest_is_checked_separately(self):
        self.lock['manifestSha256'] = 'b' * 64
        self.write_lock()
        with self.assertRaisesRegex(ValueError, 'manifest SHA256'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_manifest_origin_identity_cannot_disagree_with_lock(self):
        for key, bad in (
            ('sourceRepository', 'other/frontend'),
            ('sourceCommit', 'b' * 40),
            ('upstreamVersion', '0.12.3'),
            ('upstreamCommit', 'c' * 40),
        ):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = bad
                self.write_bundle()
                with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                    self.prepare()
                self.assert_no_install_or_staging()
                self.manifest[key] = original

    def test_lock_identity_and_download_origin_are_strict(self):
        changes = (
            ('schema', True), ('schema', 2),
            ('sourceRepository', 'other/frontend'),
            ('sourceCommit', 'main'), ('sourceCommit', 'A' * 40),
            ('upstreamVersion', '0.12.3'), ('upstreamCommit', 'b' * 40),
            ('archiveSha256', 'bad'), ('manifestSha256', 'G' * 64),
            ('archiveUrl', self.lock['archiveUrl'].replace('https:', 'http:')),
            ('archiveUrl', self.lock['archiveUrl'] + '?latest=true'),
            ('archiveUrl', 'https://example.com/frontend.zip'),
        )
        for key, bad in changes:
            with self.subTest(key=key, bad=bad):
                original = self.lock[key]
                self.lock[key] = bad
                self.write_lock()
                with patch.object(prepare_frontend, 'download_archive', side_effect=AssertionError('Invalid locks must not download')):
                    with self.assertRaisesRegex(ValueError, 'identity|URL'):
                        prepare_frontend.prepare_frontend(self.root)
                self.assert_no_install_or_staging()
                self.lock[key] = original

    def test_missing_declared_resource_is_rejected(self):
        self.write_bundle(files={'index.html': self.files['index.html']})
        with self.assertRaisesRegex(ValueError, 'inventory'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_missing_entry_and_invalid_mime_are_rejected(self):
        original = self.manifest['files']
        self.manifest['files'] = original[1:]
        self.write_bundle()
        with self.assertRaisesRegex(ValueError, 'entry'):
            self.prepare()
        self.assert_no_install_or_staging()
        self.manifest['files'] = original
        self.manifest['files'][0]['mime'] = 'text/html\r\nHeader: value'
        self.write_bundle()
        with self.assertRaisesRegex(ValueError, 'MIME'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_path_traversal_and_unlisted_resources_are_rejected(self):
        for name in ('../escape.txt', '/absolute.txt', 'a\\b.js', 'a/./b.js', 'a//b.js', 'C:/escape.txt', 'unlisted.js'):
            with self.subTest(name=name):
                self.write_bundle(extra={name: b'bad'})
                with self.assertRaisesRegex(ValueError, 'path|inventory'):
                    self.prepare()
                self.assert_no_install_or_staging()

    def test_archive_symlink_is_rejected(self):
        entry = zipfile.ZipInfo('link')
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, 'a') as zipped:
            zipped.writestr(entry, '../escape')
        self.lock['archiveSha256'] = digest(self.archive.read_bytes())
        self.write_lock()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.prepare()
        self.assert_no_install_or_staging()

    def test_nonempty_invalid_output_is_never_overwritten(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination / 'keep.txt'
        sentinel.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'Refusing to overwrite'):
            self.prepare()
        self.assertEqual(sentinel.read_bytes(), b'keep')
        self.assertEqual(list((self.root / '.build').iterdir()), [self.destination])

    def test_corrupt_cache_is_preserved_and_rejected(self):
        self.prepare()
        asset = self.destination / '_app/chunk.js'
        asset.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Refusing to overwrite'):
            self.prepare()
        self.assertEqual(asset.read_bytes(), b'corrupt')

    def test_output_symlink_is_never_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.destination.parent.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_oversized_local_and_download_archives_are_rejected(self):
        with patch.object(prepare_frontend, 'MAX_ARCHIVE_BYTES', 5):
            with self.assertRaisesRegex(ValueError, 'exceeds 1 GiB'):
                self.prepare()
        self.assert_no_install_or_staging()
        response = io.BytesIO(b'123456')
        response.headers = {}
        with patch.object(prepare_frontend, 'MAX_ARCHIVE_BYTES', 5), patch.object(prepare_frontend.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(ValueError, 'exceeds 1 GiB'):
                prepare_frontend.prepare_frontend(self.root)
        self.assert_no_install_or_staging()


if __name__ == '__main__':
    unittest.main()
