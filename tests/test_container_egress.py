import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from engine.outer_transport import ProxyEndpoint, proxy_udp_socket
from runtime.egress import ListenAddressError, SocksEgress
from control.app.egress_contract import current_status


class ContainerEgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = SocksEgress(Path(self.temp.name), Path.cwd(), dry_run=True)
        self.proxy = {"enabled": True, "exits": {
            "gb": {"enabled": True, "mode": "manual", "proxy_url": "socks5://relay.invalid:1080"},
            "us": {"enabled": True, "mode": "manual", "proxy_url": "socks5://other.invalid:1080"}}}

    def test_generates_only_socks_without_tun_or_route_mutations(self):
        with patch.object(self.app, "listen_address", return_value="172.30.0.3"):
            config, states = self.app.build_proxy_config(self.proxy)
        self.assertEqual([item["type"] for item in config["inbounds"]], ["socks", "socks"])
        self.assertEqual(len({item["listen_port"] for item in config["inbounds"]}), 2)
        self.assertTrue(all(item["listen"] == "172.30.0.3" for item in config["inbounds"]))
        self.assertNotIn("interface", states["gb"])
        self.assertEqual(states["gb"]["proxy_host"], "mdd-egress")
        self.assertNotIn("auto_detect_interface", config["route"])
        for rule in config["route"]["rules"]:
            self.assertFalse(any(tag.startswith("tun-") for tag in rule.get("inbound", [])))
        with self.assertRaises(RuntimeError):
            self.app.apply_routes({("198.51.100.1", "mdd-gb")})

    def test_publishes_separate_contract_without_claiming_host_routes(self):
        with patch.object(self.app, "listen_address", return_value="172.30.0.3"), \
             patch.object(self.app, "process_reselect_requests"), \
             patch.object(self.app, "process_stalled_reports"), \
             patch.object(self.app, "update_selected_nodes"):
            self.app.reconcile_socks({"proxy": self.proxy})
        status = json.loads(self.app.status_path.read_text())
        self.assertEqual(status["transport"], "socks5")
        self.assertTrue(status["exits"]["gb"]["ready"])
        self.assertTrue(current_status(status, self.proxy, time.time()))
        self.assertFalse((self.app.root / "proxy-status.json").exists())

    def test_disabled_stops_children_and_revokes_readiness(self):
        process = Mock()
        process.poll.return_value = None
        self.app.singbox = process
        self.app.reconcile_socks({"proxy": {"enabled": False}})
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertEqual(json.loads(self.app.status_path.read_text())["exits"], {})

    def test_invalid_config_revokes_old_listener_without_publishing_credentials(self):
        process = Mock()
        process.poll.return_value = None
        self.app.singbox = process
        with patch.object(self.app, "build_proxy_config", side_effect=ValueError("secret-value")):
            self.app.reconcile_socks({"proxy": self.proxy})
        status = self.app.status_path.read_text()
        process.terminate.assert_called_once()
        self.assertNotIn("secret-value", status)
        self.assertEqual(json.loads(status)["exits"], {})

    def test_the_engine_network_address_is_resolved_once(self):
        """This used to run inside the inbound loop, so it resolved once per enabled
        country on every reconcile pass. A container's own address is fixed."""
        answers = [(None, None, None, None, ("172.30.0.3", 0))]
        with patch("runtime.egress.socket.getaddrinfo", return_value=answers) as resolve:
            self.assertEqual(self.app.listen_address(), "172.30.0.3")
            self.assertEqual(self.app.listen_address(), "172.30.0.3")
        self.assertEqual(resolve.call_count, 1)

    def test_an_ambiguous_address_is_not_cached_as_a_success(self):
        two = [(None, None, None, None, ("172.30.0.3", 0)),
               (None, None, None, None, ("10.9.0.4", 0))]
        with patch("runtime.egress.socket.getaddrinfo", return_value=two):
            with self.assertRaises(ListenAddressError):
                self.app.listen_address()
        one = [(None, None, None, None, ("172.30.0.3", 0))]
        with patch("runtime.egress.socket.getaddrinfo", return_value=one):
            self.assertEqual(self.app.listen_address(), "172.30.0.3")

    def test_ambiguous_engine_listener_has_a_diagnostic_error_code(self):
        with patch.object(self.app, "build_proxy_config",
                          side_effect=ListenAddressError("two addresses")):
            self.app.reconcile_socks({"proxy": self.proxy})
        status = json.loads(self.app.status_path.read_text())
        self.assertEqual(status["error_code"], "listen_address_unavailable")
        self.assertEqual(status["error_type"], "ListenAddressError")

    def test_direct_mode_does_not_invent_a_socks_listener(self):
        with patch.object(self.app, "listen_address", return_value="172.30.0.3"):
            config, states = self.app.build_proxy_config({"exits": {
                "gb": {"enabled": True, "mode": "direct"}}})
        self.assertEqual(config["inbounds"], [])
        self.assertNotIn("proxy_host", states["gb"])

    def test_country_error_does_not_publish_upstream_secrets(self):
        with patch.object(self.app, "build_proxy_config", return_value=(
                {"inbounds": []}, {"gb": {"ready": False, "terminal": True,
                                          "error": "bad socks5://user:secret@upstream:1080"}})):
            self.app.reconcile_socks({"proxy": self.proxy})
        status = self.app.status_path.read_text()
        self.assertNotIn("secret", status)
        self.assertTrue(json.loads(status)["exits"]["gb"]["terminal"])


class OuterTransportTests(unittest.TestCase):
    def test_parse_credentials_without_revealing_them_in_repr(self):
        endpoint = ProxyEndpoint.parse("socks5://name:p%40ss@egress:22157")
        self.assertEqual(endpoint.password, "p@ss")
        self.assertNotIn("p@ss", repr(endpoint))

    def test_rejects_ambiguous_or_non_socks_configuration(self):
        for url in ("", "http://proxy:80", "socks5://proxy", "socks5://proxy:0",
                    "socks5://proxy:70000", "socks5://proxy:1080/path",
                    "socks5://proxy:1080?fallback=direct"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ProxyEndpoint.parse(url)

    def test_failure_closes_socket_without_direct_retry(self):
        with patch("engine.outer_transport.socks.socksocket") as factory:
            udp = factory.return_value
            udp.bind.side_effect = OSError("proxy unavailable")
            with self.assertRaises(OSError):
                proxy_udp_socket("socks5://egress:22157", ("198.51.100.1", 4500))
            factory.assert_called_once()
            udp.close.assert_called_once()
            udp.connect.assert_not_called()

    def test_bad_destination_does_not_create_socket(self):
        with patch("engine.outer_transport.socks.socksocket") as factory:
            with self.assertRaises(ValueError):
                proxy_udp_socket("socks5://egress:22157", ("not-an-ip", 4500))
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
