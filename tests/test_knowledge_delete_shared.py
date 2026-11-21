"""Regression guard: confirmed knowledge-file removal must allow shared KBs."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILTIN = (ROOT / 'backend/open_webui/tools/builtin.py').read_text()


class KnowledgeDeleteSharedTests(unittest.TestCase):
    def test_confirmed_removal_passes_shared_confirmation(self):
        self.assertIn(
            'knowledge_cleanup(actor, knowledge.id, file_ids=[file_id], confirm_shared=True)',
            BUILTIN,
        )


if __name__ == '__main__':
    unittest.main()
