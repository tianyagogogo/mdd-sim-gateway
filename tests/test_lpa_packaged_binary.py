import os
import unittest
from unittest.mock import patch

from control.app import lpa


class PackagedLpacTests(unittest.TestCase):
    def test_packaged_binary_replaces_the_absent_legacy_data_binary(self):
        legacy = os.path.join(lpa.cfg.DATA_DIR, "lpac", "lpac")
        with patch.object(lpa.cfg, "get_settings", return_value={
                "esim": {"lpac_bin": legacy}}), \
                patch.object(lpa.os.path, "isfile",
                             side_effect=lambda path: path == "/usr/local/bin/lpac"):
            self.assertEqual(lpa.lpac_bin(), "/usr/local/bin/lpac")

    def test_an_explicit_custom_binary_remains_authoritative(self):
        custom = "/opt/company/lpac"
        with patch.object(lpa.cfg, "get_settings", return_value={
                "esim": {"lpac_bin": custom}}), \
                patch.object(lpa.os.path, "isfile", return_value=False):
            self.assertEqual(lpa.lpac_bin(), custom)


if __name__ == "__main__":
    unittest.main()
