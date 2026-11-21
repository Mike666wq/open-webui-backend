"""Regression guards for knowledge scope vs access-control semantics."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = (ROOT / 'backend/open_webui/tools/builtin.py').read_text()
KB_FS = (ROOT / 'backend/open_webui/tools/knowledge_fs.py').read_text()


class KnowledgeScopePermissionTests(unittest.TestCase):
    def test_attachment_is_not_an_access_denial(self):
        self.assertNotIn('is not attached to this model', BUILTIN)
        self.assertIn('Access denied to knowledge base', BUILTIN)
        self.assertIn('Explicit IDs are a permission-bound request', BUILTIN)

    def test_list_knowledge_discovers_all_accessible_bases(self):
        self.assertIn('Match the workspace listing: every readable KB is discoverable.', BUILTIN)
        self.assertIn('every readable KB is discoverable', BUILTIN)
        self.assertIn('return [(kb.id, kb.name, kb.description or \'\')]', KB_FS)


if __name__ == '__main__':
    unittest.main()
