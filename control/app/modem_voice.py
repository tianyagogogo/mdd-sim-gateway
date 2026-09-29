"""Whether a modem can hand cellular call audio to the gateway.

A modem that answers a cellular call is not enough: to auto-answer, record or bridge the call,
its audio has to reach the host. Quectel modules do that with `AT+QPCMV` (voice over USB): PCM
at 8 kHz / 16-bit / mono over the USB NMEA serial port (option 0, no sound driver needed) or a
USB sound card (option 2, UAC, needs `AT+QCFG="USBCFG"` with the UAC bit and snd-usb-audio).

The probe only reads. Verified on a DJI-customised EG25-G (firmware QDC507GLEFM21): it answers
`AT+QPCMV=?` with the standard ranges but returns a bare ERROR (no CME code, even with CMEE=2)
to every read and write of AT+QPCMV and to AT+QDAI?. A call on it connects and stays silent in
both directions, with or without UAC enabled.
"""
from __future__ import annotations

import re
import subprocess

from .modem_ims import MODEM_PATH_RE, _at

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown"

READ_RE = re.compile(r"\+QPCMV:\s*(\d)(?:\s*,\s*(\d))?")
TEST_RE = re.compile(r"\+QPCMV:\s*\(")
USBCFG_RE = re.compile(r'\+QCFG:\s*"usbcfg",\s*0x[0-9A-Fa-f]+,\s*0x[0-9A-Fa-f]+((?:\s*,\s*\d)+)',
                       re.IGNORECASE)
REVISION_RE = re.compile(r"([A-Z0-9]{8,}[A-Z0-9_.]*)")

# A reason is shown through t() in the WebUI; tests/test_i18n_coverage.py checks each has zh.
REASONS = {
    "modem_busy": "The modem is not accepting commands right now; try again shortly.",
    "firmware_locked": "The modem firmware lists the call-audio command but refuses it, so call "
                       "audio cannot reach the gateway. Calls connect but stay silent. This is "
                       "seen on DJI-customised modules.",
    "no_command": "This modem has no voice-over-USB command (Quectel AT+QPCMV), so call audio "
                  "cannot reach the gateway.",
}


def _uac(modem_path: str, runner) -> tuple[bool, bool]:
    """(the firmware can expose a USB sound card, the sound card is on)."""
    match = USBCFG_RE.search(_at(modem_path, 'AT+QCFG="USBCFG"', runner) or "")
    if not match:
        return False, False
    flags = [value.strip() for value in match.group(1).split(",") if value.strip()]
    # Seven function flags mean the last one is the UAC switch; six mean no UAC support.
    return len(flags) >= 7, len(flags) >= 7 and flags[-1] == "1"


def status(modem_path: str, runner=subprocess.run) -> dict:
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        return {"status": UNKNOWN, "reason": "The modem is not available."}
    if _at(modem_path, "AT", runner) is None:
        return {"status": UNKNOWN, "reason": REASONS["modem_busy"]}
    revision = REVISION_RE.search(_at(modem_path, "AT+QGMR", runner) or "")
    firmware = revision.group(1) if revision else ""
    uac_capable, uac_enabled = _uac(modem_path, runner)
    read = READ_RE.search(_at(modem_path, "AT+QPCMV?", runner) or "")
    if read:
        return {
            "status": SUPPORTED, "reason": "", "firmware": firmware,
            "active": read.group(1) == "1",
            # Serial PCM needs no sound driver, so it works in containers and on NAS hosts.
            "transports": ["serial"] + (["uac"] if uac_capable else []),
            "uac_enabled": uac_enabled,
        }
    listed = bool(TEST_RE.search(_at(modem_path, "AT+QPCMV=?", runner) or ""))
    return {"status": UNSUPPORTED, "firmware": firmware, "transports": [],
            "uac_enabled": uac_enabled,
            "reason": REASONS["firmware_locked" if listed else "no_command"]}
