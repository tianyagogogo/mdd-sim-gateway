"""Request-URI parameters for outgoing calls, per line, with a carrier default.

T-Mobile US (310-240) answers an INVITE to a US number with 500 "CC_IMS_TRY_NEXT_MGCF_FAIL"
unless the request URI carries ;user=phone (#114; confirmed on a live line). The parameters are
added to the INVITE only, never to SMS, and only on lines where they are switched on.
"""
import unittest
import unittest.mock
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from control.app import config
from engine import render

ROOT = Path(__file__).resolve().parent.parent
BASE_CTX = dict(webrtc_enable=True, webrtc_user="webrtc", ring_timeout=35, msisdn="+1",
                realm="ims.mnc240.mcc310.3gppnetwork.org", vm_ring_seconds=25,
                vm_max_seconds=120)
PLAIN_DIAL = "same => n,Dial(PJSIP/${DIALTARGET}@volte_ims,35,b(ims-outbound-headers^s^1))"
LINE = {"id": "1", "index": 0, "imsi": "310240000000000", "mcc": "310", "mnc": "240",
        "iccid": "test-card", "imei": "490154203237518", "ami_secret": "test-secret",
        "epdg": "198.51.100.10", "sip": {"webrtc": {"enable": True, "password": "pw"}}}


def dialplan(**overrides) -> str:
    env = Environment(loader=FileSystemLoader(str(ROOT / "engine" / "templates")),
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    return env.get_template("extensions.conf.j2").render(**{**BASE_CTX, **overrides})


def rendered(sip=None, mcc="310", mnc="240"):
    line = {**LINE, "mcc": mcc, "mnc": mnc,
            "sip": {**LINE["sip"], **(sip or {})}}
    return config.render_instance_json(line, {})["sip"]


class CarrierDefaultTests(unittest.TestCase):
    def test_tmobile_us_turns_on_user_phone_and_nothing_else(self):
        self.assertEqual(config.carrier_sip_defaults("310", "240", "test-card"),
                         {"invite_uri_params_enable": True, "invite_uri_params": "user=phone"})

    def test_a_line_on_it_calls_with_user_phone_and_keeps_sms_as_it_was(self):
        sip = rendered()
        self.assertEqual(sip["invite_uri_params"], "user=phone")
        # The endpoint-wide switch would put ;user=phone on SMS too; it stays off.
        self.assertFalse(sip["user_eq_phone"])
        # A profile without a PANI country no longer raises, and invents no identity.
        self.assertEqual(sip["pani"], "")

    def test_the_line_can_switch_it_off_or_change_it(self):
        self.assertEqual(rendered({"invite_uri_params_enable": False})["invite_uri_params"], "")
        self.assertEqual(rendered({"invite_uri_params": "user=dialstring"})["invite_uri_params"],
                         "user=dialstring")
        # Blank text means "use the carrier default", as for the other SIP fields.
        self.assertEqual(rendered({"invite_uri_params": ""})["invite_uri_params"], "user=phone")

    def test_other_carriers_add_nothing_unless_told_to(self):
        self.assertEqual(rendered(mcc="234", mnc="15")["invite_uri_params"], "")
        self.assertEqual(rendered({"invite_uri_params_enable": True,
                                   "invite_uri_params": "user=phone"},
                                  mcc="234", mnc="15")["invite_uri_params"], "user=phone")
        giffgaff = config.carrier_sip_defaults("234", "10", "test-card")
        # O2 keeps the endpoint-wide switch its SMS has always had, and shows the call option.
        self.assertTrue(giffgaff["user_eq_phone"])
        self.assertTrue(giffgaff["invite_uri_params_enable"])
        self.assertEqual(giffgaff["invite_uri_params"], "user=phone")
        self.assertIn("country=GB", giffgaff["pani"])


class SanitiseTests(unittest.TestCase):
    def test_only_uri_parameters_pass(self):
        for fn in (config.sanitize_uri_params, render.sanitize_uri_params):
            with self.subTest(fn=fn.__module__):
                self.assertEqual(fn(";user=phone"), "user=phone")
                self.assertEqual(fn("user=phone; lr ;x-a=b%20c"), "user=phone;lr;x-a=b%20c")
                # Dial() separators, dialplan expressions, whitespace and newlines never reach
                # the dialplan: the offending part is dropped, not repaired.
                self.assertEqual(fn("user=phone,30,g"), "")
                self.assertEqual(fn("user=phone;a=${SHELL(id)}"), "user=phone")
                self.assertEqual(fn("user=phone&PJSIP/x"), "")
                self.assertEqual(fn("user=phone\nsame => n,System(x)"), "")
                self.assertEqual(fn("a=$[1+1];b=[x]"), "")
                self.assertLessEqual(len(fn("a=" + "b" * 500)), 128)

    def test_saving_a_line_stores_the_cleaned_text(self):
        with unittest.mock.patch.object(config, "save"), \
                unittest.mock.patch.object(config, "load",
                                           return_value={"instances": {}, "settings": {}}):
            saved = config.upsert_instance({"id": "1", "imsi": "310240000000000",
                                            "sip": {"invite_uri_params": " ;user=phone,1 "}})
        self.assertEqual(saved["sip"]["invite_uri_params"], "")


class EngineTests(unittest.TestCase):
    def ctx(self, sip):
        return render.build_context({"id": "1", "imsi": "310240000000000", "mcc": "310",
                                     "mnc": "240", "ami_secret": "x", "local_addr": "172.17.0.2",
                                     "sip": {"webrtc": {"enable": True, "password": "pw"},
                                             **sip}})

    def test_the_engine_receives_the_cleaned_parameters(self):
        self.assertEqual(self.ctx({})["invite_uri_params"], "")
        self.assertEqual(self.ctx({"invite_uri_params": "user=phone,g"})["invite_uri_params"], "")
        self.assertEqual(self.ctx({"invite_uri_params": "user=phone"})["invite_uri_params"],
                         "user=phone")
        self.assertEqual(self.ctx({"invite_uri_params": "user=phone",
                                   "invite_uri_params_enable": False})["invite_uri_params"], "")

    def test_other_lines_dial_exactly_as_before(self):
        self.assertIn(PLAIN_DIAL, dialplan(invite_uri_params=""))

    def test_the_uri_is_spelled_out_with_escaped_semicolons(self):
        out = dialplan(invite_uri_params="user=phone;x-a=b")
        self.assertNotIn(PLAIN_DIAL, out)
        self.assertIn("same => n,Dial(PJSIP/volte_ims/sip:${DIALTARGET}"
                      r"@ims.mnc240.mcc310.3gppnetwork.org\;user=phone\;x-a=b,35,"
                      "b(ims-outbound-headers^s^1))", out)


if __name__ == "__main__":
    unittest.main()
