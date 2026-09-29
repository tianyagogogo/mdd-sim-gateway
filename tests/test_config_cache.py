import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from control.app import config
from host.mdd_orchestrator import Orchestrator


class ConfigLoadCacheTests(unittest.TestCase):
    """load() used to parse config.yaml on every settings read and line lookup, which was
    most of Control's CPU on a Raspberry Pi with the device page open."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        paths = patch.multiple(config, DATA_DIR=self.temp.name,
                               CONFIG_PATH=str(Path(self.temp.name) / "config.yaml"))
        paths.start()
        self.addCleanup(paths.stop)
        config._loaded = None
        self.addCleanup(setattr, config, "_loaded", None)
        config.save({"settings": {"timezone": "Asia/Shanghai"},
                     "instances": {"1": {"id": "1", "name": "one"}}})

    def parses(self):
        return patch.object(config.yaml, "load", wraps=yaml.load)

    def test_an_unchanged_file_is_parsed_once(self):
        with self.parses() as parse:
            config.load()
            config.get_settings()
            config.list_instances()
            config.get_instance("1")
        self.assertEqual(parse.call_count, 1)

    def test_callers_get_their_own_copy(self):
        first = config.load()
        first["instances"]["1"]["name"] = "mutated"
        first["settings"]["timezone"] = "UTC"
        again = config.load()
        self.assertEqual(again["instances"]["1"]["name"], "one")
        self.assertEqual(again["settings"]["timezone"], "Asia/Shanghai")

    def test_saving_through_config_is_seen_at_once(self):
        config.load()
        config.upsert_instance({"id": "1", "name": "renamed"})
        self.assertEqual(config.get_instance("1")["name"], "renamed")

    def test_a_write_by_another_process_is_picked_up(self):
        config.load()
        path = Path(config.CONFIG_PATH)
        doc = yaml.safe_load(path.read_text())
        doc["instances"]["1"]["name"] = "edited elsewhere"
        path.write_text(yaml.safe_dump(doc))
        # Make sure the stat key moves even on filesystems with coarse timestamps.
        later = time.time() + 5
        os.utime(path, (later, later))
        self.assertEqual(config.get_instance("1")["name"], "edited elsewhere")


class SubscriptionCacheTests(unittest.TestCase):
    def test_the_subscription_is_parsed_again_only_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator.__new__(Orchestrator)
            app.root = Path(temp)
            app.cache = Path(temp) / "subscription.yaml"
            cache = app.root / "subscriptions" / "p1.yaml"
            cache.parent.mkdir(parents=True)
            cache.write_text(yaml.safe_dump({"proxies": [{"name": "HK 01", "type": "ss"}]}))
            with patch("host.mdd_orchestrator.load_yaml_text",
                       wraps=__import__("host.mdd_orchestrator", fromlist=["x"]).load_yaml_text) as parse:
                first = app.subscription("https://example.invalid/sub", 30, "p1")
                first["proxies"][0]["name"] = "mutated"
                second = app.subscription("https://example.invalid/sub", 30, "p1")
                self.assertEqual(second["proxies"][0]["name"], "HK 01")
                self.assertEqual(parse.call_count, 1)
                cache.write_text(yaml.safe_dump({"proxies": [{"name": "JP 01", "type": "ss"}]}))
                later = time.time() + 5
                os.utime(cache, (later, later))
                self.assertEqual(app.subscription("https://example.invalid/sub", 30, "p1")
                                 ["proxies"][0]["name"], "JP 01")
                self.assertEqual(parse.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class BuiltInModemProfileTests(unittest.TestCase):
    """The first start writes the defaults to config.yaml and the saved hardware block then
    replaced them wholesale, so a model added in a later release never reached an existing
    install."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        paths = patch.multiple(config, DATA_DIR=self.temp.name,
                               CONFIG_PATH=str(Path(self.temp.name) / "config.yaml"))
        paths.start()
        self.addCleanup(paths.stop)
        config._loaded = None
        self.addCleanup(setattr, config, "_loaded", None)

    def profiles(self, saved):
        config.save({"settings": {"hardware": {"auto_detect": True, "modem_profiles": saved}},
                     "instances": {}})
        return {(p["vid"], p["pid"]): p
                for p in config.get_settings()["hardware"]["modem_profiles"]}

    def test_an_install_saved_before_the_ec20_existed_still_recognises_it(self):
        profiles = self.profiles([{"name": "DJI/Quectel EC25", "vid": "2c7c", "pid": "0125",
                                   "at_interface": 2}])
        self.assertEqual(profiles[("05c6", "9215")]["at_interface"], 2)
        self.assertEqual(profiles[("05c6", "9215")]["name"], "Quectel EC20")

    def test_an_operators_own_entry_for_a_built_in_model_wins(self):
        profiles = self.profiles([{"name": "My EC20", "vid": "05C6", "pid": "9215",
                                   "at_interface": 3}])
        self.assertNotIn(("05c6", "9215"), profiles)
        self.assertEqual(profiles[("05C6", "9215")]["at_interface"], 3)
        self.assertIn(("2c7c", "0125"), profiles)
