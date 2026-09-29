import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control.app import config, main, operations
from host import mdd_container_update


class ContainerComposeRewriteTests(unittest.TestCase):
    def test_only_release_image_references_change(self):
        source = """name: mdd-sim-gateway
services:
  control:
    image: ghcr.io/mddidd/mdd-sim-gateway-control:v1.0.0
    environment:
      MDD_ENGINE_IMAGE: ghcr.io/mddidd/mdd-sim-gateway-engine:v1.0.0
      KEEP_ME: ghcr.io/example/unrelated:v1
  hardware:
    image: 'ghcr.io/mddidd/mdd-sim-gateway-hardware:v1.0.0' # keep this comment
  egress:
    image: ghcr.io/mddidd/mdd-sim-gateway-egress@sha256:""" + "a" * 64 + "\n"
        images = mdd_container_update.canonical_images("MddIdd/mdd-sim-gateway", "2.0.0")

        updated = mdd_container_update.rewrite_compose(source, images)

        for component, image in images.items():
            self.assertIn(image, updated)
        self.assertIn("KEEP_ME: ghcr.io/example/unrelated:v1", updated)
        self.assertIn("' # keep this comment", updated)
        self.assertNotIn("mdd-sim-gateway-control:v1.0.0", updated)

    def test_incomplete_compose_is_rejected_before_mutation(self):
        with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
            mdd_container_update.rewrite_compose(
                "services:\n  control:\n    image: ghcr.io/x/mdd-sim-gateway-control:v1\n",
                mdd_container_update.canonical_images("x/y", "2.0.0"))

    def test_exactly_one_project_compose_file_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text("services: {}\n")
            self.assertEqual(mdd_container_update.find_compose(root).name, "docker-compose.yml")
            (root / "compose.yaml").write_text("services: {}\n")
            with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
                mdd_container_update.find_compose(root)


class ContainerDownloadRouteTests(unittest.TestCase):
    """The container helper gets URLs, never bare selections it would treat as direct."""

    settings = {"proxy": {"exits": {"us": {"enabled": True, "profile_id": "sub"}},
                          "profiles": {"sub": {"type": "subscription", "name": "Sub"},
                                       "hk": {"type": "socks5", "name": "HK",
                                              "server": "hk.example", "port": 1080}}}}
    ready = {"exits": {"us": {"ready": True, "transport": "socks5",
                              "proxy_host": "mdd-egress", "proxy_port": 22538}}}

    def resolve(self, selections, state=None, current=True):
        from control.app import egress, egress_contract
        with patch.object(operations.cfg, "get_settings", return_value=self.settings), \
                patch.object(egress, "status", return_value=state or self.ready), \
                patch.object(egress_contract, "current_status", return_value=current):
            return operations._container_download_routes(selections)

    def test_a_country_exit_becomes_its_internal_socks_listener(self):
        self.assertEqual(self.resolve([{"proxy_mode": "country", "proxy_country": "us"}]), [
            {"proxy_url": "socks5h://mdd-egress:22538", "route": "country",
             "route_name": "US"}])

    def test_a_subscription_profile_goes_through_the_exit_that_uses_it(self):
        routes = self.resolve([{"proxy_mode": "library", "proxy_profile_id": "sub"}])
        self.assertEqual(routes[0]["proxy_url"], "socks5h://mdd-egress:22538")
        self.assertEqual(routes[0]["route"], "library")

    def test_a_socks_profile_is_dialled_directly(self):
        routes = self.resolve([{"proxy_mode": "library", "proxy_profile_id": "hk"}])
        self.assertEqual(routes[0]["proxy_url"], "socks5h://hk.example:1080")

    def test_an_exit_that_is_not_ready_is_refused_rather_than_made_direct(self):
        with self.assertRaisesRegex(ValueError, "US is not ready"):
            self.resolve([{"proxy_mode": "country", "proxy_country": "us"}],
                         state={"exits": {"us": {"ready": False}}})

    def test_stale_egress_status_counts_as_not_ready(self):
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.resolve([{"proxy_mode": "country", "proxy_country": "us"}], current=False)

    def test_auto_keeps_only_the_candidates_that_resolve(self):
        routes = self.resolve([{"proxy_mode": "direct"},
                               {"proxy_mode": "library", "proxy_profile_id": "sub"}],
                              state={"exits": {}})
        self.assertEqual([route["route"] for route in routes], ["direct"])


class ContainerComposeOrderTests(unittest.TestCase):
    def test_compose_runs_from_the_host_path_so_the_nas_can_manage_the_containers(self):
        """Run from /data, Compose labelled the containers `/data/compose.yaml`, a path that
        does not exist on the NAS, and Container Manager sent every stop or delete for
        container "undefined"."""
        commands = []
        with tempfile.TemporaryDirectory() as host:
            (Path(host) / "compose.yaml").write_text("services: {}\n")
            with patch.dict(os.environ, {"MDD_HOST_DATA": host}), \
                    patch.object(mdd_container_update, "run",
                                 side_effect=lambda command, **kwargs: commands.append(
                                     (command, kwargs["cwd"]))):
                mdd_container_update.compose_up(Path("/data/compose.yaml"), lambda _c: None)
        for command, cwd in commands:
            self.assertEqual(command[command.index("-f") + 1], f"{host}/compose.yaml")
            self.assertEqual(command[command.index("--project-directory") + 1], host)
            self.assertEqual(str(cwd), host)

    def test_an_old_launcher_without_the_host_mount_still_works(self):
        commands = []
        with patch.dict(os.environ, {"MDD_HOST_DATA": "/does/not/exist"}), \
                patch.object(mdd_container_update, "run",
                             side_effect=lambda command, **kwargs: commands.append(command)):
            mdd_container_update.compose_up(Path("/data/compose.yaml"), lambda _c: None)
        self.assertEqual(commands[0][commands[0].index("-f") + 1], "/data/compose.yaml")

    def test_control_starts_only_after_our_own_wait_for_hardware(self):
        """Compose's service_healthy gate gave up on the first unhealthy report, which a
        Hardware start recovering a stale QMI session always produces."""
        events = []
        with patch.object(mdd_container_update, "run",
                          side_effect=lambda command, **_: events.append(command[-2:])):
            mdd_container_update.compose_up(Path("/data/docker-compose.yml"),
                                            lambda component: events.append(component))
        self.assertEqual(events, [["hardware", "egress"], "hardware", "egress",
                                  ["--no-deps", "control"]])

    def test_every_compose_start_skips_compose_dependency_gating(self):
        commands = []
        with patch.object(mdd_container_update, "run",
                          side_effect=lambda command, **_: commands.append(command)):
            mdd_container_update.compose_up(Path("/data/docker-compose.yml"), lambda _c: None)
        self.assertTrue(all("--no-deps" in command for command in commands))


class SettleServicesTests(unittest.TestCase):
    """A Hardware container on the DS1621+ took 22 s to exit after SIGKILL. Docker's stop gave
    up first, Compose aborted mid-recreate, and the rollback hit a name conflict."""

    @staticmethod
    def container(name, running_states):
        item = Mock()
        item.name = name
        states = iter(running_states)
        item.attrs = {"State": {"Running": True}}

        def reload():
            item.attrs = {"State": {"Running": next(states)}}

        item.reload.side_effect = reload
        return item

    def test_a_slow_container_is_waited_for_instead_of_failing_the_update(self):
        slow = self.container("mdd-sim-gateway-hardware", [True, True, False])
        slow.stop.side_effect = mdd_container_update.docker.errors.APIError(
            "tried to kill container, but did not receive an exit event")
        client = Mock()
        client.containers.list.return_value = [slow]
        with patch.object(mdd_container_update.time, "sleep"):
            mdd_container_update.settle_services(client, ("hardware",))
        self.assertEqual(slow.reload.call_count, 3)
        client.containers.list.assert_called_once_with(all=True, filters={"label": [
            "com.docker.compose.project=mdd-sim-gateway",
            "com.docker.compose.service=hardware"]})

    def test_temporaries_left_by_a_failed_recreate_are_removed(self):
        leftover = self.container("69e1b009d23a_mdd-sim-gateway-hardware", [])
        current = self.container("mdd-sim-gateway-hardware", [False])
        client = Mock()
        client.containers.list.return_value = [leftover, current]
        mdd_container_update.settle_services(client, ("hardware",))
        leftover.remove.assert_called_once_with(force=True)
        leftover.stop.assert_not_called()
        current.remove.assert_not_called()

    def test_a_container_that_never_exits_fails_with_its_name(self):
        stuck = self.container("mdd-sim-gateway-hardware", [True] * 1000)
        client = Mock()
        client.containers.list.return_value = [stuck]
        clock = iter(range(0, 10000, 10))
        with patch.object(mdd_container_update.time, "sleep"), \
                patch.object(mdd_container_update.time, "monotonic",
                             side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(mdd_container_update.mdd_update.UpdateError,
                                       "mdd-sim-gateway-hardware did not exit"):
            mdd_container_update.settle_services(client, ("hardware",))

    def test_compose_up_settles_each_group_before_starting_it(self):
        events = []
        with patch.object(mdd_container_update, "settle_services",
                          side_effect=lambda _client, services: events.append(services)), \
                patch.object(mdd_container_update, "run",
                             side_effect=lambda command, **_: events.append(command[-1])):
            mdd_container_update.compose_up(Path("/data/docker-compose.yml"),
                                            lambda _c: None, Mock())
        self.assertEqual(events, [("hardware", "egress"), "egress", ("control",), "control"])


class ComposeEnvironmentTests(unittest.TestCase):
    def test_the_control_image_environment_never_reaches_compose_interpolation(self):
        """The Control image sets MDD_HTTP_PORT=8443 for its own listener; the Compose file
        uses the same name for the host port. Inherited, it moved the published port."""
        envs = []
        leaked = {"PATH": "/usr/bin", "MDD_HTTP_PORT": "8443", "MDD_RTP_BASE": "10000",
                  "MDD_DATA": "/data"}
        with patch.dict(os.environ, leaked, clear=True), \
                patch.object(mdd_container_update, "run",
                             side_effect=lambda command, **kwargs: envs.append(kwargs["env"])):
            mdd_container_update.compose_up(Path("/data/docker-compose.yml"), lambda _c: None)
        self.assertEqual(envs, [{"PATH": "/usr/bin"}, {"PATH": "/usr/bin"}])


class DockerRootSpaceTests(unittest.TestCase):
    def test_the_probe_overrides_the_control_entrypoint(self):
        """rc1 and rc2 passed the probe as a command only. The Control image's ENTRYPOINT
        is `python run.py`, so the probe started the whole control plane in a container
        without /data and every container update failed at its first step."""
        client = Mock()
        client.info.return_value = {"DockerRootDir": "/volume1/@docker"}
        client.containers.get.return_value.image.id = "sha256:control"
        client.containers.run.return_value = b"12345\n"

        self.assertEqual(mdd_container_update.docker_root_free_bytes(client), 12345)

        kwargs = client.containers.run.call_args.kwargs
        self.assertEqual(kwargs["entrypoint"], ["python", "-c"])
        # DSM defaults to its `db` log driver, from which docker-py returns no output.
        self.assertEqual(kwargs["log_config"]["type"], "json-file")
        command = client.containers.run.call_args.args[1]
        self.assertEqual(len(command), 1)
        self.assertIn("statvfs('/docker-root')", command[0])
        self.assertEqual(kwargs["volumes"], {"/volume1/@docker": {
            "bind": "/docker-root", "mode": "ro"}})


class ContainerUpdateLaunchTests(unittest.TestCase):
    def test_control_launches_a_detached_owned_helper_on_both_project_networks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = root / "orchestrator"
            orchestrator.mkdir()
            (orchestrator / "update-request.json").write_text(json.dumps({
                "version": "2.0.0", "repository": "MddIdd/mdd-sim-gateway",
                "network": {"route": "direct", "proxy_url": ""},
                "networks": [{"route": "direct", "proxy_url": ""}],
                "asset_sizes": {"SHA256SUMS": 123},
            }))
            control = Mock()
            control.image.id = "sha256:control"
            control.attrs = {"Config": {"Labels": {
                "io.mdd-sim-gateway.managed": "true",
                "io.mdd-sim-gateway.component": "control"}}}
            helper = Mock()
            client = Mock()
            client.containers.get.return_value = control
            client.containers.create.return_value = helper
            network = Mock()
            client.networks.get.return_value = network
            env = {"MDD_CONTAINER_STACK": "1", "MDD_HOST_DATA": "/volume1/docker/mdd",
                   "MDD_ENGINE_NETWORK": "mdd-engine", "MDD_ENGINE_DIRECT_NETWORK": "mdd-uplink"}
            with patch.object(config, "DATA_DIR", str(root)), patch.dict(os.environ, env), \
                    patch.object(operations.docker, "from_env", return_value=client):
                result = operations.launch_container_update()

            self.assertTrue(result["ok"])
            create = client.containers.create.call_args
            self.assertEqual(create.args[0], "sha256:control")
            self.assertEqual(create.kwargs["network"], "mdd-uplink")
            volumes = create.kwargs["volumes"]
            self.assertIn("/volume1/docker/mdd:/data:rw", volumes)
            # Also at its host path, so Compose labels match Container Manager's.
            self.assertIn("/volume1/docker/mdd:/volume1/docker/mdd:rw", volumes)
            self.assertEqual(create.kwargs["environment"]["MDD_HOST_DATA"], "/volume1/docker/mdd")
            self.assertIn("/app/host/mdd_container_update.py", create.kwargs["command"])
            client.networks.get.assert_called_once_with("mdd-engine")
            network.connect.assert_called_once_with(helper)
            helper.start.assert_called_once_with()
            self.assertFalse((orchestrator / "update-request.json").exists())
            saved = json.loads((root / "update/network.json").read_text())
            self.assertEqual(saved["asset_sizes"], {"SHA256SUMS": 123})

    def test_invalid_host_data_fails_without_starting_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "orchestrator").mkdir()
            (root / "orchestrator/update-request.json").write_text(json.dumps({
                "version": "2.0.0", "repository": "MddIdd/mdd-sim-gateway"}))
            with patch.object(config, "DATA_DIR", str(root)), patch.dict(
                    os.environ, {"MDD_HOST_DATA": "relative/path"}, clear=False), \
                    patch.object(operations.docker, "from_env") as docker_client:
                result = operations.launch_container_update()
            self.assertFalse(result["ok"])
            docker_client.assert_not_called()


class ContainerUpdateRollbackTests(unittest.TestCase):
    def test_failed_base_recreation_restores_the_original_compose(self):
        attempts = []

        def compose_up_stub(_compose, wait, _client=None):
            # The first start (new release) fails; the rollback start waits like the real one.
            attempts.append(1)
            if len(attempts) == 1:
                raise mdd_container_update.mdd_update.UpdateError("new Control unhealthy")
            wait("hardware")
            wait("egress")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "update").mkdir()
            original = """services:
  control:
    image: ghcr.io/mddidd/mdd-sim-gateway-control:v1.0.0
    environment:
      MDD_ENGINE_IMAGE: ghcr.io/mddidd/mdd-sim-gateway-engine:v1.0.0
  hardware:
    image: ghcr.io/mddidd/mdd-sim-gateway-hardware:v1.0.0
  egress:
    image: ghcr.io/mddidd/mdd-sim-gateway-egress:v1.0.0
"""
            (root / "docker-compose.yml").write_text(original)
            network = root / "update/network.json"
            network.write_text(json.dumps({"route": "direct", "routes": [
                {"route": "direct", "proxy_url": ""}]}))
            base = {name: SimpleNamespace(image=SimpleNamespace(id=f"sha256:old-{name}"))
                    for name in mdd_container_update.BASE_COMPONENTS}
            client = Mock()
            client.containers.get.side_effect = lambda name: base[name.removeprefix(
                "mdd-sim-gateway-")]
            client.containers.list.return_value = []
            status = mdd_container_update.mdd_update.Status(
                root / "orchestrator/update-status.json", "2.0.0")

            def fetch(_url, destination, *_args, **_kwargs):
                destination.write_bytes(b"verified")
                return 0

            with patch.object(mdd_container_update.docker, "from_env", return_value=client), \
                    patch.object(mdd_container_update.mdd_update, "fetch_release_asset",
                                 side_effect=fetch), \
                    patch.object(mdd_container_update.mdd_update, "verify_release_file"), \
                    patch.object(mdd_container_update, "docker_root_free_bytes",
                                 return_value=10 * 1024 ** 3), \
                    patch.object(mdd_container_update, "run"), \
                    patch.object(mdd_container_update, "verify_and_tag_image",
                                 side_effect=lambda _c, component, *_a: f"sha256:new-{component}"), \
                    patch("control.app.operations.create_local_backup",
                          return_value={"name": "backup.tar.gz"}), \
                    patch.object(mdd_container_update, "compose_up",
                                 side_effect=compose_up_stub) as compose_up, \
                    patch.object(mdd_container_update, "wait_container") as wait:
                with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
                    mdd_container_update.perform(
                        root, "2.0.0", "MddIdd/mdd-sim-gateway", network, status)

            self.assertEqual((root / "docker-compose.yml").read_text(), original)
            self.assertEqual(compose_up.call_count, 2)
            self.assertEqual(wait.call_count, len(mdd_container_update.BASE_COMPONENTS))
            self.assertEqual([call.args[1] for call in wait.call_args_list],
                             [f"mdd-sim-gateway-{c}" for c in ("hardware", "egress", "control")])
            failed = json.loads((root / "orchestrator/update-status.json").read_text())
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["rollback_succeeded"])

    def test_the_rollback_drill_fails_once_after_the_switch_and_restores_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "update").mkdir()
            (root / "update/fail-after-switch").touch()
            original = """services:
  control:
    image: ghcr.io/mddidd/mdd-sim-gateway-control:v1.0.0
    environment:
      MDD_ENGINE_IMAGE: ghcr.io/mddidd/mdd-sim-gateway-engine:v1.0.0
  hardware:
    image: ghcr.io/mddidd/mdd-sim-gateway-hardware:v1.0.0
  egress:
    image: ghcr.io/mddidd/mdd-sim-gateway-egress:v1.0.0
"""
            (root / "docker-compose.yml").write_text(original)
            network = root / "update/network.json"
            network.write_text(json.dumps({"routes": [{"route": "direct", "proxy_url": ""}]}))
            base = {name: SimpleNamespace(image=SimpleNamespace(id=f"sha256:old-{name}"))
                    for name in mdd_container_update.BASE_COMPONENTS}
            client = Mock()
            client.containers.get.side_effect = lambda name: base[name.removeprefix(
                "mdd-sim-gateway-")]
            client.containers.list.return_value = []
            status = mdd_container_update.mdd_update.Status(
                root / "orchestrator/update-status.json", "2.0.0")
            composes = []

            def fetch(_url, destination, *_args, **_kwargs):
                destination.write_bytes(b"verified")
                return 0

            with patch.object(mdd_container_update.docker, "from_env", return_value=client), \
                    patch.object(mdd_container_update.mdd_update, "fetch_release_asset",
                                 side_effect=fetch), \
                    patch.object(mdd_container_update.mdd_update, "verify_release_file"), \
                    patch.object(mdd_container_update, "docker_root_free_bytes",
                                 return_value=10 * 1024 ** 3), \
                    patch.object(mdd_container_update, "run"), \
                    patch.object(mdd_container_update, "verify_and_tag_image",
                                 side_effect=lambda _c, component, *_a: f"sha256:new-{component}"), \
                    patch("control.app.operations.create_local_backup",
                          return_value={"name": "backup.tar.gz"}), \
                    patch.object(mdd_container_update, "compose_up",
                                 side_effect=lambda compose, _wait, _client=None: composes.append(
                                     compose.read_text())), \
                    patch.object(mdd_container_update, "roll_engines") as rolled, \
                    patch.object(mdd_container_update, "wait_container"):
                with self.assertRaisesRegex(mdd_container_update.mdd_update.UpdateError,
                                            "rollback drill"):
                    mdd_container_update.perform(
                        root, "2.0.0", "MddIdd/mdd-sim-gateway", network, status)

            rolled.assert_called_once()
            self.assertIn("control:v2.0.0", composes[0])     # the new release really ran
            self.assertEqual(composes[1], original)           # then the old one came back
            self.assertEqual((root / "docker-compose.yml").read_text(), original)
            self.assertFalse((root / "update/fail-after-switch").exists())
            self.assertFalse((root / "update/installed-images.json").exists())
            failed = json.loads((root / "orchestrator/update-status.json").read_text())
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["rollback_succeeded"])

    def test_rollout_recreates_only_running_engines_explicitly(self):
        running = Mock(name="running")
        running.name = "mdd-sim-gateway-engine-line-1"
        stopped = Mock(name="stopped")
        stopped.name = "mdd-sim-gateway-engine-line-2"
        client = Mock()
        client.containers.list.return_value = [running]
        status = Mock()

        with patch.object(mdd_container_update, "recreate_engine") as recreate, \
                patch.object(mdd_container_update, "wait_container") as wait:
            mdd_container_update.roll_engines(client, "sha256:new", status)

        client.containers.list.assert_called_once_with(filters={"label": [
            "io.mdd-sim-gateway.managed=true",
            "io.mdd-sim-gateway.component=engine"]})
        running.remove.assert_called_once_with(force=True)
        stopped.remove.assert_not_called()
        recreate.assert_called_once_with(client, running.name)
        wait.assert_called_once_with(client, running.name, "sha256:new", timeout=240)

    def test_recreate_engine_runs_control_lifecycle_code(self):
        control = Mock()
        control.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
        client = Mock()
        client.containers.get.return_value = control

        mdd_container_update.recreate_engine(
            client, "mdd-sim-gateway-engine-line_1")

        command = control.exec_run.call_args.args[0]
        self.assertEqual(command[:2], ["python", "-c"])
        self.assertEqual(command[-1], "line_1")
        self.assertIn("engine.start", command[2])

    def test_recreate_engine_accepts_docker_valid_dotted_instance_id(self):
        control = Mock()
        control.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
        client = Mock()
        client.containers.get.return_value = control

        mdd_container_update.recreate_engine(
            client, "mdd-sim-gateway-engine-line.1")

        self.assertEqual(control.exec_run.call_args.args[0][-1], "line.1")


class ContainerUpdateApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_apply_launches_the_detached_executor(self):
        with patch.object(main.update_check, "request_apply",
                          return_value={"ok": True, "version": "2.0.0"}), \
                patch.object(main.operations, "container_stack_enabled", return_value=True), \
                patch.object(main.operations, "launch_container_update",
                             return_value={"ok": True, "executor": "container"}) as launch:
            result = await main.api_system_update_apply({"version": "2.0.0"})
        self.assertTrue(result["ok"])
        launch.assert_called_once_with()

    async def test_container_launch_failure_is_returned_to_the_dialog(self):
        with patch.object(main.update_check, "request_apply", return_value={"ok": True}), \
                patch.object(main.operations, "container_stack_enabled", return_value=True), \
                patch.object(main.operations, "launch_container_update",
                             return_value={"ok": False, "phase": "launch",
                                           "error": "docker unavailable"}):
            result = await main.api_system_update_apply({"version": "2.0.0"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "docker unavailable")


if __name__ == "__main__":
    unittest.main()
