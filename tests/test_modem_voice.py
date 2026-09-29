import unittest
from types import SimpleNamespace

from control.app import modem_voice

MODEM = "/org/freedesktop/ModemManager1/Modem/2"

# The DJI-customised EG25-G, answers as ModemManager relayed them on the DS1621+ and the Pi.
DJI = {
    "AT": "response: ''",
    "AT+QGMR": "response: 'QDC507GLEFM21_01.001.02.001'",
    'AT+QCFG="USBCFG"': "response: '+QCFG: \"usbcfg\",0x2C7C,0x125,1,1,1,1,1,0,0'",
    "AT+QPCMV=?": "response: '+QPCMV: (0,1),(0-2)'",
    # AT+QPCMV? is refused: ModemManager fails the command.
}


class Runner:
    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def __call__(self, args, **kwargs):
        command = args[-1].split("=", 1)[1]
        self.commands.append(command)
        if command not in self.answers:
            return SimpleNamespace(returncode=1, stdout="", stderr="Unknown error")
        return SimpleNamespace(returncode=0, stdout=self.answers[command], stderr="")


class ModemVoiceTests(unittest.TestCase):
    def test_dji_firmware_lists_the_command_but_refuses_it(self):
        value = modem_voice.status(MODEM, Runner(DJI))
        self.assertEqual(value["status"], modem_voice.UNSUPPORTED)
        self.assertEqual(value["reason"], modem_voice.REASONS["firmware_locked"])
        self.assertEqual(value["firmware"], "QDC507GLEFM21_01.001.02.001")
        self.assertEqual(value["transports"], [])

    def test_a_module_with_call_audio_offers_serial_and_uac(self):
        answers = {**DJI, "AT+QGMR": "response: 'EG25GGBR07A08M2G_01.003.01.003'",
                   "AT+QPCMV?": "response: '+QPCMV: 0'"}
        value = modem_voice.status(MODEM, Runner(answers))
        self.assertEqual(value["status"], modem_voice.SUPPORTED)
        self.assertFalse(value["active"])
        self.assertEqual(value["transports"], ["serial", "uac"])
        self.assertFalse(value["uac_enabled"])

    def test_firmware_without_the_uac_switch_offers_serial_only(self):
        answers = {**DJI, "AT+QPCMV?": "response: '+QPCMV: 1,0'",
                   'AT+QCFG="USBCFG"': "response: '+QCFG: \"usbcfg\",0x2C7C,0x125,1,1,1,1,1,0'"}
        value = modem_voice.status(MODEM, Runner(answers))
        self.assertTrue(value["active"])
        self.assertEqual(value["transports"], ["serial"])

    def test_a_module_without_the_command_says_so(self):
        answers = {"AT": "response: ''"}
        value = modem_voice.status(MODEM, Runner(answers))
        self.assertEqual(value["status"], modem_voice.UNSUPPORTED)
        self.assertEqual(value["reason"], modem_voice.REASONS["no_command"])

    def test_a_modem_that_answers_nothing_is_unknown_not_unsupported(self):
        value = modem_voice.status(MODEM, Runner({}))
        self.assertEqual(value["status"], modem_voice.UNKNOWN)

    def test_the_probe_never_writes_a_setting(self):
        runner = Runner(DJI)
        modem_voice.status(MODEM, runner)
        self.assertTrue(all(command.endswith("?") or command in ("AT", "AT+QGMR")
                            or command.endswith('="USBCFG"') or command.endswith("=?")
                            for command in runner.commands), runner.commands)

    def test_only_a_modemmanager_modem_path_is_accepted(self):
        runner = Runner(DJI)
        self.assertEqual(modem_voice.status("/dev/ttyUSB2", runner)["status"], modem_voice.UNKNOWN)
        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()
