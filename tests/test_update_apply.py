"""One-click self-update: control-plane request publishing + host updater file handling."""
import importlib.util
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
import requests
from pathlib import Path
from unittest.mock import Mock, patch

from control.app import config, update_check
from host import mdd_orchestrator

_SPEC = importlib.util.spec_from_file_location(
    "mdd_update", Path(__file__).resolve().parent.parent / "host" / "mdd_update.py")
mdd_update = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mdd_update)

_AVAILABLE = {"ok": True, "update_available": True, "latest": "9.9.9",
              "release_url": "https://example.invalid/release"}


class ReleaseWorkflowTests(unittest.TestCase):
    def test_all_four_native_image_sets_are_checksummed_and_published(self):
        workflow = (Path(__file__).resolve().parent.parent /
                    ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('docker save --output "$engine_archive"', workflow)
        self.assertIn("name: engine-image-${{ matrix.arch }}", workflow)
        self.assertIn("name: control-image-${{ matrix.arch }}", workflow)
        self.assertIn("name: runtime-images-${{ matrix.arch }}", workflow)
        for kind in ("engine", "control", "hardware", "egress"):
            for arch in ("arm64", "amd64"):
                asset = f'mdd-sim-gateway-{kind}-${{GITHUB_REF_NAME}}-{arch}.tar.gz'
                # Embedded manifest, top-level sums, and gh release create.
                self.assertEqual(workflow.count(f'"{asset}"'), 3, asset)
        self.assertIn('> "$root/engine/release-image.SHA256SUMS"', workflow)
        self.assertIn('for component in engine control hardware egress', workflow)
        compose = 'mdd-sim-gateway-compose-${GITHUB_REF_NAME}.yaml'
        self.assertEqual(workflow.count(f'"{compose}"'), 2)
        self.assertIn('sha256sum "$archive" \\\n            "$compose_asset"', workflow)
        self.assertIn('source = source.replace(marker, os.environ["GITHUB_REF_NAME"])', workflow)
        self.assertIn('"/volume1/docker/mdd-sim-gateway"', workflow)
        self.assertIn('"192.168.1.100"', workflow)
        self.assertIn("! grep -F '${MDD_DATA_DIR'", workflow)
        self.assertIn('docker compose -f "$compose_asset" config --quiet', workflow)

    def test_runtime_images_carry_release_identity_and_ownership(self):
        root = Path(__file__).resolve().parent.parent
        for component in ("hardware", "egress"):
            dockerfile = (root / "runtime" / f"Dockerfile.{component}").read_text()
            self.assertIn("ARG MDD_VERSION=dev", dockerfile)
            self.assertIn(f'io.mdd-sim-gateway.component="{component}"'
                          if component == "hardware"
                          else f"io.mdd-sim-gateway.component={component}", dockerfile)
            self.assertIn("io.mdd-sim-gateway.managed=", dockerfile)
            self.assertIn('org.opencontainers.image.version="${MDD_VERSION}"', dockerfile)


class RequestApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request_path = os.path.join(self.tmp.name, "orchestrator", "update-request.json")
        self.status_path = os.path.join(self.tmp.name, "orchestrator", "update-status.json")

    def test_request_is_published_with_version_and_repository(self):
        available = {**_AVAILABLE, "network": {
            "proxy_mode": "library", "proxy_profile_id": "primary"}}
        with patch.object(update_check, "check", return_value=available):
            result = update_check.request_apply()
        self.assertTrue(result["ok"])
        with open(self.request_path, encoding="utf-8") as handle:
            request = json.load(handle)
        self.assertEqual(request["version"], "9.9.9")
        self.assertEqual(request["repository"], update_check.repository())
        self.assertEqual(request["network"]["proxy_profile_id"], "primary")
        self.assertEqual(request["networks"][0]["proxy_profile_id"], "primary")
        with open(self.status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["phase"], "requested")

    def test_no_available_update_publishes_nothing(self):
        with patch.object(update_check, "check",
                          return_value={"ok": True, "update_available": False}):
            result = update_check.request_apply()
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(self.request_path))

    def test_selected_test_version_is_resolved_before_request_is_published(self):
        selected = {**_AVAILABLE, "latest": "9.9.9-rc1", "prerelease": True,
                    "network": {"proxy_mode": "direct", "proxy_profile_id": ""}}
        with patch.object(update_check, "check_release", return_value=selected) as lookup:
            result = update_check.request_apply(version="9.9.9-rc1")
        self.assertTrue(result["ok"])
        lookup.assert_called_once_with("9.9.9-rc1", allow_prerelease=True,
                                       allow_older=True)
        with open(self.request_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["version"], "9.9.9-rc1")

    def test_invalid_selected_version_is_rejected_without_lookup(self):
        with patch.object(update_check, "check_release") as lookup:
            result = update_check.request_apply(version="../../main")
        self.assertEqual(result["error_code"], "update.error.invalid_version")
        lookup.assert_not_called()
        self.assertFalse(os.path.exists(self.request_path))

    def test_running_update_is_not_requested_twice(self):
        os.makedirs(os.path.dirname(self.status_path))
        with open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump({"state": "running", "phase": "reloading",
                       "updated_at": int(time.time())}, handle)
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)):
            result = update_check.request_apply()
        self.assertEqual(result["error_code"], "update.error.in_progress")
        self.assertFalse(os.path.exists(self.request_path))

    def test_unconsumed_request_is_reported_as_stalled(self):
        os.makedirs(os.path.dirname(self.request_path))
        with open(self.request_path, "w", encoding="utf-8") as handle:
            json.dump({"version": "9.9.9", "requested_at": int(time.time()) - 300}, handle)
        status = update_check.apply_status()
        self.assertTrue(status["requested"])
        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["error_code"], "update.error.not_picked_up")

    def _publish_status(self, **fields):
        os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
        with open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump({"state": "running", "phase": "downloading", **fields}, handle)

    def test_a_progress_document_nothing_refreshes_stops_counting_as_live(self):
        """An updater killed with its host left the dialog resuming into it forever."""
        self._publish_status(updated_at=int(time.time()) - 20 * 60)
        status = update_check.apply_status()
        self.assertEqual(status["state"], "running")
        self.assertTrue(status["stale"])

        self._publish_status(updated_at=int(time.time()) - 7 * 3600)
        status = update_check.apply_status()
        self.assertEqual(status["state"], "stalled")
        self.assertEqual(status["error_code"], "update.error.abandoned")

    def test_a_live_progress_document_is_neither_stale_nor_cancellable(self):
        self._publish_status(updated_at=int(time.time()))
        status = update_check.apply_status()
        self.assertFalse(status["stale"])
        result = update_check.cancel_apply()
        self.assertEqual(result["error_code"], "update.error.in_progress")
        self.assertTrue(os.path.exists(self.status_path))

    def test_an_abandoned_run_can_be_cancelled_and_requested_again(self):
        self._publish_status(updated_at=int(time.time()) - 20 * 60)
        self.assertTrue(update_check.cancel_apply()["ok"])
        self.assertFalse(os.path.exists(self.status_path))
        self.assertFalse(os.path.exists(self.request_path))
        # Without cancelling, the same stale document must not block a fresh request either.
        self._publish_status(updated_at=int(time.time()) - 20 * 60)
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)):
            self.assertTrue(update_check.request_apply()["ok"])


class UpdaterTests(unittest.TestCase):
    def test_docker_control_recreation_preserves_node_test_runtime(self):
        installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text(
            encoding="utf-8")
        start = installer.index("run_control() {")
        end = installer.index("\n}\n", start)
        run_control = installer[start:end]

        self.assertIn(
            "-v /usr/local/bin/sing-box:/usr/local/bin/sing-box:ro", run_control)
        self.assertIn("-v /usr/local/bin/xray:/usr/local/bin/xray:ro", run_control)
        self.assertIn('-v "${REPO_DIR}/host:/app/host:ro"', run_control)
        self.assertIn("-e MDD_SINGBOX_BIN=/usr/local/bin/sing-box", run_control)
        self.assertIn("-e MDD_XRAY_BIN=/usr/local/bin/xray", run_control)

    def test_reload_reuses_satisfied_python_requirements_offline(self):
        installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text(
            encoding="utf-8")
        offline = 'python" -m pip install --quiet --no-index'
        online = 'python" -m pip install --quiet wheel'
        self.assertIn(offline, installer)
        self.assertIn(online, installer)
        self.assertLess(installer.index(offline), installer.index(online))
        self.assertNotIn('pip" install --quiet --upgrade pip wheel', installer)

    def test_release_archive_checksum_is_required_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "mdd-sim-gateway-v9.9.9.tar.gz")
            archive.write_bytes(b"release")
            digest = hashlib.sha256(b"release").hexdigest()
            sums = Path(tmp, "SHA256SUMS")
            sums.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            mdd_update.verify_release_archive(archive, sums)
            archive.write_bytes(b"changed")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.verify_release_archive(archive, sums)

    def test_proxy_environment_is_explicit_and_not_added_to_curl_arguments(self):
        proxy = "socks5h://user:secret@127.0.0.1:1080"
        env = mdd_update.network_environment(proxy)
        self.assertEqual(env["ALL_PROXY"], proxy)
        process = SimpleNamespace(returncode=0)
        process.poll = Mock(side_effect=[None, 0])
        process.communicate = Mock(return_value=("", ""))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                mdd_update.subprocess, "Popen", return_value=process) as popen, \
                patch.object(mdd_update.time, "sleep"):
            status = mdd_update.Status(Path(tmp, "status.json"), "9.9.9")
            mdd_update.download("https://example.invalid/release.tar.gz",
                                Path(tmp, "release.tar.gz"), env, proxy, status=status,
                                artifact="release.tar.gz", total_bytes=1234,
                                route="library", route_name="Primary")
            published = json.loads(Path(tmp, "status.json").read_text())
        args = popen.call_args.args[0]
        self.assertNotIn(proxy, args)
        self.assertEqual(popen.call_args.kwargs["env"]["HTTPS_PROXY"], proxy)
        self.assertEqual(published["phase"], "downloading")
        self.assertEqual(published["artifact"], "release.tar.gz")
        self.assertEqual(published["total_bytes"], 1234)
        self.assertEqual(published["route_name"], "Primary")

    def test_engine_asset_gets_a_bounded_large_file_timeout(self):
        process = SimpleNamespace(returncode=0, poll=Mock(return_value=0),
                                  communicate=Mock(return_value=("", "")))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.subprocess, "Popen", return_value=process) as popen:
            mdd_update.download(
                "https://example.invalid/engine.tar.gz", Path(tmp, "engine.tar.gz"), {},
                artifact="engine.tar.gz", phase="engine_image")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--max-time") + 1], "1800")

    def test_control_asset_gets_the_same_large_file_timeout(self):
        process = SimpleNamespace(returncode=0, poll=Mock(return_value=0),
                                  communicate=Mock(return_value=("", "")))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.subprocess, "Popen", return_value=process) as popen:
            mdd_update.download(
                "https://example.invalid/control.tar.gz", Path(tmp, "control.tar.gz"), {},
                artifact="control.tar.gz", phase="control_image")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--max-time") + 1], "1800")

    def test_transfer_rate_follows_the_recent_window_not_the_whole_download(self):
        """A minute lost to curl's connect retries must not depress the speed, and with it the
        remaining-time estimate, for the rest of the transfer."""
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp, "release.tar.gz")
            sizes = [0, 2_000_000, 4_000_000]  # stalled, then 1 MB/s over 2s polls

            def poll():
                if not sizes:
                    return 0
                destination.write_bytes(b"\0" * sizes.pop(0))
                return None

            process = SimpleNamespace(returncode=0, poll=poll,
                                      communicate=Mock(return_value=("", "")))
            with patch.object(mdd_update.subprocess, "Popen", return_value=process), \
                    patch.object(mdd_update.time, "sleep"), \
                    patch.object(mdd_update.time, "monotonic",
                                 side_effect=[0.0, 2.0, 4.0, 6.0]):
                status = mdd_update.Status(Path(tmp, "status.json"), "9.9.9")
                mdd_update.download("https://example.invalid/release.tar.gz", destination,
                                    {}, status=status, artifact="release.tar.gz",
                                    total_bytes=6_000_000)
            published = json.loads(Path(tmp, "status.json").read_text())
        self.assertEqual(published["downloaded_bytes"], 4_000_000)
        self.assertEqual(published["total_bytes"], 6_000_000)
        # Averaged over the whole download this would read ~666 KB/s and promise three seconds
        # too many; the window sees the megabyte per second the transfer is actually doing.
        self.assertGreater(published["bytes_per_second"], 900_000)
        self.assertLessEqual(published["bytes_per_second"], 1_000_000)

    def test_verified_control_image_is_loaded_and_identity_checked(self):
        completed = lambda code=0, out="", err="": type(
            "Completed", (), {"returncode": code, "stdout": out, "stderr": err})()
        calls = [completed(0, "sha256:old\n"), completed(), completed(0, "Loaded image\n"),
                 completed(0, "arm64|9.9.9\n")]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="aarch64"), \
                patch.object(mdd_update.subprocess, "run", side_effect=calls) as run:
            artifact = Path(tmp, "control.tar.gz")
            artifact.write_bytes(b"image")
            mdd_update.load_control_image(artifact, "9.9.9")
        self.assertEqual(run.call_args_list[2].args[0][:3], ["docker", "load", "--input"])

    def test_verified_runtime_image_is_loaded_and_identity_checked(self):
        completed = lambda code=0, out="", err="": type(
            "Completed", (), {"returncode": code, "stdout": out, "stderr": err})()
        calls = [completed(0, "sha256:old\n"), completed(), completed(0, "Loaded image\n"),
                 completed(0, "amd64|hardware|true|9.9.9\n")]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                patch.object(mdd_update.subprocess, "run", side_effect=calls) as run:
            artifact = Path(tmp, "hardware.tar.gz")
            artifact.write_bytes(b"image")
            mdd_update.load_runtime_image(artifact, "9.9.9", "hardware")
        self.assertEqual(run.call_args_list[2].args[0][:3], ["docker", "load", "--input"])

    def test_runtime_image_mismatch_restores_previous_tag(self):
        completed = lambda code=0, out="", err="": type(
            "Completed", (), {"returncode": code, "stdout": out, "stderr": err})()
        calls = [completed(0, "sha256:old\n"), completed(), completed(),
                 completed(0, "amd64|control|true|9.9.9\n"), completed()]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                patch.object(mdd_update.subprocess, "run", side_effect=calls) as run:
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.load_runtime_image(
                    Path(tmp, "hardware.tar.gz"), "9.9.9", "hardware")
        self.assertEqual(run.call_args_list[-1].args[0], [
            "docker", "tag", "mdd-sim-gateway/hardware:previous",
            "mdd-sim-gateway/hardware"])

    def test_release_engine_archive_is_loaded_and_identity_checked_before_install(self):
        runtime_fp, base_fp = "a" * 64, "b" * 64
        process = SimpleNamespace(returncode=0)
        process.poll = Mock(side_effect=[None, 0])
        inspected = SimpleNamespace(
            returncode=0,
            stdout=f"arm64|9.9.9|{runtime_fp}|{base_fp}\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="aarch64"), \
                patch.object(mdd_update.subprocess, "Popen", return_value=process) as popen, \
                patch.object(mdd_update.subprocess, "run", return_value=inspected), \
                patch.object(mdd_update.time, "sleep"):
            status = mdd_update.Status(Path(tmp, "status.json"), "9.9.9")
            archive = Path(tmp, "mdd-sim-gateway-engine-v9.9.9-arm64.tar.gz")
            image = mdd_update.load_release_engine(
                archive, "9.9.9", runtime_fp, base_fp, status, Path(tmp, "engine.log"))
        self.assertEqual(image, "ghcr.io/mddidd/mdd-sim-gateway-engine:v9.9.9")
        self.assertEqual(popen.call_args.args[0],
                         ["docker", "load", "--input", str(archive)])

    def test_amd64_release_engine_identity_is_accepted_on_amd64(self):
        runtime_fp, base_fp = "a" * 64, "b" * 64
        process = SimpleNamespace(returncode=0, poll=Mock(return_value=0))
        inspected = SimpleNamespace(
            returncode=0,
            stdout=f"amd64|9.9.9|{runtime_fp}|{base_fp}\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                patch.object(mdd_update.subprocess, "Popen", return_value=process), \
                patch.object(mdd_update.subprocess, "run", return_value=inspected):
            image = mdd_update.load_release_engine(
                Path(tmp, "engine.tar.gz"), "9.9.9", runtime_fp, base_fp, None,
                Path(tmp, "engine.log"))
        self.assertEqual(image, "ghcr.io/mddidd/mdd-sim-gateway-engine:v9.9.9")

    def test_release_engine_identity_mismatch_is_rejected(self):
        process = SimpleNamespace(returncode=0, poll=Mock(return_value=0))
        inspected = SimpleNamespace(returncode=0, stdout="amd64|9.9.9|bad|bad\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.subprocess, "Popen", return_value=process), \
                patch.object(mdd_update.subprocess, "run", return_value=inspected):
            status = mdd_update.Status(Path(tmp, "status.json"), "9.9.9")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.load_release_engine(
                    Path(tmp, "engine.tar.gz"), "9.9.9", "a" * 64, "b" * 64,
                    status, Path(tmp, "engine.log"))

    def test_old_updater_handoff_uses_the_release_routes_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data = base / "repo", base / "data"
            manifest = repo / mdd_update.ENGINE_HANDOFF_MANIFEST
            manifest.parent.mkdir(parents=True)
            data.mkdir()
            engine_name = "mdd-sim-gateway-engine-v9.9.9-arm64.tar.gz"
            payload = b"verified engine archive"
            manifest.write_text(
                f"{hashlib.sha256(payload).hexdigest()}  {engine_name}\n",
                encoding="utf-8")
            proxy = "socks5h://127.0.0.1:1080"
            network = data / "update/network.json"
            network.parent.mkdir()
            network.write_text(json.dumps({"routes": [
                {"proxy_url": "", "route": "direct", "route_name": ""},
                {"proxy_url": proxy, "route": "library", "route_name": "Primary"},
            ], "asset_sizes": {engine_name: len(payload)}}), encoding="utf-8")
            attempts = []

            def fake_download(_url, destination, _env, proxy_url="", **_kwargs):
                attempts.append(proxy_url)
                if not proxy_url:
                    raise mdd_update.UpdateError("direct route stalled")
                destination.write_bytes(payload)

            distributed = "ghcr.io/mddidd/mdd-sim-gateway-engine:v9.9.9"
            with patch.object(mdd_update.platform, "machine", return_value="aarch64"), \
                    patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update.shutil, "disk_usage",
                                 return_value=SimpleNamespace(free=3 * 1024 ** 3)), \
                    patch.object(mdd_update, "release_engine_fingerprints",
                                 return_value=("a" * 64, "b" * 64)), \
                    patch.object(mdd_update, "load_release_engine",
                                 return_value=distributed) as load_engine:
                actual = mdd_update.perform_engine_handoff(
                    repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", network)

            self.assertEqual(actual, distributed)
            self.assertEqual(attempts, ["", proxy])
            self.assertEqual(load_engine.call_args.args[0].name, engine_name)
            self.assertTrue(network.exists())  # the still-running old updater owns this file

    def test_fresh_amd64_docker_install_imports_only_amd64_release_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data = base / "repo", base / "data"
            manifest = repo / mdd_update.ENGINE_HANDOFF_MANIFEST
            manifest.parent.mkdir(parents=True)
            data.mkdir()
            engine_name = "mdd-sim-gateway-engine-v9.9.9-amd64.tar.gz"
            control_name = "mdd-sim-gateway-control-v9.9.9-amd64.tar.gz"
            manifest.write_text(
                f"{'a' * 64}  {engine_name}\n{'b' * 64}  {control_name}\n",
                encoding="utf-8")
            downloads = []

            def fake_fetch(_url, destination, _name, _routes, active_route=0, **_kwargs):
                downloads.append(destination.name)
                destination.write_bytes(b"asset")
                return active_route

            distributed = "ghcr.io/mddidd/mdd-sim-gateway-engine:v9.9.9"
            with patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                    patch.object(mdd_update, "fetch_release_asset",
                                 side_effect=fake_fetch), \
                    patch.object(mdd_update.shutil, "disk_usage",
                                 return_value=SimpleNamespace(free=4 * 1024 ** 3)), \
                    patch.object(mdd_update, "verify_release_file") as verify, \
                    patch.object(mdd_update, "release_engine_fingerprints",
                                 return_value=("c" * 64, "d" * 64)), \
                    patch.object(mdd_update, "load_release_engine",
                                 return_value=distributed) as load_engine, \
                    patch.object(mdd_update, "load_control_image") as load_control:
                actual = mdd_update.perform_release_image_install(
                    repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", "docker")

            self.assertEqual(actual, distributed)
            self.assertEqual(downloads, [engine_name, control_name])
            self.assertEqual(load_engine.call_args.args[0].name, engine_name)
            self.assertEqual(load_control.call_args.args[0].name, control_name)
            self.assertEqual(
                {call.args[2] for call in verify.call_args_list},
                {"amd64 Engine image", "amd64 control image"})
            self.assertFalse(any("arm64" in name for name in downloads))

    def test_full_container_install_imports_all_four_native_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data = base / "repo", base / "data"
            manifest = repo / mdd_update.ENGINE_HANDOFF_MANIFEST
            manifest.parent.mkdir(parents=True)
            data.mkdir()
            names = [f"mdd-sim-gateway-{component}-v9.9.9-amd64.tar.gz"
                     for component in ("engine", "control", "hardware", "egress")]
            manifest.write_text("".join(
                f"{'a' * 64}  {name}\n" for name in names), encoding="utf-8")
            downloads = []

            def fake_fetch(_url, destination, _name, _routes, active_route=0, **_kwargs):
                downloads.append(destination.name)
                destination.write_bytes(b"asset")
                return active_route

            with patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                    patch.object(mdd_update, "fetch_release_asset", side_effect=fake_fetch), \
                    patch.object(mdd_update.shutil, "disk_usage",
                                 return_value=SimpleNamespace(free=7 * 1024 ** 3)), \
                    patch.object(mdd_update, "verify_release_file"), \
                    patch.object(mdd_update, "release_engine_fingerprints",
                                 return_value=("c" * 64, "d" * 64)), \
                    patch.object(mdd_update, "load_release_engine",
                                 return_value="engine:v9.9.9"), \
                    patch.object(mdd_update, "load_control_image") as load_control, \
                    patch.object(mdd_update, "load_runtime_image") as load_runtime:
                mdd_update.perform_release_image_install(
                    repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", "container")

            self.assertEqual(downloads, names)
            load_control.assert_called_once()
            self.assertEqual(
                [call.args[2] for call in load_runtime.call_args_list],
                ["hardware", "egress"])
            self.assertFalse(any("arm64" in name for name in downloads))

    def test_fresh_install_without_embedded_manifest_never_downloads_images(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update, "fetch_release_asset") as fetch:
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform_release_image_install(
                    Path(tmp), Path(tmp), "9.9.9", "MddIdd/mdd-sim-gateway", "local")
        fetch.assert_not_called()

    def test_control_image_mismatch_restores_previous_tag(self):
        completed = lambda code=0, out="", err="": type(
            "Completed", (), {"returncode": code, "stdout": out, "stderr": err})()
        calls = [completed(0, "sha256:old\n"), completed(), completed(),
                 completed(0, "amd64|9.9.9\n"), completed()]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(mdd_update.platform, "machine", return_value="aarch64"), \
                patch.object(mdd_update.subprocess, "run", side_effect=calls) as run:
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.load_control_image(Path(tmp, "control.tar.gz"), "9.9.9")
        self.assertEqual(run.call_args_list[-1].args[0], [
            "docker", "tag", "mdd-sim-gateway/control:previous",
            "mdd-sim-gateway/control"])

    def test_apply_tree_preserves_installation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, source = Path(tmp, "repo"), Path(tmp, "source")
            for relative in ["data/auth.json", ".git/config", "control/.venv/bin/python",
                            "control/app/stale.py", "webui/node_modules/pkg/index.js",
                            "webui/dist/index.html", "README.md"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old", encoding="utf-8")
            (repo / ".env").write_text("MDD_PORT=9999", encoding="utf-8")
            for relative in ["control/app/new.py", "webui/src/App.jsx", "README.md", "VERSION"]:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new", encoding="utf-8")

            mdd_update.apply_tree(source, repo)

            self.assertEqual((repo / "data/auth.json").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / ".env").read_text(encoding="utf-8"), "MDD_PORT=9999")
            self.assertEqual((repo / ".git/config").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / "control/.venv/bin/python").read_text(encoding="utf-8"), "old")
            self.assertTrue((repo / "webui/node_modules/pkg/index.js").exists())
            self.assertEqual((repo / "webui/dist/index.html").read_text(encoding="utf-8"), "old",
                             "the served dist must survive so a failed rebuild keeps the UI up")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "new")
            self.assertEqual((repo / "control/app/new.py").read_text(encoding="utf-8"), "new")
            self.assertFalse((repo / "control/app/stale.py").exists(),
                             "files removed upstream must not linger in managed directories")

    def test_apply_tree_replaces_only_marked_release_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, source = Path(tmp, "repo"), Path(tmp, "source")
            (repo / "webui/dist").mkdir(parents=True)
            (repo / "webui/dist/index.html").write_text("old", encoding="utf-8")
            (source / "webui/dist").mkdir(parents=True)
            (source / "webui/dist/index.html").write_text("new", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            mdd_update.apply_tree(source, repo)
            self.assertEqual((repo / "webui/dist/index.html").read_text(), "new")

    def test_perform_accepts_release_without_distribution_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data, payload = base / "repo", base / "data", base / "payload"
            source = payload / "mdd-sim-gateway-v9.9.9"
            (source / "webui/dist").mkdir(parents=True)
            (source / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            (source / "webui/dist/index.html").write_text("new", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            archive = base / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(source, arcname=source.name)
            sums = base / "SHA256SUMS"
            sums.write_text(
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  "
                "mdd-sim-gateway-v9.9.9.tar.gz\n", encoding="utf-8")
            repo.mkdir()
            data.mkdir()
            (repo / "VERSION").write_text("1.3.4\n", encoding="utf-8")
            status = mdd_update.Status(data / "orchestrator/status.json", "9.9.9")

            def fake_download(_url, destination, _env, _proxy="", **_kwargs):
                shutil.copy2(sums if destination.name == "SHA256SUMS" else archive,
                             destination)

            with patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update, "reload_services", return_value=0):
                mdd_update.perform(repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", status)

            self.assertEqual((repo / "VERSION").read_text().strip(), "9.9.9")
            self.assertFalse((repo / "EDITION").exists())

    def test_changed_engine_is_fetched_loaded_and_activated_by_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data, source = base / "repo", base / "data", base / "source"
            repo.mkdir(); data.mkdir()
            (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (source / "webui/dist").mkdir(parents=True)
            (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            (source / "webui/dist/index.html").write_text("ok", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            status = mdd_update.Status(data / "orchestrator/status.json", "9.9.9")
            runtime_fp, base_fp = "a" * 64, "b" * 64
            distributed = "ghcr.io/mddidd/mdd-sim-gateway-engine:v9.9.9"
            downloads = []

            def fake_download(url, destination, _env, _proxy="", **_kwargs):
                downloads.append((url, destination.name, _kwargs.get("phase")))
                destination.write_bytes(b"ok")

            with patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update.platform, "machine", return_value="aarch64"), \
                    patch.object(mdd_update, "verify_release_archive"), \
                    patch.object(mdd_update, "extract", return_value=source), \
                    patch.object(mdd_update, "release_engine_fingerprints",
                                 return_value=(runtime_fp, base_fp)), \
                    patch.object(mdd_update, "engine_image_matches_inputs", return_value=False), \
                    patch.object(mdd_update.shutil, "disk_usage",
                                 return_value=SimpleNamespace(free=3 * 1024 ** 3)), \
                    patch.object(mdd_update, "verify_release_file") as verify_file, \
                    patch.object(mdd_update, "load_release_engine",
                                 return_value=distributed) as load_engine, \
                    patch.object(mdd_update, "backup", return_value=base / "backup.tar.gz"), \
                    patch.object(mdd_update, "apply_tree"), \
                    patch.object(mdd_update, "reload_services", return_value=0) as reload:
                mdd_update.perform(repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", status)

            command, _, env = reload.call_args.args[:3]
            self.assertEqual(command, ["sh", str(repo / "install.sh"), "reload"])
            self.assertEqual(env["MDD_ENGINE_DISTRIBUTION_IMAGE"], distributed)
            engine_name = "mdd-sim-gateway-engine-v9.9.9-arm64.tar.gz"
            self.assertIn((f"https://github.com/MddIdd/mdd-sim-gateway/releases/download/"
                           f"v9.9.9/{engine_name}", engine_name, "engine_image"), downloads)
            self.assertEqual(load_engine.call_args.args[0].name, engine_name)
            self.assertTrue(any(call.args[0].name == engine_name
                                and call.args[2] == "arm64 Engine image"
                                for call in verify_file.call_args_list))

    def test_amd64_downloads_native_engine_and_docker_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data, source = base / "repo", base / "data", base / "source"
            repo.mkdir(); data.mkdir()
            (data / "install-mode").write_text("docker\n", encoding="utf-8")
            (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (source / "webui/dist").mkdir(parents=True)
            (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            (source / "webui/dist/index.html").write_text("ok", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            downloads = []

            def fake_download(url, destination, _env, _proxy="", **_kwargs):
                downloads.append(destination.name)
                destination.write_bytes(b"ok")

            status = mdd_update.Status(data / "orchestrator/status.json", "9.9.9")
            with patch.object(mdd_update.platform, "machine", return_value="x86_64"), \
                    patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update, "verify_release_archive"), \
                    patch.object(mdd_update, "extract", return_value=source), \
                    patch.object(mdd_update, "release_engine_fingerprints",
                                 return_value=("a" * 64, "b" * 64)), \
                    patch.object(mdd_update, "engine_image_matches_inputs", return_value=False), \
                    patch.object(mdd_update.shutil, "disk_usage",
                                 return_value=SimpleNamespace(free=4 * 1024 ** 3)), \
                    patch.object(mdd_update, "verify_release_file") as verify_file, \
                    patch.object(mdd_update, "backup", return_value=base / "backup.tar.gz"), \
                    patch.object(mdd_update, "apply_tree"), \
                    patch.object(mdd_update, "load_release_engine",
                                 return_value="engine:v9.9.9") as load_engine, \
                    patch.object(mdd_update, "load_control_image") as load_control, \
                    patch.object(mdd_update, "reload_services", return_value=0) as reload:
                mdd_update.perform(repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", status)

            command, _, env = reload.call_args.args[:3]
            self.assertEqual(command, ["sh", str(repo / "install.sh"), "reload"])
            self.assertEqual(env["MDD_ENGINE_DISTRIBUTION_IMAGE"], "engine:v9.9.9")
            self.assertEqual(env["MDD_REUSE_CONTROL_IMAGE"], "1")
            self.assertEqual(env["MDD_PRUNE_BUILD_CACHE"], "1")
            self.assertEqual(downloads, [
                "mdd-sim-gateway-v9.9.9.tar.gz", "SHA256SUMS",
                "mdd-sim-gateway-engine-v9.9.9-amd64.tar.gz",
                "mdd-sim-gateway-control-v9.9.9-amd64.tar.gz",
            ])
            self.assertEqual(load_engine.call_args.args[0].name,
                             "mdd-sim-gateway-engine-v9.9.9-amd64.tar.gz")
            self.assertEqual(load_control.call_args.args[0].name,
                             "mdd-sim-gateway-control-v9.9.9-amd64.tar.gz")
            self.assertEqual(
                {call.args[2] for call in verify_file.call_args_list},
                {"amd64 Engine image", "amd64 control image"})

    def test_perform_rejects_malformed_version_and_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = mdd_update.Status(Path(tmp, "status.json"), "x")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "../evil", "MddIdd/mdd-sim-gateway", status)
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "1.0.2", "MddIdd/x/../y", status)

    def test_asset_download_falls_back_and_reuses_the_successful_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data, source = base / "repo", base / "data", base / "source"
            repo.mkdir(); data.mkdir()
            (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (source / "webui/dist").mkdir(parents=True)
            (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            (source / "webui/dist/index.html").write_text("ok", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            attempts = []

            def fake_download(_url, destination, _env, proxy_url="", **_kwargs):
                attempts.append(proxy_url)
                if not proxy_url:
                    raise mdd_update.UpdateError("direct route stalled")
                destination.write_bytes(b"ok")

            status = mdd_update.Status(data / "orchestrator/status.json", "9.9.9")
            routes = [
                {"proxy_url": "", "route": "direct", "route_name": ""},
                {"proxy_url": "socks5h://127.0.0.1:1080", "route": "library",
                 "route_name": "Primary"},
            ]
            with patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update, "verify_release_archive"), \
                    patch.object(mdd_update, "extract", return_value=source), \
                    patch.object(mdd_update, "backup", return_value=base / "backup.tar.gz"), \
                    patch.object(mdd_update, "apply_tree"), \
                    patch.object(mdd_update, "reload_services", return_value=0):
                mdd_update.perform(repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", status,
                                   routes=routes)

            self.assertEqual(attempts[0], "")
            self.assertEqual(attempts[1:], ["socks5h://127.0.0.1:1080"] * 2)


class OrchestratorUpdateTests(unittest.TestCase):
    def test_country_exit_is_resolved_as_a_named_update_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "update-request.json").write_text(json.dumps({
                "version": "9.9.9", "repository": "MddIdd/mdd-sim-gateway",
                "network": {"proxy_mode": "country", "proxy_country": "us"},
            }), encoding="utf-8")
            (root / "desired.json").write_text(json.dumps({"proxy": {
                "profiles": {"primary": {"name": "Primary", "type": "node"}},
                "exits": {"us": {"enabled": True, "profile_id": "primary"}},
            }}), encoding="utf-8")
            (root / "proxy-status.json").write_text(json.dumps({"exits": {"us": {
                "ready": True, "proxy_host": mdd_orchestrator.COUNTRY_PROXY_LISTEN,
                "proxy_port": 22538,
            }}}), encoding="utf-8")
            app = mdd_orchestrator.Orchestrator(data, Path(__file__).resolve().parent.parent)
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(app, "service_active", return_value=False), \
                    patch.object(mdd_orchestrator, "run", return_value=completed):
                app.process_update_request()
            route = json.loads((data / "update/network.json").read_text())
        self.assertEqual(route["proxy_url"],
                         f"socks5h://{mdd_orchestrator.COUNTRY_PROXY_LISTEN}:22538")
        self.assertEqual((route["route"], route["route_name"]), ("country", "US"))

    def test_library_proxy_is_resolved_into_private_file_not_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "update-request.json").write_text(json.dumps({
                "version": "9.9.9", "repository": "MddIdd/mdd-sim-gateway",
                "network": {"proxy_mode": "library", "proxy_profile_id": "primary"},
                "asset_sizes": {"release.tar.gz": 1234, "../invalid": 999},
            }), encoding="utf-8")
            (root / "desired.json").write_text(json.dumps({"proxy": {
                "profiles": {"primary": {"name": "Primary", "type": "node"}},
                "exits": {"us": {"enabled": True, "profile_id": "primary"}},
            }}), encoding="utf-8")
            (root / "proxy-status.json").write_text(json.dumps({"exits": {"us": {
                "ready": True, "proxy_host": mdd_orchestrator.COUNTRY_PROXY_LISTEN,
                "proxy_port": 22538,
            }}}), encoding="utf-8")
            app = mdd_orchestrator.Orchestrator(
                data, Path(__file__).resolve().parent.parent)
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(app, "service_active", return_value=False), \
                    patch.object(mdd_orchestrator, "run", return_value=completed) as run:
                app.process_update_request()
            network_path = data / "update" / "network.json"
            self.assertEqual(json.loads(network_path.read_text())["proxy_url"],
                             f"socks5h://{mdd_orchestrator.COUNTRY_PROXY_LISTEN}:22538")
            self.assertEqual(json.loads(network_path.read_text())["route"], "library")
            self.assertEqual(json.loads(network_path.read_text())["route_name"], "Primary")
            self.assertEqual(json.loads(network_path.read_text())["asset_sizes"],
                             {"release.tar.gz": 1234})
            self.assertEqual(len(json.loads(network_path.read_text())["routes"]), 1)
            command = run.call_args_list[-1].args[0]
            self.assertNotIn("socks5h://", " ".join(command))
            self.assertEqual(network_path.stat().st_mode & 0o777, 0o600)

    def _restart(self, scope: str, *, mode: str = "local"):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            (data / "install-mode").write_text(mode, encoding="utf-8")
            (root / "service-restart-request.json").write_text(
                json.dumps({"scope": scope}), encoding="utf-8")
            app = mdd_orchestrator.Orchestrator(
                data, Path(__file__).resolve().parent.parent)
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(mdd_orchestrator, "run", return_value=completed) as run:
                app.process_service_restart_request()
            commands = [call.args[0] for call in run.call_args_list]
            status = json.loads((root / "service-restart-status.json").read_text())
            return commands, status, (root / "service-restart-request.json").exists()

    def test_restarting_the_control_plane_does_not_disturb_this_process(self):
        commands, status, pending = self._restart("control")
        self.assertEqual(commands, [["systemctl", "restart", "mdd-sim-gateway-control"]])
        self.assertEqual(status["state"], "success")
        self.assertFalse(pending)  # consumed, so a restart loop cannot replay it
        docker_commands, _, _ = self._restart("control", mode="docker")
        self.assertEqual(docker_commands,
                         [["docker", "restart", mdd_orchestrator.CONTROL_CONTAINER]])

    def test_a_restart_that_kills_this_process_is_detached_into_its_own_unit(self):
        commands, status, _ = self._restart("services")
        self.assertEqual(commands[0][:2], ["systemctl", "reset-failed"])
        self.assertEqual(commands[-1][:3],
                         ["systemd-run", "--unit", "mdd-sim-gateway-restart"])
        self.assertTrue(commands[-1][-2].endswith("install.sh"))
        self.assertEqual(commands[-1][-1], "restart")
        # Completion cannot be published: install.sh restarts this very process.
        self.assertEqual(status["state"], "running")

    def test_a_host_reboot_is_handed_to_systemd(self):
        commands, status, _ = self._restart("host")
        self.assertEqual(commands, [["systemctl", "reboot"]])
        self.assertEqual(status["state"], "running")

    def test_a_restart_that_took_this_process_with_it_is_completed_on_the_way_back(self):
        """Observed on the ARM64 host: after `systemctl reboot` the document still said
        "running", because the process that would have finished it was the one being killed."""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            status_path = root / "service-restart-status.json"
            app = mdd_orchestrator.Orchestrator(data, Path(__file__).resolve().parent.parent)
            for scope in ("host", "services"):
                status_path.write_text(json.dumps({"state": "running", "scope": scope}),
                                       encoding="utf-8")
                app.settle_service_restart()
                self.assertEqual(json.loads(status_path.read_text())["state"], "success")
            # A control-plane restart reports its own result, so a "running" one is still live.
            status_path.write_text(json.dumps({"state": "running", "scope": "control"}),
                                   encoding="utf-8")
            app.settle_service_restart()
            self.assertEqual(json.loads(status_path.read_text())["state"], "running")

    def test_an_unknown_restart_scope_runs_nothing(self):
        commands, status, _ = self._restart("reformat")
        self.assertEqual(commands, [])
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error_code"], "restart.error.invalid_scope")

    def _reap(self, status: dict, *, unit_active: bool, request: dict | None = None):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "update-status.json").write_text(json.dumps(status), encoding="utf-8")
            if request is not None:
                (root / "update-request.json").write_text(json.dumps(request), encoding="utf-8")
            app = mdd_orchestrator.Orchestrator(
                data, Path(__file__).resolve().parent.parent)
            with patch.object(app, "service_active", return_value=unit_active):
                app.reap_abandoned_update()
            return json.loads((root / "update-status.json").read_text())

    def test_an_update_that_died_with_its_host_is_retired(self):
        """systemd knows what the document cannot say: no unit left means the run is over."""
        retired = self._reap({"state": "running", "phase": "downloading", "target": "9.9.9",
                              "artifact": "release.tar.gz",
                              "updated_at": int(time.time()) - 3600}, unit_active=False)
        self.assertEqual(retired["state"], "failed")
        self.assertEqual(retired["error_code"], "update.error.abandoned")
        # The stage and asset it died on are what make the failure diagnosable.
        self.assertEqual(retired["phase"], "downloading")
        self.assertEqual(retired["artifact"], "release.tar.gz")

    def test_a_running_updater_and_a_queued_request_are_both_left_alone(self):
        running = {"state": "running", "phase": "reloading",
                   "updated_at": int(time.time()) - 3600}
        self.assertEqual(self._reap(running, unit_active=True)["state"], "running")
        # A request the loop has not consumed yet has no unit to find.
        self.assertEqual(self._reap(running, unit_active=False,
                                    request={"version": "9.9.9"})["state"], "running")
        # A launch this pass raced must not be retired before systemd reports the unit.
        fresh = {"state": "running", "phase": "launching", "updated_at": int(time.time())}
        self.assertEqual(self._reap(fresh, unit_active=False)["state"], "running")


if __name__ == "__main__":
    unittest.main()


class StarCountTests(unittest.TestCase):
    """The count decorates a link and keeps its last successful value across outages."""

    def setUp(self):
        update_check._stars_cache = None
        update_check._stars_checked_at = 0

    def test_a_failed_star_lookup_leaves_the_release_check_intact(self):
        session = SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(
            requests.RequestException("offline")))
        self.assertIsNone(update_check._stargazers(session, {}, "owner/repo"))

    def test_a_star_count_is_read_from_the_repository_endpoint(self):
        calls = []

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"stargazers_count": 13300}

        def get(url, **kwargs):
            calls.append(url)
            return Response()

        value = update_check._stargazers(SimpleNamespace(get=get), {}, "owner/repo")
        self.assertEqual(value, 13300)
        self.assertEqual(calls, ["https://api.github.com/repos/owner/repo"])

        offline = SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(
            requests.RequestException("offline")))
        self.assertEqual(update_check._stargazers(offline, {}, "owner/repo"), 13300)

    def test_a_malformed_star_count_is_absent_rather_than_zero(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"stargazers_count": None}

        self.assertIsNone(update_check._stargazers(
            SimpleNamespace(get=lambda *a, **k: Response()), {}, "owner/repo"))
