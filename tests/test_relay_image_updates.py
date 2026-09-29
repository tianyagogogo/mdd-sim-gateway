"""The media relay image in releases, updates and rollbacks.

The relay is upstream coturn, unmodified, shipped as a checksummed Release asset. It is optional:
only a gateway in relay mode fetches it, a failed fetch never fails an update, and a rollback
removes the relay so a Control that predates relay mode cannot leave its port open.
Full-container (Compose) paths here are covered by these tests only; they have not been run
on a real container-stack deployment.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import mdd_container_update, mdd_update


class RelayModeTests(unittest.TestCase):
    def test_only_a_recorded_relay_mode_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            self.assertFalse(mdd_container_update.relay_mode(project))
            (project / "media").mkdir()
            state = project / "media" / "state.json"
            state.write_text(json.dumps({"mode": "direct"}))
            self.assertFalse(mdd_container_update.relay_mode(project))
            state.write_text("{not json")
            self.assertFalse(mdd_container_update.relay_mode(project))
            state.write_text(json.dumps({"mode": "relay", "secret": "x"}))
            self.assertTrue(mdd_container_update.relay_mode(project))


class ContainerUpdateRelayTests(unittest.TestCase):
    def fetch(self, fetch_error=None, verify_error=None):
        with tempfile.TemporaryDirectory() as temp:
            staging = Path(temp)
            with patch.object(mdd_update, "fetch_release_asset",
                              side_effect=fetch_error, return_value=2) as fetch, \
                    patch.object(mdd_update, "verify_release_file",
                                 side_effect=verify_error) as verify, \
                    patch.object(mdd_update, "load_relay_image") as load, \
                    patch("sys.stderr"):
                active = mdd_container_update.fetch_relay_image(
                    "https://example.invalid/v1.13.0", "1.13.0", "arm64",
                    staging / "SHA256SUMS", [{}], 1, {}, staging)
        return active, fetch, verify, load

    def test_the_verified_asset_is_loaded(self):
        active, fetch, verify, load = self.fetch()
        self.assertEqual(active, 2)
        self.assertTrue(fetch.call_args[0][0].endswith("/mdd-sim-gateway-relay-v1.13.0-arm64.tar.gz"))
        verify.assert_called_once()
        load.assert_called_once()

    def test_a_download_failure_never_fails_the_update(self):
        active, _fetch, verify, load = self.fetch(
            fetch_error=mdd_update.UpdateError("offline"))
        self.assertEqual(active, 1)
        verify.assert_not_called()
        load.assert_not_called()

    def test_an_asset_failing_its_checksum_is_never_loaded(self):
        _active, _fetch, _verify, load = self.fetch(
            verify_error=mdd_update.UpdateError("checksum mismatch"))
        load.assert_not_called()

    def test_a_rollback_removes_only_the_managed_relay(self):
        relay = Mock(attrs={"Config": {"Labels": {"io.mdd-sim-gateway.managed": "true",
                                                   "io.mdd-sim-gateway.component": "relay"}}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: relay))
        mdd_container_update.remove_relay(client)
        relay.remove.assert_called_once_with(force=True, v=True)

        foreign = Mock(attrs={"Config": {"Labels": {}}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: foreign))
        mdd_container_update.remove_relay(client)
        foreign.remove.assert_not_called()

    def test_a_docker_error_never_fails_the_rollback(self):
        def get(name):
            raise RuntimeError("daemon busy")
        with patch("sys.stderr"):
            mdd_container_update.remove_relay(
                SimpleNamespace(containers=SimpleNamespace(get=get)))

    def test_the_rollback_path_calls_it(self):
        source = Path(mdd_container_update.__file__).read_text()
        rollback = source[source.index('status.publish("running", "rollback"'):]
        self.assertIn("remove_relay(client)", rollback[:rollback.index("rollback_ok = True")])


class RelayAssetLoadTests(unittest.TestCase):
    def test_the_loaded_image_must_match_this_host(self):
        results = [SimpleNamespace(returncode=0, stdout="", stderr=""),
                   SimpleNamespace(returncode=0, stdout="riscv64\n", stderr="")]
        with patch.object(mdd_update.subprocess, "run", side_effect=results), \
                patch.object(mdd_update, "host_arch", return_value="amd64"):
            with self.assertRaisesRegex(mdd_update.UpdateError, "identity mismatch"):
                mdd_update.load_relay_image(Path("relay.tar.gz"), "1.13.0")

    def test_it_is_loaded_under_the_local_tag_the_control_plane_runs(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="amd64\n", stderr="")

        with patch.object(mdd_update.subprocess, "run", side_effect=run), \
                patch.object(mdd_update, "host_arch", return_value="amd64"):
            image = mdd_update.load_relay_image(Path("relay.tar.gz"), "1.13.0")
        self.assertEqual(image, "mdd-sim-gateway/relay:v1.13.0")
        self.assertIn("mdd-sim-gateway/relay:v1.13.0", calls[1])


if __name__ == "__main__":
    unittest.main()
