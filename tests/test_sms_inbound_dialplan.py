"""An inbound SMS goes to the manager only, not also to the browser softphone.

The softphone has no SIP MESSAGE handler and answered every forwarded text with 405 Method Not
Allowed; the WebUI shows texts from the manager's store, so the forward did nothing but fail.
"""
import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
CTX = dict(webrtc_enable=True, webrtc_user="webrtc", ring_timeout=35, msisdn="+15550100",
           realm="ims.mnc240.mcc310.3gppnetwork.org", vm_ring_seconds=25, vm_max_seconds=120)


def section(text: str, name: str) -> str:
    start = text.index(f"[{name}]")
    end = text.find("\n[", start + 1)
    return text[start:end if end != -1 else None]


class InboundSmsTests(unittest.TestCase):
    def setUp(self):
        env = Environment(loader=FileSystemLoader(str(ROOT / "engine" / "templates")),
                          trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        self.dialplan = env.get_template("extensions.conf.j2").render(**CTX)

    def test_the_manager_gets_it_and_the_softphone_does_not(self):
        inbound = section(self.dialplan, "volte_ims_msg")
        self.assertIn("notify.py sms_in", inbound)
        self.assertNotIn("MessageSend(", inbound)

    def test_outgoing_sms_still_leaves_through_the_carrier(self):
        self.assertIn("MessageSend(pjsip:volte_ims/${EXTEN}@volte_ims",
                      section(self.dialplan, "msg-from-local"))


if __name__ == "__main__":
    unittest.main()
