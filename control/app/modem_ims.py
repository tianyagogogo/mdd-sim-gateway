"""VoLTE/IMS on the modem itself, for carriers whose 4G voice and SMS need it.

This is the modem's own IMS registration on the cellular network, not the VoWiFi line's. China
Telecom (and other VoLTE-only networks) have no circuit-switched fallback on LTE, so without it
a modem registers and carries data, but callers hear "switched off" and every text fails with
QMI `WmsMessageDeliveryFailure`. Verified on an EC25 with a China Telecom SIM: turning on the
modem's automatic carrier profile (MBN) selection and IMS, then resetting the modem, selected
`VoLTE_OPNMKT_CT`, registered IMS and the same SIM's texts went through.

Only Quectel modules are handled (`AT+QCFG="ims"` / `AT+QMBNCFG`). Commands go through
`mmcli --command`, which needs ModemManager running with --debug, as the gateway configures it.
"""
from __future__ import annotations

import re
import subprocess

MODEM_PATH_RE = re.compile(r"^/org/freedesktop/ModemManager1/Modem/\d+$")
IMS_RE = re.compile(r'\+QCFG:\s*"ims",\s*(\d+)\s*,\s*(\d+)')
AUTOSEL_RE = re.compile(r'\+QMBNCFG:\s*"AutoSel",\s*(\d+)')
PROFILE_RE = re.compile(r'\+QMBNCFG:\s*"List",\s*\d+\s*,\s*(\d)\s*,\s*(\d)\s*,\s*"([^"]+)"')

# AT+QCFG="ims",<mode>: 0 follows the carrier profile, 1 forces IMS on, 2 forces it off.
IMS_FOLLOW_PROFILE, IMS_ON, IMS_OFF = 0, 1, 2
TIMEOUT = 15


def _at(modem_path: str, command: str, runner=subprocess.run) -> str | None:
    """The response text, or None when the modem or ModemManager refused the command."""
    try:
        result = runner(["mmcli", "-m", modem_path, f"--command={command}"],
                        capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout or ""


def status(modem_path: str, runner=subprocess.run) -> dict:
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        return {"supported": False, "reason": "The modem is not available."}
    ims = IMS_RE.search(_at(modem_path, 'AT+QCFG="ims"', runner) or "")
    if not ims:
        return {"supported": False,
                "reason": "This modem does not expose IMS settings (Quectel modules only)."}
    mode, ready = int(ims.group(1)), int(ims.group(2))
    autosel = AUTOSEL_RE.search(_at(modem_path, 'AT+QMBNCFG="AutoSel"', runner) or "")
    listing = _at(modem_path, 'AT+QMBNCFG="List"', runner) or ""
    profiles = [{"name": name, "selected": selected == "1", "active": active == "1"}
                for selected, active, name in PROFILE_RE.findall(listing)]
    active = next((item["name"] for item in profiles if item["active"]), "")
    return {
        "supported": True,
        "mode": mode,
        # Mode 0 defers to the carrier profile, so the live state is the only honest answer.
        "enabled": mode == IMS_ON or (mode == IMS_FOLLOW_PROFILE and ready == 1),
        "registered": ready == 1,
        "auto_select": bool(autosel and autosel.group(1) == "1"),
        "active_profile": active,
        "profiles": [item["name"] for item in profiles],
    }


def set_enabled(modem_path: str, enabled: bool, runner=subprocess.run) -> dict:
    """Write the setting and reset the modem; it re-registers in about a minute.

    Turning IMS on also lets the modem pick the carrier profile for the inserted SIM, which is
    what enables VoLTE for that carrier; forcing IMS on under a generic profile is not enough.
    """
    if not MODEM_PATH_RE.fullmatch(str(modem_path or "")):
        return {"ok": False, "error": "The modem is not available."}
    commands = (['AT+QMBNCFG="AutoSel",1', f'AT+QCFG="ims",{IMS_ON}'] if enabled
                else [f'AT+QCFG="ims",{IMS_OFF}'])
    for command in commands:
        if _at(modem_path, command, runner) is None:
            return {"ok": False, "error": f"The modem rejected {command}."}
    # The carrier profile and IMS mode are read at boot. The reset drops the modem off USB
    # for a moment; the hardware layer re-enumerates it and rebuilds the SIM bridge.
    _at(modem_path, "AT+CFUN=1,1", runner)
    return {"ok": True, "restarting": True}
