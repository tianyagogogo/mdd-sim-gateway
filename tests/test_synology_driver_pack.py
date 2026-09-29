import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "drivers" / "packs" / "synology-ds1621plus-dsm7.4.1"
MANIFEST = json.loads((ROOT / "runtime" / "synology-v1000-7.4-modules.json").read_text())
CATALOG = json.loads((ROOT / "drivers" / "catalog" /
                      "synology-ds1621plus-dsm-7.4.1-90080-k4.4.302plus-x86_64.json").read_text())
SHA256 = re.compile(r"[0-9a-f]{64}")


class SynologyDriverPackTests(unittest.TestCase):
    def test_loader_is_valid_shell_and_covers_exact_manifest(self):
        loader = ROOT / "runtime" / "synology-load-modules.sh"
        subprocess.run(["sh", "-n", str(loader)], check=True)
        text = loader.read_text(encoding="utf-8")
        for filename in MANIFEST["modules"]:
            self.assertIn(Path(filename).stem.replace("-", "_"), text)
        self.assertIn("sha256sum -c SHA256SUMS", text)
        self.assertIn('"$(uname -r)" = "$MDD_KERNEL"', text)

    def test_installer_refuses_before_it_touches_the_system(self):
        script = (PACK / "install.sh").read_text(encoding="utf-8")
        subprocess.run(["sh", "-n", str(PACK / "install.sh")], check=True)
        first_write = script.index("install -d")
        for check in ('"$(uname -m)" = "$MDD_ARCH"', '"$(uname -r)" = "$MDD_KERNEL"',
                      '"$product-$build" = "$MDD_DSM"', "$MDD_PLATFORM_MARKER",
                      "sha256sum -c SHA256SUMS"):
            self.assertLess(script.index(check), first_write, check)

    def test_uninstall_never_forces_a_module_out(self):
        script = (PACK / "uninstall.sh").read_text(encoding="utf-8")
        subprocess.run(["sh", "-n", str(PACK / "uninstall.sh")], check=True)
        self.assertNotIn("rmmod -f", script)
        self.assertNotIn("docker", script)

    def test_every_build_input_is_pinned(self):
        inputs = MANIFEST["inputs"]
        for toolkit in ("synology_dev", "synology_env"):
            self.assertRegex(inputs[toolkit]["sha256"], SHA256)
        self.assertTrue(inputs["linux_source"]["unmodified"])
        self.assertRegex(inputs["linux_source"]["copying_sha256"], SHA256)
        for path, digest in inputs["linux_source"]["files"].items():
            self.assertRegex(digest, SHA256, path)
        for module, digest in MANIFEST["modules"].items():
            self.assertRegex(digest, SHA256, module)

    def test_catalogue_names_the_pack_this_build_produces(self):
        pack = CATALOG["driver_pack"]
        self.assertEqual(pack["status"], "published")
        self.assertEqual(pack["asset"], MANIFEST["pack"]["name"] + ".tar.gz")
        self.assertRegex(pack["sha256"], SHA256)


if __name__ == "__main__":
    unittest.main()
