"""Stdlib tests for the server-owned workspace loop.

No app import, no database and no live embedding service: pure helpers are
loaded from source and the integration seams are guarded by source assertions.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'backend/open_webui/utils/workspace_loop.py'
SPEC = importlib.util.spec_from_file_location('workspace_loop_helpers', HELPER)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def read(path: str) -> str:
    return (ROOT / path).read_text()


class WorkspaceLoopHelpersTest(unittest.TestCase):
    def test_canonical_title_is_literal_and_stable(self):
        self.assertEqual(MOD.canonical_note_title(None), 'Untitled')
        self.assertEqual(MOD.canonical_note_title('   '), 'Untitled')
        self.assertEqual(MOD.canonical_note_title('  C 语言  '), '  C 语言  ')

    def test_fingerprint_matches_js_json_stringify_shape(self):
        title = '数组应用与排序'
        content = '中文内容\n第二行'
        expected = hashlib.sha256(
            json.dumps([title, content], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        self.assertEqual(MOD.note_snapshot_fingerprint(title, content), expected)
        # The old Python-default json.dumps form escapes non-ASCII and must not
        # be used for stored frontend-compatible fingerprints.
        old = hashlib.sha256(json.dumps([title, content]).encode('utf-8')).hexdigest()
        self.assertNotEqual(expected, old)
        self.assertEqual(
            MOD.note_snapshot_fingerprint('', 'x'),
            hashlib.sha256(b'["Untitled","x"]').hexdigest(),
        )

    def test_safe_filename(self):
        self.assertEqual(MOD.safe_markdown_filename('a/b:c'), 'a b c.md')
        self.assertEqual(MOD.safe_markdown_filename(None), 'Untitled.md')
        self.assertNotIn('/', MOD.safe_markdown_filename('../../x'))

    def test_structured_error_contract_source(self):
        # The CI checks job installs no FastAPI; keep this a source assertion.
        src = HELPER.read_text()
        self.assertIn('from fastapi import HTTPException', src)
        for key in ("'code': code", "'stage': stage", "'message': message", "'retryable': retryable"):
            self.assertIn(key, src)

    def test_extract_chat_message_shapes(self):
        list_shape = {'messages': [{'id': 'm1', 'content': [{'type': 'text', 'text': 'hello'}]}]}
        self.assertEqual(MOD.extract_chat_message_content(list_shape, 'm1'), 'hello')
        history_shape = {'history': {'messages': {'m2': {'content': {'md': '# note'}}}}}
        self.assertEqual(MOD.extract_chat_message_content(history_shape, 'm2'), '# note')
        self.assertEqual(MOD.extract_chat_message_content(list_shape, 'missing'), '')

    def test_merge_snapshot_does_not_clobber_meta(self):
        meta = {'other': 1, 'note_knowledge': {'kb1': {'file_id': 'f1'}}}
        out = MOD.merge_note_knowledge_snapshot(meta, 'kb2', {'file_id': 'f2'})
        self.assertEqual(out['other'], 1)
        self.assertEqual(out['note_knowledge']['kb1']['file_id'], 'f1')
        self.assertEqual(out['note_knowledge']['kb2']['file_id'], 'f2')
        self.assertEqual(meta['note_knowledge'], {'kb1': {'file_id': 'f1'}})

    def test_classify_snapshot_state_precedence(self):
        good = {
            'kb_exists': True,
            'can_write': True,
            'external': False,
            'file_exists': True,
            'linked': True,
            'fingerprint_matches': True,
        }
        self.assertEqual(MOD.classify_snapshot_state(good), 'synced')
        self.assertEqual(MOD.classify_snapshot_state({**good, 'fingerprint_matches': False}), 'changed')
        self.assertEqual(MOD.classify_snapshot_state({**good, 'index_pending': True}), 'indexing')
        self.assertEqual(MOD.classify_snapshot_state({**good, 'index_failed': True}), 'failed')
        self.assertEqual(MOD.classify_snapshot_state({**good, 'can_write': False}), 'unavailable')


class WorkspaceLoopWiringTest(unittest.TestCase):
    def test_loop_router_is_mounted_before_notes_catchall(self):
        main = read('backend/open_webui/main.py')
        self.assertIn('workspace_loop,', main)
        i = main.index("app.include_router(workspace_loop.router, prefix='/api/v1/notes'")
        j = main.index("app.include_router(notes.router, prefix='/api/v1/notes'")
        self.assertLess(i, j)

    def test_required_endpoints_exist(self):
        src = read('backend/open_webui/routers/workspace_loop.py')
        for needle in (
            "@router.get('/loop/readiness')",
            "@router.get('/{id}/knowledge/readiness')",
            "@router.post('/{id}/knowledge/{knowledge_id}/sync')",
            "@router.delete('/{id}/knowledge/{knowledge_id}/snapshot')",
            "@router.post('/from-chat')",
        ):
            self.assertIn(needle, src)
        self.assertIn('merge_note_knowledge_snapshot', src)
        self.assertIn("data['status'] = state", src)
        self.assertIn('structured_loop_error', src)
        self.assertIn('await check_notes_access(user, db=db)', src)

    def test_indexing_state_writes_both_status_surfaces(self):
        src = read('backend/open_webui/utils/knowledge_indexing.py')
        self.assertIn('Union[asyncio.Task, Coroutine]', src)
        self.assertIn('asyncio.create_task(task)', src)
        self.assertIn("data = {**(row.data or {}), 'status': state", src)
        self.assertIn('values(data=data, meta=meta)', src)

    def test_shutdown_drains_indexing(self):
        src = read('backend/open_webui/main.py')
        self.assertIn('await drain_indexing()', src)

    def test_agent_tool_links_before_indexing(self):
        src = read('backend/open_webui/tools/builtin.py')
        link = src.index('linked = await Knowledges.add_file_to_knowledge_by_id(')
        track = src.index('knowledge_indexing.track_indexing(', link)
        self.assertLess(link, track)

    def test_note_list_exposes_write_access(self):
        router = read('backend/open_webui/routers/notes.py')
        model = read('backend/open_webui/models/notes.py')
        self.assertIn('write_access: Optional[bool] = False', router)
        self.assertIn('_user_note_write_access', router)
        self.assertIn('write_access: Optional[bool] = False', model)


if __name__ == '__main__':
    unittest.main()
