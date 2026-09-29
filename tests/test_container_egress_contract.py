import copy
import os
import time
import unittest
from unittest.mock import patch

from control.app import egress
from control.app.egress_contract import proxy_fingerprint, socks_endpoint


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.proxy = {"enabled": True, "exits": {
            "gb": {"enabled": True, "mode": "manual", "proxy_url": "socks5://upstream:1080"}}}
        self.state = {"version": 1, "transport": "socks5", "enabled": True,
                      "updated_at": time.time(), "config_fingerprint": proxy_fingerprint(self.proxy),
                      "exits": {"gb": {"ready": True, "mode": "manual", "transport": "socks5",
                                       "proxy_host": "mdd-egress", "proxy_port": 22157}}}
        self.inst = {"id": "sim1", "proxy_country": "gb"}
        self.env = patch.dict(os.environ, {"MDD_EGRESS_TRANSPORT": "socks5"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def ensure(self, state=None):
        with patch.object(egress, "publish"), patch.object(egress, "status", return_value=state or self.state), \
                patch.object(egress.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(egress.time, "sleep"):
            return egress.ensure_line(self.inst, {"proxy": self.proxy}, timeout=1)

    def test_matching_country_returns_internal_endpoint_not_upstream_credentials(self):
        result = self.ensure()
        self.assertEqual(result["proxy_url"], "socks5://mdd-egress:22157")

    def test_stale_future_nan_missing_or_wrong_contract_cannot_start(self):
        for field, value in (("updated_at", time.time() - 16), ("updated_at", time.time() + 30),
                             ("updated_at", float("nan")), ("updated_at", "today"),
                             ("version", 0), ("transport", "host"), ("enabled", False),
                             ("config_fingerprint", "old"), ("error_type", "RuntimeError")):
            state = {**self.state, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(egress.EgressError):
                self.ensure(state)

    def test_configuration_change_revokes_previously_ready_exit(self):
        self.proxy["exits"]["gb"]["proxy_url"] = "socks5://replacement:1080"
        with self.assertRaises(egress.EgressError):
            self.ensure()

    def test_ready_other_country_cannot_authorize_this_line(self):
        state = copy.deepcopy(self.state)
        state["exits"]["us"] = state["exits"].pop("gb")
        with self.assertRaises(egress.EgressError):
            self.ensure(state)

    def test_direct_requires_explicit_configuration(self):
        self.state["exits"]["gb"] = {"ready": True, "transport": "direct", "mode": "direct"}
        with self.assertRaises(egress.EgressError):
            self.ensure()
        self.proxy["exits"]["gb"] = {"enabled": True, "mode": "direct"}
        self.state["config_fingerprint"] = proxy_fingerprint(self.proxy)
        self.assertEqual(self.ensure()["transport"], "direct")

    def test_invalid_transport_fails_even_when_proxy_disabled(self):
        with patch.dict(os.environ, {"MDD_EGRESS_TRANSPORT": "socks"}), \
                patch.object(egress, "publish") as publish:
            with self.assertRaises(egress.EgressError):
                egress.ensure_line(self.inst, {})
            publish.assert_not_called()

    def test_legacy_route_file_is_never_consumed_in_socks_mode(self):
        with patch.object(egress, "_read_json", return_value={}) as read:
            egress.status()
        self.assertTrue(read.call_args.args[0].endswith("/socks-egress-status.json"))

    def test_bad_endpoint_cannot_be_interpreted_as_url_or_credentials(self):
        for host, port in (("user:secret@relay", 1080), ("relay/path", 1080),
                           ("relay", True), ("relay", "1080"), ("relay", 65536)):
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                socks_endpoint({"proxy_host": host, "proxy_port": port})

    def test_fingerprint_is_key_order_independent(self):
        self.assertEqual(proxy_fingerprint({"a": 1, "b": 2}),
                         proxy_fingerprint({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
