"""Guard the smoke container network topology against regressions.

The smoke checks must reach the candidate container over a host loopback port
while the container itself has no external network. --internal networks
silently drop published ports (moby/moby#53256), which previously surfaced as
an opaque ``KeyError: '8080/tcp'``. Lock the working configuration in place.
"""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'scripts' / 'image_smoke.py').read_text()


class ImageSmokeNetworkTests(unittest.TestCase):
    def test_internal_network_is_not_used(self):
        # --internal makes Docker ignore --publish, so the host can never
        # reach the container and ports['8080/tcp'] comes back None.
        self.assertNotIn("'--internal'", SOURCE)

    def test_bridge_disables_masquerade_and_blackholes_dns(self):
        self.assertIn("'--driver', 'bridge'", SOURCE)
        self.assertRegex(SOURCE, r"'--opt',\s*'com\.docker\.network\.bridge\.ip_masq=false'")
        self.assertRegex(SOURCE, r"'--dns',\s*'169\.254\.169\.254'")

    def test_port_binding_is_validated_before_use(self):
        # Fail with a readable message instead of an opaque KeyError when the
        # host port mapping is missing again.
        self.assertIn("ports.get('8080/tcp')", SOURCE)
        self.assertIn('127.0.0.1::8080', SOURCE)


if __name__ == '__main__':
    unittest.main()
