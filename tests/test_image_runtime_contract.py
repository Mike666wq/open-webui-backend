"""Source-level guards for runtime dependencies needed by document imports."""

import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / 'Dockerfile').read_text()
CHROMA = (ROOT / 'backend/open_webui/retrieval/vector/dbs/chroma.py').read_text()
WRAPPER = ROOT / 'scripts/soffice-wrapper.sh'


class ImageRuntimeContractTests(unittest.TestCase):
    def test_libreoffice_and_wrapper_are_baked_into_image(self):
        for package in ('libreoffice-impress', 'libreoffice-writer', 'fonts-noto-cjk'):
            self.assertIn(package, DOCKERFILE)
        self.assertIn('scripts/soffice-wrapper.sh /usr/local/bin/soffice', DOCKERFILE)
        self.assertTrue(os.access(WRAPPER, os.X_OK))
        wrapper = WRAPPER.read_text()
        self.assertIn('mktemp -d /tmp/lo-profile', wrapper)
        self.assertIn('HOME="$profile_dir"', wrapper)

    def test_chroma_checks_dimension_before_write(self):
        self.assertIn('_verify_collection_dimension', CHROMA)
        self.assertIn("include=['embeddings']", CHROMA)
        self.assertIn('was created with dim=', CHROMA)
        self.assertIn('self._verify_collection_dimension(collection, len(embeddings[0]))', CHROMA)


if __name__ == '__main__':
    unittest.main()
