import unittest
from types import SimpleNamespace

from control.app import modem_ims

MODEM = "/org/freedesktop/ModemManager1/Modem/3"

# Answers captured from the EC25 on the DS1621+ with a China Telecom SIM, after IMS was on.
ANSWERS = {
    'AT+QCFG="ims"': "response: '+QCFG: \"ims\",1,1'",
    'AT+QMBNCFG="AutoSel"': "response: '+QMBNCFG: \"AutoSel\",1'",
    'AT+QMBNCFG="List"': ("response: '+QMBNCFG: \"List\",0,0,0,\"ROW_Generic_3GPP\",0x0501081F,202304121\n"
                          "+QMBNCFG: \"List\",12,1,1,\"VoLTE_OPNMKT_CT\",0x050113FC,202301161\n"
                          "+QMBNCFG: \"List\",13,0,0,\"CU-VoLTE\",0x05011508,202212271'"),
}


class Runner:
    def __init__(self, answers=None, refuse=()):
        self.answers = dict(ANSWERS if answers is None else answers)
        self.refuse = set(refuse)
        self.commands = []

    def __call__(self, args, **kwargs):
        command = args[-1].split("=", 1)[1]
        self.commands.append(command)
        if command in self.refuse:
            return SimpleNamespace(returncode=1, stdout="", stderr="error")
        return SimpleNamespace(returncode=0, stdout=self.answers.get(command, "response: ''"),
                               stderr="")


class ModemImsTests(unittest.TestCase):
    def test_status_reads_mode_registration_and_carrier_profile(self):
        value = modem_ims.status(MODEM, Runner())
        self.assertTrue(value["supported"])
        self.assertTrue(value["enabled"])
        self.assertTrue(value["registered"])
        self.assertTrue(value["auto_select"])
        self.assertEqual(value["active_profile"], "VoLTE_OPNMKT_CT")
        self.assertIn("CU-VoLTE", value["profiles"])

    def test_the_factory_generic_profile_reads_as_off(self):
        """The same modem before the change: IMS follows a generic profile that has it off."""
        answers = {**ANSWERS, 'AT+QCFG="ims"': "response: '+QCFG: \"ims\",0,0'",
                   'AT+QMBNCFG="AutoSel"': "response: '+QMBNCFG: \"AutoSel\",0'"}
        value = modem_ims.status(MODEM, Runner(answers))
        self.assertFalse(value["enabled"])
        self.assertFalse(value["registered"])
        self.assertFalse(value["auto_select"])

    def test_a_modem_without_quectel_commands_is_unsupported(self):
        value = modem_ims.status(MODEM, Runner(refuse={'AT+QCFG="ims"'}))
        self.assertFalse(value["supported"])
        self.assertIn("Quectel", value["reason"])

    def test_turning_on_selects_the_carrier_profile_then_resets(self):
        runner = Runner()
        self.assertEqual(modem_ims.set_enabled(MODEM, True, runner),
                         {"ok": True, "restarting": True})
        self.assertEqual(runner.commands,
                         ['AT+QMBNCFG="AutoSel",1', 'AT+QCFG="ims",1', "AT+CFUN=1,1"])

    def test_turning_off_forces_ims_off_then_resets(self):
        runner = Runner()
        modem_ims.set_enabled(MODEM, False, runner)
        self.assertEqual(runner.commands, ['AT+QCFG="ims",2', "AT+CFUN=1,1"])

    def test_a_rejected_setting_does_not_reset_the_modem(self):
        runner = Runner(refuse={'AT+QCFG="ims",1'})
        result = modem_ims.set_enabled(MODEM, True, runner)
        self.assertFalse(result["ok"])
        self.assertNotIn("AT+CFUN=1,1", runner.commands)

    def test_only_a_modemmanager_modem_path_is_accepted(self):
        runner = Runner()
        self.assertFalse(modem_ims.status("/tmp/x; rm", runner)["supported"])
        self.assertFalse(modem_ims.set_enabled("", True, runner)["ok"])
        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()
