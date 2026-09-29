"""RTP forwarder for lines behind a SOCKS exit on the container stack (control/app/rtp_forward.py).

Such a line is only on the internal Engine network, where Docker publishes no port, so the
browser softphone's media never reached it. The forwarder publishes its range and relays.
"""
import asyncio
import json
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import docker

from control.app import engine, rtp_forward


def container(name, labels, status="running"):
    return SimpleNamespace(name=name, status=status, attrs={"Config": {"Labels": labels}})


def engine_container(iid, span=None):
    labels = {rtp_forward.MANAGED_LABEL: "true", rtp_forward.COMPONENT_LABEL: "engine"}
    if span:
        labels[rtp_forward.FORWARD_LABEL] = span
    return container(f"mdd-sim-gateway-engine-{iid}", labels)


def forwarder(mapping):
    return container(rtp_forward.CONTAINER, {
        rtp_forward.MANAGED_LABEL: "true",
        rtp_forward.MAP_LABEL: json.dumps(mapping)})


class Client:
    """Just enough of docker.DockerClient for reconcile()."""

    def __init__(self, engines, current=None):
        self.engines, self.current = engines, current
        self.containers = Mock()
        self.containers.list.side_effect = lambda **kwargs: list(self.engines)
        self.containers.get.side_effect = self._get
        self.created = MagicMock(name="forwarder")
        self.containers.create.return_value = self.created
        self.networks = Mock()

    def _get(self, name):
        if name == rtp_forward.CONTAINER:
            if self.current is None:
                raise docker.errors.NotFound(name)
            return self.current
        return SimpleNamespace(image=SimpleNamespace(id="sha256:control"))


class PlanTests(unittest.TestCase):
    CONFIGURED = {"mdd-sim-gateway-engine-1", "mdd-sim-gateway-engine-2"}

    def test_engines_with_the_label_are_forwarded(self):
        client = Client([engine_container(1, "30000-30011"), engine_container(2)])
        self.assertEqual(rtp_forward.plan(client, self.CONFIGURED), [
            {"target": "mdd-sim-gateway-engine-1", "first": 30000, "last": 30011}])

    def test_a_stopped_line_keeps_its_range_so_the_others_keep_their_calls(self):
        kept = {"target": "mdd-sim-gateway-engine-2", "first": 30012, "last": 30023}
        client = Client([engine_container(1, "30000-30011")], current=forwarder([kept]))
        self.assertIn(kept, rtp_forward.plan(client, self.CONFIGURED))

    def test_a_deleted_line_or_one_no_longer_behind_an_exit_lets_go(self):
        gone = {"target": "mdd-sim-gateway-engine-9", "first": 30100, "last": 30111}
        direct_now = {"target": "mdd-sim-gateway-engine-2", "first": 30012, "last": 30023}
        client = Client([engine_container(1, "30000-30011"), engine_container(2)],
                        current=forwarder([gone, direct_now]))
        self.assertEqual([entry["target"] for entry in rtp_forward.plan(client, self.CONFIGURED)],
                         ["mdd-sim-gateway-engine-1"])

    def test_exclude_frees_a_range_before_its_engine_publishes_it(self):
        client = Client([engine_container(1, "30000-30011")])
        self.assertEqual(rtp_forward.plan(client, self.CONFIGURED,
                                          exclude="mdd-sim-gateway-engine-1"), [])

    def test_bad_or_overlapping_ranges_are_not_forwarded(self):
        client = Client([engine_container(1, "30000-30011"), engine_container(2, "30005-30020"),
                         engine_container(3, "80-90"), engine_container(4, "x")])
        self.assertEqual([entry["target"] for entry in rtp_forward.plan(client, set())],
                         ["mdd-sim-gateway-engine-1"])


class ReconcileTests(unittest.TestCase):
    def test_the_forwarder_publishes_the_ranges_with_nothing_else(self):
        client = Client([engine_container(1, "30000-30001")])
        rtp_forward.reconcile(client, "mdd-sim-gateway-engine", set())
        kwargs = client.containers.create.call_args.kwargs
        self.assertEqual(client.containers.create.call_args.args, ("sha256:control",))
        self.assertEqual(kwargs["ports"], {"30000/udp": 30000, "30001/udp": 30001})
        self.assertEqual(kwargs["network"], "bridge")
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertEqual(kwargs["user"], "65534:65534")
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["healthcheck"], {"test": ["NONE"]})
        self.assertEqual(json.loads(kwargs["environment"][rtp_forward.ENV_MAP]), [
            {"target": "mdd-sim-gateway-engine-1", "first": 30000, "last": 30001}])
        client.networks.get.assert_called_once_with("mdd-sim-gateway-engine")
        client.networks.get.return_value.connect.assert_called_once_with(client.created)
        client.created.start.assert_called_once_with()

    def test_an_unchanged_forwarder_is_left_running(self):
        mapping = [{"target": "mdd-sim-gateway-engine-1", "first": 30000, "last": 30001}]
        current = container(rtp_forward.CONTAINER, {
            rtp_forward.MANAGED_LABEL: "true",
            rtp_forward.MAP_LABEL: json.dumps(mapping),
            rtp_forward.CONFIG_LABEL: rtp_forward.fingerprint(
                mapping, "sha256:control", "mdd-sim-gateway-engine")})
        current.remove = Mock()
        client = Client([engine_container(1, "30000-30001")], current=current)
        rtp_forward.reconcile(client, "mdd-sim-gateway-engine", set())
        current.remove.assert_not_called()
        client.containers.create.assert_not_called()

    def test_no_line_behind_an_exit_leaves_no_forwarder(self):
        current = forwarder([])
        current.remove = Mock()
        client = Client([engine_container(1)], current=current)
        rtp_forward.reconcile(client, "mdd-sim-gateway-engine", set())
        current.remove.assert_called_once_with(force=True, v=True)
        client.containers.create.assert_not_called()

    def test_a_foreign_container_by_that_name_is_never_touched(self):
        current = container(rtp_forward.CONTAINER, {})
        current.remove = Mock()
        client = Client([engine_container(1, "30000-30001")], current=current)
        with self.assertRaises(RuntimeError):
            rtp_forward.reconcile(client, "mdd-sim-gateway-engine", set())
        current.remove.assert_not_called()


class EngineStartTests(unittest.TestCase):
    """Which lines the forwarder takes, as decided when an Engine is started."""

    def start(self, exit_info, internal=True, media_attachment=None):
        client = Mock()
        client.images.get.return_value = SimpleNamespace(
            id="sha256:engine", attrs={"Config": {"Labels": {engine.ENGINE_LABEL: "socks5"}}})
        client.containers.get.side_effect = docker.errors.NotFound("engine")
        client.containers.run.return_value = SimpleNamespace(id="new", name="engine")
        client.networks.get.return_value.attrs = {"Internal": internal}
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(engine._internal_networks, clear=True), \
                patch.object(engine, "_client", return_value=client), \
                patch.object(engine, "ENGINE_NETWORK", "mdd-sim-gateway-engine"), \
                patch.object(engine, "DIRECT_NETWORK", "mdd-sim-gateway-uplink"), \
                patch.object(engine, "_instance_paths", return_value=(temp, temp)), \
                patch.object(engine, "_clear_runtime_state"), \
                patch.object(engine.media, "engine_attachment", return_value=media_attachment), \
                patch.object(engine.egress, "resolve_ipv4_via_socks", return_value="198.51.100.1"), \
                patch.object(engine.egress, "ensure_line", return_value=exit_info), \
                patch.object(engine.cfg, "write_instance_json"), \
                patch.object(engine, "reconcile_rtp_forward") as reconcile:
            engine.start({"id": "1", "ports": {"rtp_start": 30000, "rtp_end": 30011}}, {})
        return client.containers.run.call_args.kwargs, reconcile

    SOCKS = {"transport": "socks5", "proxy_url": "socks5://mdd-egress:22157"}

    def test_a_line_behind_an_exit_is_forwarded_instead_of_publishing(self):
        options, reconcile = self.start(self.SOCKS)
        self.assertEqual(options["ports"], {})
        span = options["labels"][rtp_forward.FORWARD_LABEL]
        self.assertEqual(span.split("-")[0], "30000")
        reconcile.assert_called_once_with(unittest.mock.ANY)

    def test_a_direct_line_publishes_its_own_ports_after_the_forwarder_lets_go(self):
        options, reconcile = self.start({})
        self.assertIn("30000/udp", options["ports"])
        self.assertNotIn(rtp_forward.FORWARD_LABEL, options["labels"])
        reconcile.assert_called_once_with(unittest.mock.ANY,
                                          exclude="mdd-sim-gateway-engine-1")

    def test_an_engine_network_that_is_not_internal_publishes_as_before(self):
        options, _reconcile = self.start(self.SOCKS, internal=False)
        self.assertIn("30000/udp", options["ports"])
        self.assertNotIn(rtp_forward.FORWARD_LABEL, options["labels"])


class ForwarderTests(unittest.TestCase):
    """The relay itself, over real UDP sockets on the loopback."""

    @staticmethod
    def free_udp_port():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_each_browser_address_gets_its_own_flow_and_only_its_own_answers(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            seen_sources = []

            class Echo(asyncio.DatagramProtocol):
                def connection_made(self, transport):
                    self.transport = transport

                def datagram_received(self, data, addr):
                    seen_sources.append(addr)
                    self.transport.sendto(b"engine:" + data, addr)

            engine_transport, _ = await loop.create_datagram_endpoint(
                Echo, local_addr=("127.0.0.1", 0))
            engine_port = engine_transport.get_extra_info("sockname")[1]
            port = self.free_udp_port()
            real = socket.getaddrinfo

            def resolve(host, *args, **kwargs):
                if host == "mdd-sim-gateway-engine-1":
                    return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "",
                             ("127.0.0.1", engine_port))]
                return real(host, *args, **kwargs)

            with patch("socket.getaddrinfo", side_effect=resolve):
                task = loop.create_task(rtp_forward.serve(
                    [{"target": "mdd-sim-gateway-engine-1", "first": port, "last": port}],
                    host="127.0.0.1"))
                await asyncio.sleep(0.1)
                browsers = []
                for name in (b"a", b"b"):
                    received = asyncio.Queue()

                    class Browser(asyncio.DatagramProtocol):
                        def datagram_received(self, data, addr, received=received):
                            received.put_nowait((data, addr))

                    transport, _ = await loop.create_datagram_endpoint(
                        Browser, remote_addr=("127.0.0.1", port))
                    browsers.append((name, transport, received))
                for name, transport, _ in browsers:
                    transport.sendto(name)
                answers = {}
                for name, _, received in browsers:
                    data, addr = await asyncio.wait_for(received.get(), 2)
                    answers[name] = (data, addr[1])
                task.cancel()
                for _, transport, _ in browsers:
                    transport.close()
                engine_transport.close()
            return answers, seen_sources, port

        answers, seen_sources, port = asyncio.run(scenario())
        self.assertEqual(answers, {b"a": (b"engine:a", port), b"b": (b"engine:b", port)})
        # The Engine sees one source per browser, so it answers each on its own.
        self.assertEqual(len(set(seen_sources)), 2)

    def test_a_port_opens_no_more_than_its_share_of_flows(self):
        outside = rtp_forward._Outside(30000, "mdd-sim-gateway-engine-1")
        outside.transport = Mock()
        with patch.object(rtp_forward._Flow, "open", new=Mock(return_value=None)), \
                patch("asyncio.get_running_loop") as loop:
            for index in range(rtp_forward.FLOWS_PER_PORT + 5):
                outside.datagram_received(b"x", ("192.0.2.1", 40000 + index))
        self.assertEqual(len(outside.flows), rtp_forward.FLOWS_PER_PORT)
        self.assertEqual(loop.return_value.create_task.call_count, rtp_forward.FLOWS_PER_PORT)


if __name__ == "__main__":
    unittest.main()
