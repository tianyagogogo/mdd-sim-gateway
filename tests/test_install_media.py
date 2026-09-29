"""install.sh: the relay image and the media-mode switch.

The shell functions are cut out of install.sh and run with docker replaced by a stub that
records how it was called and answers as configured.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def shell_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL, re.M | re.S)
    assert match, name
    return match.group(0)


class InstallMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        (self.repo / "engine").mkdir(parents=True)
        (self.repo / "host").mkdir()
        (self.repo / "VERSION").write_text("1.13.0\n")
        self.manifest = self.repo / "engine" / "release-image.SHA256SUMS"
        self.data = root / "data"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"

    def tearDown(self):
        self.temp.cleanup()

    def stub(self, name, rc=0):
        tool = self.bin / name
        tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\nexit {rc}\n')
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)

    def run_installer(self, *calls: str, present=False, importer_rc=0):
        self.log.write_text("")
        self.stub("docker", 0 if present else 1)
        self.stub("python3", importer_rc)
        script = "\n".join([
            'info() { echo "info: $*"; }',
            'warn() { echo "warn: $*"; }',
            'have() { command -v "$1" >/dev/null 2>&1; }',
            f'REPO_DIR="{self.repo}"',
            f'MDD_DATA_DIR="{self.data}"',
            f'ENGINE_HANDOFF_MANIFEST="{self.manifest}"',
            shell_function("relay_image_ref"),
            shell_function("media_mode_recorded"),
            shell_function("ensure_relay_image"),
            *calls,
        ])
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"})

    def calls(self, tool):
        return [line for line in self.log.read_text().splitlines() if line.startswith(tool)]

    def test_the_relay_runs_from_a_local_tag_named_after_this_version(self):
        out = self.run_installer("relay_image_ref")
        self.assertEqual(out.stdout, "mdd-sim-gateway/relay:v1.13.0")

    def test_a_present_image_is_left_alone(self):
        self.manifest.write_text("x  mdd-sim-gateway-relay-v1.13.0-amd64.tar.gz\n")
        self.assertEqual(self.run_installer("ensure_relay_image", present=True).returncode, 0)
        self.assertEqual(self.calls("python3"), [])

    def test_an_official_release_imports_its_relay_asset(self):
        self.manifest.write_text("x  mdd-sim-gateway-relay-v1.13.0-amd64.tar.gz\n")
        out = self.run_installer("ensure_relay_image")
        self.assertEqual(out.returncode, 0)
        [call] = self.calls("python3")
        self.assertIn("--install-relay-image", call)
        self.assertIn("--version 1.13.0", call)
        self.assertIn("imported the media relay image", out.stdout)

    def test_a_failed_import_is_not_fatal(self):
        self.manifest.write_text("x  mdd-sim-gateway-relay-v1.13.0-amd64.tar.gz\n")
        out = self.run_installer("ensure_relay_image; echo rc=$?", importer_rc=1)
        self.assertIn("rc=0", out.stdout)
        self.assertIn("control plane will try the registries", out.stdout)

    def test_a_checkout_or_an_older_release_leaves_it_to_the_control_plane(self):
        self.assertEqual(self.run_installer("ensure_relay_image").returncode, 0)
        self.manifest.write_text("x  mdd-sim-gateway-engine-v1.13.0-amd64.tar.gz\n")
        self.assertEqual(self.run_installer("ensure_relay_image").returncode, 0)
        self.assertEqual(self.calls("python3"), [])

    def test_the_recorded_mode_is_direct_until_relay_is_written(self):
        self.assertEqual(self.run_installer("media_mode_recorded").stdout.strip(), "direct")
        (self.data / "media").mkdir(parents=True)
        (self.data / "media" / "state.json").write_text('{\n  "mode": "relay"\n}\n')
        self.assertEqual(self.run_installer("media_mode_recorded").stdout.strip(), "relay")

    def test_reload_looks_for_the_relay_image_only_in_relay_mode(self):
        reload = shell_function("cmd_reload")
        self.assertRegex(reload, r'if \[ "\$\(media_mode_recorded\)" = relay \]; then\n'
                                 r'\s+ensure_relay_image\n')


if __name__ == "__main__":
    unittest.main()
