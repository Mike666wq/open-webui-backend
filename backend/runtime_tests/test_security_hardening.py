"""Regression guards for the security/robustness fixes.

These are static/unit assertions on the specific code paths that were
fail-open or unbounded before; they run without a live server.
"""

import ast
import unittest
from pathlib import Path

BASE = Path(__file__).parents[1] / 'open_webui'


def _source(relative):
    return (BASE / relative).read_text()


class SocketDocumentAuthTests(unittest.TestCase):
    def test_unknown_document_types_fail_closed(self):
        source = _source('socket/main.py')
        tree = ast.parse(source)
        wrapped = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'wrapped'
            and any(
                isinstance(inner, ast.Attribute) and inner.attr == 'get' for inner in ast.walk(node)
            )
        )
        body = ast.unparse(wrapped)
        # A document id that is not note: must return before handler() runs.
        self.assertIn("if not document_id.startswith('note:')", body)
        self.assertIn('return', body)

    def test_normalize_keeps_colon_form(self):
        source = _source('socket/main.py')
        self.assertIn("document_id = 'note:' + document_id[5:]", source)


class UploadBoundTests(unittest.TestCase):
    def test_local_provider_reads_in_chunks_with_limit(self):
        source = _source('storage/provider.py')
        self.assertIn('_upload_byte_limit()', source)
        self.assertIn('file.read(1024 * 1024)', source)
        self.assertIn('errno.EFBIG', source)
        self.assertNotIn('        contents = file.read()\n        if not contents:', source)

    def test_upload_limit_has_a_default_ceiling(self):
        source = _source('storage/provider.py')
        self.assertIn('DEFAULT_MAX_UPLOAD_MB', source)

    def test_oversized_upload_is_rejected(self):
        import io
        from open_webui.storage import provider as storage_provider

        original = storage_provider._upload_byte_limit
        storage_provider._upload_byte_limit = lambda: 16
        try:
            with self.assertRaises(OSError):
                storage_provider.LocalStorageProvider.upload_file(
                    io.BytesIO(b'x' * 64), 'audit-oversize-test.bin', {}
                )
        finally:
            storage_provider._upload_byte_limit = original


class MigrationGuardTests(unittest.TestCase):
    def test_chat_message_meta_migration_is_idempotent(self):
        source = _source('migrations/versions/856c5b02fb54_add_chat_message_meta.py')
        self.assertIn("sa.inspect(op.get_bind()).get_columns('chat_message')", source)
        self.assertIn("if 'meta' not in columns:", source)

    def test_timer_backfill_converts_seconds_to_ns(self):
        source = _source('migrations/versions/d4c1a8e37b62_add_chat_timer_at_and_chat_indexes.py')
        self.assertIn('due_at *= 1_000_000_000', source)
        self.assertIn('100_000_000_000', source)


class ChatUpdateRobustnessTests(unittest.TestCase):
    def test_missing_merged_message_does_not_raise(self):
        source = _source('models/chats.py')
        self.assertIn('merged_messages.get(mid)', source)
        self.assertNotIn("updated['history']['messages'][mid].get('done')", source)

    def test_update_failure_is_logged(self):
        source = _source('models/chats.py')
        self.assertIn("log.exception('Failed to update chat %s', id)", source)


class AccessGrantTests(unittest.TestCase):
    def test_wildcard_user_write_is_dropped(self):
        from open_webui.models.access_grants import normalize_access_grants

        grants = normalize_access_grants([
            {'principal_type': 'user', 'principal_id': '*', 'permission': 'write'},
            {'principal_type': 'user', 'principal_id': '*', 'permission': 'read'},
            {'principal_type': 'anyone', 'principal_id': '*', 'permission': 'write'},
        ])
        self.assertEqual(len(grants), 1, grants)
        self.assertEqual(grants[0]['permission'], 'read')


if __name__ == '__main__':
    unittest.main()


class ProviderConfigContractTests(unittest.TestCase):
    def test_custom_headers_accepted_and_validated(self):
        source = _source('routers/providers.py')
        self.assertEqual(source.count("'custom_headers', 'headers'"), 2)
        self.assertIn('def _validate_custom_headers(', source)
        self.assertIn("lowered == 'authorization'", source)

    def test_custom_headers_rejects_authorization_override(self):
        from fastapi import HTTPException
        from open_webui.routers.providers import _validate_custom_headers

        with self.assertRaises(HTTPException):
            _validate_custom_headers({'custom_headers': {'Authorization': 'Bearer x'}})
        with self.assertRaises(HTTPException):
            _validate_custom_headers({'custom_headers': {'Host': 'evil'}})
        with self.assertRaises(HTTPException):
            _validate_custom_headers({'custom_headers': 'not-a-dict'})
        # A normal gateway header is accepted.
        _validate_custom_headers({'custom_headers': {'X-Api-Version': '2'}})


class LegacyCatalogCacheTests(unittest.TestCase):
    def test_failure_refreshes_updated_at(self):
        source = _source('utils/legacy_catalogs.py')
        self.assertIn("'updated_at': attempt_at", source)

    def test_return_is_never_none(self):
        source = _source('utils/legacy_catalogs.py')
        self.assertIn("copy.deepcopy(entry.get('models')) or []", source)

    def test_task_callbacks_registered_once(self):
        source = _source('utils/legacy_catalogs.py')
        self.assertIn('def _track_task(', source)
        self.assertEqual(source.count('add_done_callback('), 2)


class ProviderMigrationAliasTests(unittest.TestCase):
    def test_every_write_uses_the_filtered_alias_map(self):
        source = _source('utils/provider_migration.py')
        self.assertIn("'_legacy_access_ids': aliases,", source)
        self.assertNotIn("'_legacy_access_ids': source['aliases'],", source)
        # The preview build must still see every alias; only the writes into
        # connection.models and ProviderModel are filtered.
        start = source.index('connection.models = [')
        tail = source[start:]
        self.assertNotIn("source['aliases'].items()", tail)
        self.assertIn('for alias, mid in aliases.items()', tail)
