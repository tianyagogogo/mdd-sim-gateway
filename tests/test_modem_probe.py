import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from control.app import config, main
from host import modem_probe, vpcd_modem_bridge


def usb_device(root: Path, name: str, vid: str, pid: str, ttys: dict[int, str],
               product: str = "", device_class: str = "00", acm: bool = False):
    device = root / name
    device.mkdir(parents=True)
    (device / "idVendor").write_text(vid + "\n")
    (device / "idProduct").write_text(pid + "\n")
    (device / "bDeviceClass").write_text(device_class + "\n")
    if product:
        (device / "product").write_text(product + "\n")
    for number, tty in ttys.items():
        interface = root / f"{name}:1.{number}"
        (interface / "tty" / tty if acm else interface / tty).mkdir(parents=True)


class FakeCard:
    """Answers like a module: a queue of CSIM responses and one +CPIN? answer."""

    def __init__(self, csim=(), pin="+CPIN: READY\r\nOK"):
        self.csim_responses = list(csim)
        self.pin = pin
        self.closed_channels = []
        self.closed = False

    def csim(self, apdu):
        response = self.csim_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close_channel(self, channel):
        self.closed_channels.append(channel)

    def _at(self, command):
        if self.pin.startswith("ERR:"):
            raise vpcd_modem_bridge.ModemError(self.pin[4:])
        return self.pin.encode()

    def close(self):
        self.closed = True


class CandidateTests(unittest.TestCase):
    def test_only_unknown_modem_like_devices_are_offered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            usb_device(root, "3-2", "05c6", "9215", {0: "ttyUSB0", 1: "ttyUSB1", 2: "ttyUSB2"},
                       product="Qualcomm CDMA Technologies MSM")
            usb_device(root, "3-3", "2c7c", "0125", {2: "ttyUSB5", 3: "ttyUSB6"})   # known
            usb_device(root, "3-4", "1a86", "7523", {0: "ttyUSB9"})                 # serial cable
            usb_device(root, "3-5", "05e3", "0610", {0: "ttyUSB8", 1: "ttyUSB7"},
                       device_class="09")                                           # hub
            usb_device(root, "3-6", "1e0e", "9001", {2: "ttyACM0", 3: "ttyACM1"}, acm=True)

            found = modem_probe.list_candidates({("2c7c", "0125")}, None, root)

            self.assertEqual([item["usb_path"] for item in found], ["3-2", "3-6"])
            self.assertEqual(found[0]["interfaces"], {"0": "ttyUSB0", "1": "ttyUSB1",
                                                      "2": "ttyUSB2"})
            self.assertEqual(found[1]["interfaces"], {"2": "ttyACM0", "3": "ttyACM1"})

    def test_a_single_port_device_modemmanager_claimed_is_offered_with_its_at_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            usb_device(root, "1-1", "1199", "9071", {3: "ttyUSB3"})
            obj = "/org/freedesktop/ModemManager1/Modem/4"

            def run(args):
                if args == ["mmcli", "-L"]:
                    return SimpleNamespace(returncode=0, stdout=f"    {obj} [Sierra] EM7455\n")
                return SimpleNamespace(returncode=0, stdout=(
                    "modem.generic.device                 : /sys/devices/pci0000:00/usb1/1-1\n"
                    "modem.generic.primary-port           : ttyUSB3\n"
                    "modem.generic.ports.value[1]         : cdc-wdm0 (qmi)\n"
                    "modem.generic.ports.value[2]         : ttyUSB3 (at)\n"))

            found = modem_probe.list_candidates(set(), run, root)

            self.assertEqual(found[0]["mm_object"], obj)
            self.assertEqual(found[0]["mm_at_ports"], ["ttyUSB3"])

    def test_modemmanager_is_not_asked_when_nothing_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            usb_device(root, "3-3", "2c7c", "0125", {2: "ttyUSB5", 3: "ttyUSB6"})
            calls = []
            modem_probe.list_candidates({("2c7c", "0125")}, calls.append, root)
            self.assertEqual(calls, [])


class ProbeTests(unittest.TestCase):
    candidate = {"usb_path": "3-2", "vid": "05c6", "pid": "9215",
                 "interfaces": {"0": "ttyUSB0", "1": "ttyUSB1", "2": "ttyUSB2"},
                 "mm_object": "", "mm_at_ports": []}

    def serial(self, card, answering="/dev/ttyUSB2", busy=()):
        opened = []

        def open_port(port):
            opened.append(port)
            if port in busy:
                raise OSError(f"Could not exclusively lock port {port}")
            if port != answering:
                raise vpcd_modem_bridge.ModemError("timeout waiting for ATE0")
            return card
        return open_port, opened

    def test_the_at_port_is_found_and_a_channel_opened_and_closed(self):
        card = FakeCard(csim=[bytes.fromhex("019000")])
        open_port, opened = self.serial(card)

        outcome = modem_probe.probe(self.candidate, serial_card=open_port)

        self.assertEqual(outcome["result"], modem_probe.USABLE)
        self.assertEqual(outcome["at_interface"], 2)
        self.assertEqual(opened, ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"])
        self.assertEqual(card.closed_channels, [1])
        self.assertTrue(card.closed)

    def test_a_channel_outside_the_bridges_range_is_still_closed(self):
        card = FakeCard(csim=[bytes.fromhex("059000")])
        open_port, _opened = self.serial(card)
        outcome = modem_probe.probe(self.candidate, serial_card=open_port)
        self.assertEqual(card.closed_channels, [5])
        self.assertEqual(outcome["result"], modem_probe.USABLE)

    def test_without_a_ready_sim_the_model_is_unverified(self):
        card = FakeCard(csim=[vpcd_modem_bridge.ModemError("+CME ERROR: SIM not inserted")],
                        pin="ERR:+CME ERROR: SIM not inserted")
        open_port, _opened = self.serial(card)
        outcome = modem_probe.probe(self.candidate, serial_card=open_port)
        self.assertEqual(outcome["result"], modem_probe.UNVERIFIED)
        self.assertEqual(outcome["at_interface"], 2)

    def test_a_ready_sim_refusing_the_channel_is_unsupported(self):
        card = FakeCard(csim=[vpcd_modem_bridge.ModemError("ERROR")])
        open_port, _opened = self.serial(card)
        outcome = modem_probe.probe(self.candidate, serial_card=open_port)
        self.assertEqual(outcome["result"], modem_probe.UNSUPPORTED)
        self.assertTrue(card.closed)

    def test_no_answer_on_any_port(self):
        open_port, _opened = self.serial(FakeCard(), answering="")
        outcome = modem_probe.probe(self.candidate, serial_card=open_port)
        self.assertEqual(outcome["result"], modem_probe.NO_AT_PORT)

    def test_a_locked_port_is_reported_as_busy(self):
        open_port, _opened = self.serial(FakeCard(), answering="", busy={"/dev/ttyUSB2"})
        outcome = modem_probe.probe(self.candidate, serial_card=open_port)
        self.assertEqual(outcome["result"], modem_probe.PORT_BUSY)
        self.assertIn("ttyUSB2", outcome["detail"])

    def test_a_claimed_modem_is_probed_through_modemmanager_only(self):
        card = FakeCard(csim=[bytes.fromhex("019000")])
        candidate = {**self.candidate, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
                     "mm_at_ports": ["ttyUSB2"]}

        def never(port):
            raise AssertionError("a claimed modem's tty must not be opened")

        outcome = modem_probe.probe(candidate, serial_card=never,
                                    modemmanager_card=lambda obj: card)
        self.assertEqual(outcome["result"], modem_probe.USABLE)
        self.assertEqual(outcome["at_interface"], 2)


class ProbeRequestTests(unittest.TestCase):
    def test_a_request_is_consumed_and_answered(self):
        with tempfile.TemporaryDirectory() as temp:
            requests = modem_probe.ProbeRequests(Path(temp))
            requests.request_dir.mkdir(parents=True)
            (requests.request_dir / "probe-1.json").write_text(json.dumps(
                {"request_id": "probe-1", "usb_path": "3-2", "vid": "05c6", "pid": "9215"}))
            candidate = {**ProbeTests.candidate, "product": "EC20", "manufacturer": "Quectel"}
            card = FakeCard(csim=[bytes.fromhex("019000")])

            requests.process([candidate], log=lambda message: None,
                             serial_card=lambda port: card if port.endswith("2") else
                             (_ for _ in ()).throw(vpcd_modem_bridge.ModemError("no")))

            self.assertEqual(list(requests.request_dir.glob("*.json")), [])
            status = json.loads((requests.status_dir / "probe-1.json").read_text())
            self.assertEqual(status["state"], "done")
            self.assertEqual(status["result"], modem_probe.USABLE)
            self.assertEqual(status["name"], "Quectel EC20")

    def test_a_request_for_a_device_no_longer_listed_is_not_probed(self):
        with tempfile.TemporaryDirectory() as temp:
            requests = modem_probe.ProbeRequests(Path(temp))
            requests.request_dir.mkdir(parents=True)
            (requests.request_dir / "probe-2.json").write_text(json.dumps(
                {"request_id": "probe-2", "usb_path": "9-9", "vid": "1", "pid": "2"}))
            requests.process([], log=lambda message: None)
            status = json.loads((requests.status_dir / "probe-2.json").read_text())
            self.assertEqual(status["result"], modem_probe.NOT_FOUND)


class ControlModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        paths = patch.multiple(config, DATA_DIR=self.temp.name,
                               CONFIG_PATH=str(Path(self.temp.name) / "config.yaml"))
        paths.start()
        self.addCleanup(paths.stop)
        config._loaded = None
        self.addCleanup(setattr, config, "_loaded", None)
        config.save({"settings": {}, "instances": {}})
        publish = patch.object(main.egress, "publish")
        publish.start()
        self.addCleanup(publish.stop)

    def test_a_usable_probe_is_saved_and_can_be_removed_again(self):
        status = {"vid": "1E0E", "pid": "9001", "at_interface": 3, "name": "SIMCOM",
                  "result": "usable"}
        main._save_probed_modem_profile(status)
        saved = {main._modem_profile_key(p): p
                 for p in config.get_settings()["hardware"]["modem_profiles"]}
        self.assertEqual(saved[("1e0e", "9001")]["at_interface"], 3)
        self.assertTrue(saved[("1e0e", "9001")]["verified"])

        view = main._custom_model_view({"vid": "1e0e", "pid": "9001"}, {})
        self.assertTrue(view["verified"])

        asyncio.run(main.api_delete_modem_profile("1e0e", "9001"))
        keys = {main._modem_profile_key(p)
                for p in config.get_settings()["hardware"]["modem_profiles"]}
        self.assertNotIn(("1e0e", "9001"), keys)

    def test_an_unverified_model_is_verified_once_its_bridge_opens_channels(self):
        main._save_probed_modem_profile({"vid": "1e0e", "pid": "9001", "at_interface": 2,
                                         "name": "SIMCOM", "result": "unverified"})
        device = {"vid": "1e0e", "pid": "9001"}
        self.assertFalse(main._custom_model_view(device, {})["verified"])
        self.assertTrue(main._custom_model_view(device, {"channel_status": "ready"})["verified"])

    def test_built_in_models_cannot_be_removed(self):
        with self.assertRaises(main.HTTPException) as caught:
            asyncio.run(main.api_delete_modem_profile("2c7c", "0125"))
        self.assertEqual(caught.exception.status_code, 400)

    def test_the_probe_endpoint_saves_what_the_host_reported(self):
        orchestrator = Path(self.temp.name) / "orchestrator"
        orchestrator.mkdir()
        (orchestrator / "usb-candidates.json").write_text(json.dumps({
            "updated_at": time.time(),
            "candidates": [{"usb_path": "3-2", "vid": "05c7", "pid": "1234",
                            "interfaces": {"2": "ttyUSB2", "3": "ttyUSB3"}}]}))

        def host_answers(candidate):
            request_id, status_path = original(candidate)
            Path(status_path).parent.mkdir(parents=True, exist_ok=True)
            Path(status_path).write_text(json.dumps({
                "request_id": request_id, "state": "done", "result": "usable",
                "vid": "05c7", "pid": "1234", "at_interface": 2, "name": "Test modem"}))
            return request_id, status_path

        original = main._write_modem_probe_request
        with patch.object(main, "_write_modem_probe_request", side_effect=host_answers):
            answer = asyncio.run(main.api_probe_usb_candidate("3-2"))

        self.assertEqual(answer["result"], "usable")
        self.assertEqual(answer["saved"]["at_interface"], 2)
        self.assertEqual(answer["saved"]["source"], "probe")


if __name__ == "__main__":
    unittest.main()
