"""Regression guard: recycled knowledge-file links must not appear in KB lists."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = (ROOT / 'backend/open_webui/models/knowledge.py').read_text()


class KnowledgeRecycledLinkTests(unittest.TestCase):
    def test_file_queries_filter_recycled_links(self):
        self.assertIn('def _visible_knowledge_file_link()', MODEL)
        self.assertGreaterEqual(MODEL.count('.filter(_visible_knowledge_file_link())'), 6)


if __name__ == '__main__':
    unittest.main()
