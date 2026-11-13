"""Guard the generic ZIP fast-fail path in the document loader."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'backend/open_webui/retrieval/loaders/main.py').read_text()


class ArchiveLoaderGuardTests(unittest.TestCase):
    def test_generic_zip_is_rejected_before_text_loader(self):
        self.assertIn("file_ext == 'zip'", SOURCE)
        self.assertIn("'application/x-zip-compressed'", SOURCE)
        self.assertIn('ZIP archives are not supported for inline extraction', SOURCE)
        guard = SOURCE.index("file_ext == 'zip'")
        fallback = SOURCE.index('loader = TextLoader(file_path, encoding=self._detect_text_encoding(file_path))')
        self.assertLess(guard, fallback)


if __name__ == '__main__':
    unittest.main()
