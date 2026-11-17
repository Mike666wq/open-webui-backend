import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'backend/open_webui/utils/legacy_office_preview.py'
_spec = importlib.util.spec_from_file_location('legacy_office_preview', MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules['legacy_office_preview'] = _module
_spec.loader.exec_module(_module)
convert_legacy_ppt_to_pptx = _module.convert_legacy_ppt_to_pptx


class LegacyOfficePreviewTests(unittest.TestCase):
    def test_converts_ppt_to_pptx_in_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'slides.ppt'
            source.write_bytes(b'legacy')
            output = root / 'out'

            def fake_run(command, **kwargs):
                (output / 'slides.pptx').write_bytes(b'pptx')
                return subprocess.CompletedProcess(command, 0, stdout='converted', stderr='')

            with patch('legacy_office_preview.subprocess.run', side_effect=fake_run):
                result = convert_legacy_ppt_to_pptx(source, output)

            self.assertEqual(result.name, 'slides.pptx')
            self.assertEqual(result.read_bytes(), b'pptx')

    def test_reports_missing_libreoffice_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                'legacy_office_preview.subprocess.run',
                side_effect=FileNotFoundError,
            ):
                with self.assertRaisesRegex(RuntimeError, 'LibreOffice'):
                    convert_legacy_ppt_to_pptx(Path(tmp) / 'slides.ppt', Path(tmp) / 'out')


if __name__ == '__main__':
    unittest.main()
