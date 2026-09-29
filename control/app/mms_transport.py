"""HTTP to a carrier's MMSC, over whichever network can reach it.

An MMSC (and the WAP proxy in front of it) is normally reachable only from the carrier's MMS
APN: the WAP proxy is commonly unreachable from the internet APN and the MMSC's hostname
does not resolve in public DNS. Two clients are provided:

- ModemSocketHttp opens the MMS APN inside the modem itself, with Quectel's embedded TCP/IP
  stack (AT+QICSGP/QIACT/QIOPEN), driven through ModemManager's command channel. The host's
  own data connection on the modem is untouched and nothing new appears in the host routing
  table. It needs ModemManager running with --debug, as the SIM bridge does.
- HostHttp is a plain HTTP client from the host, for a carrier whose MMSC is reachable from
  wherever the host is (or a deployment that routes the MMS APN itself).

The AT commands travel one of two ways. Through ModemManager's command channel the modem
stays fully shared, but ModemManager completes a command only on a final result it knows and
AT+QISENDEX ends with "SEND OK", so every upload chunk waits out a command timeout: about 100
bytes a second, enough for a retrieval or an acknowledgement and nothing more. When
ModemManager is told to ignore one of the module's spare AT ports, the gateway uses that port
directly with the prompt-driven binary send, which moves an MMS in about a second.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger("vowifi.mms")

PROVIDER_DB = os.environ.get(
    "MDD_MOBILE_BROADBAND_PROVIDER_INFO",
    "/usr/share/mobile-broadband-provider-info/serviceproviders.xml")
TRANSPORTS = ("auto", "modem", "host")
DEFAULT_MAX_SIZE = 300 * 1024
DEFAULT_USER_AGENT = "Android-Mms/2.0"
MMS_CONTENT_TYPE = "application/vnd.wap.mms-message"


class MmsTransportError(Exception):
    """A failed MMSC exchange. `retryable` is False when trying again cannot help."""

    def __init__(self, message: str, *, retryable: bool = True, after_send: bool = False,
                 unsent: bool = False):
        super().__init__(message)
        self.retryable = retryable
        # The request reached the network before this failed: for a send, the MMSC may
        # have accepted the message, so the outcome is unknown rather than failed.
        self.after_send = after_send
        # The MMSC cannot have received the whole request -- no connection was made, or the
        # upload stopped before its last chunk -- so submitting it again cannot deliver the
        # message twice.
        self.unsent = unsent


@dataclass
class HttpResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""


# ----------------------------- settings -----------------------------

def lookup_provider(mcc, mnc, path: str | None = None) -> dict | None:
    """MMS APN, MMSC and proxy for a network from mobile-broadband-provider-info.

    Several providers can share one network code (an MVNO rides its host network's code); the
    first one that publishes an MMS APN is used, which is normally the host network itself.
    """
    mcc, mnc = str(mcc or "").strip(), str(mnc or "").strip()
    if not mcc or not mnc:
        return None
    path = path or PROVIDER_DB
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    entry = _mms_provider_table(path, stamp).get((mcc, mnc.lstrip("0") or "0"))
    return dict(entry) if entry else None


@functools.lru_cache(maxsize=2)
def _mms_provider_table(path: str, _stamp: int) -> dict:
    """{(mcc, mnc): first MMS APN entry}, parsed once per database file version."""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return {}
    table: dict = {}
    for provider in root.iter("provider"):
        gsm = provider.find("gsm")
        if gsm is None:
            continue
        entry = None
        for apn in gsm.findall("apn"):
            usage = apn.find("usage")
            mmsc = (apn.findtext("mmsc") or "").strip()
            if usage is not None and usage.get("type") == "mms" and mmsc:
                entry = {"name": (provider.findtext("name") or "").strip(),
                         "apn": apn.get("value", ""), "mmsc": mmsc,
                         "proxy": (apn.findtext("mmsproxy") or "").strip(),
                         "username": (apn.findtext("username") or "").strip(),
                         "password": (apn.findtext("password") or "").strip()}
                break
        if entry is None:
            continue
        for network in gsm.findall("network-id"):
            key = (network.get("mcc", ""), network.get("mnc", "").lstrip("0") or "0")
            table.setdefault(key, entry)
    return table


def resolve_settings(inst: dict, *, provider_path: str | None = None) -> dict:
    """The line's effective MMS settings: its own values, gaps filled from the provider DB."""
    own = inst.get("mms") if isinstance(inst.get("mms"), dict) else {}
    detected = lookup_provider(inst.get("mcc"), inst.get("mnc"), provider_path) or {}
    settings = {"enabled": bool(own.get("enabled", True)),
                "auto_download": bool(own.get("auto_download", True)),
                "transport": own.get("transport") if own.get("transport") in TRANSPORTS
                else "auto",
                "user_agent": str(own.get("user_agent") or DEFAULT_USER_AGENT),
                "detected": detected or None}
    try:
        settings["max_size"] = max(30 * 1024, int(own.get("max_size") or DEFAULT_MAX_SIZE))
    except (TypeError, ValueError):
        settings["max_size"] = DEFAULT_MAX_SIZE
    manual = bool(str(own.get("mmsc") or "").strip())
    source = own if manual else detected
    for key in ("apn", "mmsc", "proxy", "username", "password"):
        settings[key] = str((source or {}).get(key) or "").strip()
    settings["source"] = "line" if manual else ("provider" if detected else "")
    settings["configured"] = bool(settings["mmsc"])
    return settings


def parse_proxy(value: str) -> tuple[str, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "://" in text:
        parts = urlsplit(text)
        host, port = parts.hostname or "", parts.port or 80
    else:
        host, _, port_text = text.rpartition(":") if ":" in text else (text, "", "80")
        try:
            port = int(port_text)
        except ValueError:
            return None
    if not host or not 0 < port < 65536:
        return None
    return host, port


# ----------------------------- HTTP framing -----------------------------

def build_request(method: str, url: str, *, body: bytes = b"", headers: dict | None = None,
                  via_proxy: bool) -> tuple[bytes, str, int]:
    """Serialise one HTTP/1.1 request. Returns (bytes, connect host, connect port)."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise MmsTransportError(f"unsupported MMSC URL scheme: {parts.scheme or url!r}",
                                retryable=False)
    port = parts.port or 80
    host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
    target = url if via_proxy else (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host_header}", "Connection: close"]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    if method != "GET" or body:
        lines.append(f"Content-Length: {len(body)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head + body, parts.hostname, port


def parse_response(raw: bytes) -> tuple[HttpResponse | None, bool]:
    """(response, complete). The response is None until the header block has arrived."""
    end = raw.find(b"\r\n\r\n")
    if end < 0:
        return None, False
    lines = raw[:end].decode("latin-1").split("\r\n")
    match = re.match(r"HTTP/\d\.\d\s+(\d{3})", lines[0])
    if not match:
        raise MmsTransportError("the MMSC sent a malformed HTTP response")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    body = raw[end + 4:]
    if "chunked" in headers.get("transfer-encoding", "").lower():
        decoded, pos = bytearray(), 0
        while True:
            line_end = body.find(b"\r\n", pos)
            if line_end < 0:
                return HttpResponse(int(match.group(1)), headers, bytes(decoded)), False
            try:
                size = int(body[pos:line_end].split(b";")[0], 16)
            except ValueError:
                raise MmsTransportError("the MMSC sent a malformed chunked body") from None
            if size == 0:
                return HttpResponse(int(match.group(1)), headers, bytes(decoded)), True
            start = line_end + 2
            if len(body) < start + size + 2:
                return HttpResponse(int(match.group(1)), headers, bytes(decoded)), False
            decoded += body[start:start + size]
            pos = start + size + 2
    length = headers.get("content-length")
    if length is not None and length.isdigit():
        complete = len(body) >= int(length)
        return HttpResponse(int(match.group(1)), headers, body[:int(length)]), complete
    # Neither framing: the body runs until the connection closes.
    return HttpResponse(int(match.group(1)), headers, body), False


# ----------------------------- clients -----------------------------

class HostHttp:
    """MMSC HTTP from the host's own network stack."""

    name = "host"

    def __init__(self, settings: dict, session=None):
        import requests  # deferred: only this client needs it
        self.settings = settings
        self.session = session or requests.Session()

    def request(self, method: str, url: str, *, body: bytes = b"", headers: dict | None = None,
                timeout: float = 60.0) -> HttpResponse:
        proxy = parse_proxy(self.settings.get("proxy"))
        proxies = {"http": f"http://{proxy[0]}:{proxy[1]}"} if proxy else None
        try:
            response = self.session.request(method, url, data=body or None,
                                            headers=headers or {}, proxies=proxies,
                                            timeout=timeout, allow_redirects=False)
        except Exception as exc:
            after_send = type(exc).__name__ in ("ReadTimeout", "ChunkedEncodingError")
            raise MmsTransportError(f"MMSC request from the host failed: {exc}",
                                    after_send=after_send) from None
        return HttpResponse(response.status_code,
                            {k.lower(): v for k, v in response.headers.items()},
                            response.content)


class ModemCommand:
    """One AT command through ModemManager's Modem.Command D-Bus method.

    Usable with the modem shared, but slow for uploads: see send_chunk().
    """

    name = "modemmanager"
    CHUNK = 256              # the longest AT+QISENDEX payload the module accepted
    # Each chunk costs a two-second command timeout; a WAP proxy drops a request that
    # trickles in for minutes, so only small requests (retrievals, acknowledgements) fit.
    UPLOAD_LIMIT = 4 * 1024
    DATAFORMAT = "1,1"       # hex both ways: a D-Bus string cannot carry arbitrary bytes
    VERIFY_EACH_CHUNK = True

    def send_chunk(self, sid: int, chunk: bytes) -> None:
        # Answered with "SEND OK", which ModemManager does not treat as a final result: the
        # call times out after its one-second minimum. ModemManager declares the whole modem
        # invalid after ten consecutive timeouts on a port -- a long upload did exactly
        # that -- so the caller follows every chunk with a query that completes normally,
        # which resets that count and confirms the bytes were taken.
        self(f'AT+QISENDEX={sid},"{chunk.hex()}"', 1)

    def close(self) -> None:
        pass

    def __init__(self, modem_path: str, runner=subprocess.run):
        self.modem_path = modem_path
        self.runner = runner

    def __call__(self, command: str, timeout: int = 5) -> tuple[bool, str]:
        args = ["busctl", "--system", "--json=short", f"--timeout={int(timeout) + 5}", "call",
                "org.freedesktop.ModemManager1", self.modem_path,
                "org.freedesktop.ModemManager1.Modem", "Command", "su", command,
                str(max(1, int(timeout)))]
        try:
            result = self.runner(args, capture_output=True, text=True,
                                 timeout=int(timeout) + 10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if result.returncode:
            return False, " ".join(str(result.stderr or result.stdout or "").split())
        try:
            data = json.loads(result.stdout or "{}").get("data") or [""]
            return True, str(data[0])
        except (ValueError, AttributeError, IndexError):
            return False, "unreadable ModemManager reply"


_CGDCONT_RE = re.compile(r'\+CGDCONT:\s*(\d+)\s*,\s*"[^"]*"\s*,\s*"([^"]*)"')
_QIACT_RE = re.compile(r'\+QIACT:\s*(\d+)\s*,\s*(\d)')
_QISTATE_RE = re.compile(r'\+QISTATE:\s*(\d+)\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*\d+\s*,'
                         r'\s*\d+\s*,\s*(\d)')
_QIOPEN_RE = re.compile(r'\+QIOPEN:\s*(\d+)\s*,\s*(\d+)')
_QIRD_RE = re.compile(r'\+QIRD:\s*(\d+)\s*(?:\r?\n)?([0-9A-Fa-f]*)')
_QISEND_RE = re.compile(r'\+QISEND:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)')


class SerialAtChannel:
    """AT commands on a modem port the gateway owns (ModemManager told to ignore it).

    With the port to itself the gateway can use the module's prompt-driven binary send
    (AT+QISEND=<id>,<len>, ">" then raw bytes, "SEND OK"): over 100 KB/s on a Quectel module,
    against ~100 B/s through ModemManager.
    """

    name = "serial"
    CHUNK = 1460             # AT+QISEND's maximum length per command
    UPLOAD_LIMIT = None
    DATAFORMAT = "0,1"       # raw send, hex receive: received bytes can never fake an "OK"
    VERIFY_EACH_CHUNK = False
    _FINAL = re.compile(r"(?:^|\r\n)(OK|ERROR|\+CME ERROR:[^\r]*|\+CMS ERROR:[^\r]*)\r\n")

    def __init__(self, port: str, *, serial_factory=None, clock=time.monotonic):
        self.port = port
        self.clock = clock
        self._factory = serial_factory
        self._serial = None

    def _open(self):
        if self._serial is None:
            if self._factory is None:
                import serial  # pyserial; deferred so importing this module never needs it
                self._serial = serial.Serial(self.port, 115200, timeout=0.01, exclusive=True)
            else:
                self._serial = self._factory(self.port)
            self._exchange("ATE0", 3)
        return self._serial

    def _read_until(self, predicate, timeout: float) -> bytes:
        port = self._serial
        buffer = b""
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            buffer += port.read(4096)
            if predicate(buffer):
                break
        return buffer

    def _exchange(self, command: str, timeout: float) -> tuple[bool, str]:
        port = self._serial
        port.reset_input_buffer()
        port.write(command.encode("ascii") + b"\r")
        raw = self._read_until(lambda b: self._FINAL.search(b.decode("latin-1")), timeout)
        text = raw.decode("latin-1")
        match = self._FINAL.search(text)
        if not match:
            return False, "timed out"
        body = text[:match.start(1)].strip()
        return match.group(1) == "OK", body if match.group(1) == "OK" else match.group(1)

    def __call__(self, command: str, timeout: int = 5) -> tuple[bool, str]:
        try:
            self._open()
            return self._exchange(command, timeout)
        except Exception as exc:  # noqa: BLE001 -- a vanished port is an ordinary failure
            self.close()
            return False, f"{self.port}: {exc}"

    def send_chunk(self, sid: int, chunk: bytes) -> None:
        try:
            port = self._open()
            port.reset_input_buffer()
            port.write(f"AT+QISEND={sid},{len(chunk)}\r".encode("ascii"))
            prompt = self._read_until(lambda b: b">" in b or b"ERROR" in b, 5)
            if b">" not in prompt:
                raise MmsTransportError("the modem refused to send on the MMSC connection")
            port.write(chunk)
            result = self._read_until(lambda b: b"SEND OK" in b or b"SEND FAIL" in b
                                      or b"ERROR" in b, 20)
        except MmsTransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise MmsTransportError(f"{self.port}: {exc}") from None
        if b"SEND OK" not in result:
            said = " ".join(result.decode("latin-1").split())[:80] or "nothing"
            raise MmsTransportError("the modem could not send on the MMSC connection; "
                                    f"it may have closed (modem answered: {said})")

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
            self._serial = None


AT_PORT_ENV = "MDD_MMS_AT_PORT"


def find_at_port(modem_path: str, runner=subprocess.run, environ=os.environ) -> str | None:
    """An AT port of this modem that ModemManager ignores, i.e. one the gateway may own.

    Set by a udev rule (ID_MM_PORT_IGNORE on the module's spare AT interface); the port must
    still carry an AT port type so the diagnostics port, which is ignored too, is never used.
    MDD_MMS_AT_PORT names the device explicitly instead.
    """
    explicit = str(environ.get(AT_PORT_ENV) or "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else None
    try:
        result = runner(["mmcli", "-m", modem_path, "--output-json"], capture_output=True,
                        text=True, timeout=10, check=False)
        ports = (json.loads(result.stdout or "{}").get("modem", {}).get("generic", {})
                 .get("ports") or []) if not result.returncode else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for entry in ports:
        name, _, kind = str(entry).partition(" ")
        if kind.strip() != "(ignored)" or not name.startswith("tty"):
            continue
        try:
            udev = runner(["udevadm", "info", "-q", "property", "-n", f"/dev/{name}"],
                          capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        properties = str(udev.stdout or "")
        if re.search(r"^ID_MM_PORT_TYPE_AT_(PRIMARY|SECONDARY)=1$", properties, re.M):
            return f"/dev/{name}"
    return None


class ModemSocketHttp:
    """MMSC HTTP through the modem's own PDP context on the MMS APN (Quectel QI* commands)."""

    name = "modem"
    CONNECT_ID = 11          # the highest socket id, well away from anything else using QI*
    # The request deadline scales with the upload (two seconds a chunk), so it can be an hour.
    # A connection that has not opened in this long will not open, and an MMS send retries
    # a failed connection, so waiting the whole deadline each time held the modem ~12 minutes.
    CONNECT_TIMEOUT = 30.0
    READ = 1500

    def __init__(self, command: ModemCommand, settings: dict, *, sleep=time.sleep,
                 clock=time.monotonic):
        self.at = command
        self.settings = settings
        self.sleep = sleep
        self.clock = clock

    def _require(self, command: str, timeout: int = 5) -> str:
        ok, text = self.at(command, timeout)
        if not ok:
            raise MmsTransportError(f"modem rejected {command.split('=')[0]}: {text[:120]}")
        return text

    def supported(self) -> bool:
        ok, text = self.at("AT+QICSGP=?", 3)
        return ok and "+QICSGP" in text

    def _context(self) -> tuple[int, bool]:
        """(cid, activated_here) for a PDP context on the MMS APN, activating it if needed."""
        apn = self.settings.get("apn") or ""
        if not apn:
            raise MmsTransportError("no MMS APN is configured for this line", retryable=False)
        defined = {int(cid): name for cid, name in _CGDCONT_RE.findall(
            self._require("AT+CGDCONT?"))}
        cid = next((c for c, name in sorted(defined.items())
                    if name.casefold() == apn.casefold()), None)
        if cid is None:
            cid = next((c for c in range(4, 16) if c not in defined), None)
            if cid is None:
                raise MmsTransportError("the modem has no free PDP context for the MMS APN",
                                        retryable=False)
            user, password = self.settings.get("username", ""), self.settings.get("password", "")
            auth = 3 if user or password else 0
            self._require(f'AT+QICSGP={cid},1,"{apn}","{user}","{password}",{auth}')
        active = {int(c): state == "1" for c, state in _QIACT_RE.findall(
            self._require("AT+QIACT?"))}
        if active.get(cid):
            return cid, False
        ok, text = self.at(f"AT+QIACT={cid}", 60)
        if not ok:
            raise MmsTransportError(f"could not activate the MMS APN: {text[:120]}")
        return cid, True

    def request(self, method: str, url: str, *, body: bytes = b"", headers: dict | None = None,
                timeout: float = 120.0) -> HttpResponse:
        if not self.supported():
            raise MmsTransportError("this modem has no embedded TCP/IP stack that the gateway "
                                    "can drive (Quectel AT+QICSGP)", retryable=False)
        proxy = parse_proxy(self.settings.get("proxy"))
        payload, host, port = build_request(method, url, body=body, headers=headers,
                                            via_proxy=bool(proxy))
        limit = self.at.UPLOAD_LIMIT
        if limit is not None and len(payload) > limit:
            raise MmsTransportError(
                f"a {len(payload) // 1024} KB request is too large for ModemManager's command "
                f"channel (at most {limit // 1024} KB); sending MMS over the modem needs an AT "
                "port the gateway owns -- see the MMS section of TROUBLESHOOTING",
                retryable=False)
        if proxy:
            host, port = proxy
        deadline = self.clock() + timeout
        try:
            cid, activated = self._context()
        except MmsTransportError as exc:
            exc.unsent = True
            raise
        sid = self.CONNECT_ID
        try:
            self.at(f"AT+QICLOSE={sid},1", 3)
            try:
                self._require(f'AT+QICFG="dataformat",{self.at.DATAFORMAT}')
                self._require(f'AT+QIOPEN={cid},{sid},"TCP","{host}",{port},0,0', 10)
                self._wait_connected(
                    sid, min(deadline, self.clock() + self.CONNECT_TIMEOUT))
            except MmsTransportError as exc:
                exc.unsent = True
                raise
            self._send(sid, payload, deadline)
            try:
                return self._receive(sid, deadline)
            except MmsTransportError as exc:
                exc.after_send = True
                raise
        finally:
            self.at(f"AT+QICLOSE={sid},1", 3)
            if activated:
                self.at(f"AT+QIDEACT={cid}", 40)
            self.at.close()

    def _wait_connected(self, sid: int, deadline: float) -> None:
        while True:
            ok, text = self.at(f"AT+QISTATE=1,{sid}", 3)
            for opened, error in _QIOPEN_RE.findall(text):
                if int(opened) == sid and int(error):
                    raise MmsTransportError(f"MMSC connection failed (error {error})")
            states = {int(s): int(state) for s, state in _QISTATE_RE.findall(text)}
            if states.get(sid) == 2:
                return
            if self.clock() > deadline:
                raise MmsTransportError("timed out connecting to the MMSC")
            self.sleep(0.5)

    def _sent(self, sid: int) -> int:
        ok, text = self.at(f"AT+QISEND={sid},0", 3)
        match = _QISEND_RE.search(text) if ok else None
        return int(match.group(1)) if match else -1

    def _send(self, sid: int, payload: bytes, deadline: float) -> None:
        chunk_size = self.at.CHUNK
        for offset in range(0, len(payload), chunk_size):
            chunk = payload[offset:offset + chunk_size]
            # Up to the last chunk, the request the MMSC has seen is incomplete whatever went
            # wrong. The last chunk may have left the modem even when it reports a failure.
            last = offset + len(chunk) >= len(payload)
            try:
                self.at.send_chunk(sid, chunk)
            except MmsTransportError as exc:
                raise MmsTransportError(f"{exc}, at byte {offset} of {len(payload)}",
                                        retryable=exc.retryable, unsent=not last) from None
            if self.at.VERIFY_EACH_CHUNK:
                sent = self._sent(sid)
                if sent != offset + len(chunk):
                    raise MmsTransportError(
                        f"the modem sent {max(sent, 0)} of {len(payload)} request bytes; "
                        "the MMSC connection may have closed", unsent=not last)
            if self.clock() > deadline:
                raise MmsTransportError("timed out sending to the MMSC", unsent=not last)
        if not self.at.VERIFY_EACH_CHUNK:
            sent = self._sent(sid)
            if sent != len(payload):
                raise MmsTransportError(
                    f"the modem sent {max(sent, 0)} of {len(payload)} request bytes")

    def _receive(self, sid: int, deadline: float) -> HttpResponse:
        raw = bytearray()
        idle = 0
        while True:
            ok, text = self.at(f"AT+QIRD={sid},{self.READ}", 5)
            match = _QIRD_RE.search(text) if ok else None
            count = int(match.group(1)) if match else 0
            if count:
                raw += bytes.fromhex(match.group(2)[:count * 2])
                idle = 0
                response, complete = parse_response(bytes(raw))
                if complete:
                    return response
                continue
            ok, state_text = self.at(f"AT+QISTATE=1,{sid}", 3)
            states = {int(s): int(state) for s, state in _QISTATE_RE.findall(state_text)}
            closed = states.get(sid) in (None, 4) or '"closed"' in state_text or \
                '"closed"' in text
            if closed and idle:
                response, _complete = parse_response(bytes(raw))
                if response is None:
                    raise MmsTransportError("the MMSC closed the connection without a reply")
                return response
            if self.clock() > deadline:
                raise MmsTransportError("timed out waiting for the MMSC")
            idle += 1
            self.sleep(0.3 if closed else 0.5)


def client_for(settings: dict, modem_path: str | None, *, runner=subprocess.run):
    """The client a line's MMS goes through: the modem holding its SIM where it can, else
    the host. An explicit transport choice is honoured even when it cannot work, so the
    failure is reported instead of silently taking another network."""
    transport = settings.get("transport") or "auto"
    if transport == "host":
        return HostHttp(settings)
    if modem_path:
        port = find_at_port(modem_path, runner)
        channel = SerialAtChannel(port) if port else ModemCommand(modem_path, runner)
        client = ModemSocketHttp(channel, settings)
        if transport == "modem" or client.supported():
            return client
        channel.close()
    if transport == "modem":
        raise MmsTransportError("no modem holds this line's SIM", retryable=False)
    return HostHttp(settings)
