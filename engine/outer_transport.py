"""Explicit SOCKS5 UDP transport for the isolated country service.

There is no direct fallback. SWu uses one association for IKE/500 and another for
NAT-T/4500; proxy mode never opens a raw ESP socket.
"""
from dataclasses import dataclass
import ipaddress
import socket
from urllib.parse import unquote, urlsplit

import socks


@dataclass(frozen=True, repr=False)
class ProxyEndpoint:
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, value):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "socks5" or not parsed.hostname or parsed.port is None
                    or not 0 < parsed.port < 65536 or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise ValueError()
            return cls(parsed.hostname, parsed.port,
                       unquote(parsed.username) if parsed.username is not None else None,
                       unquote(parsed.password) if parsed.password is not None else None)
        except (TypeError, ValueError):
            # URLs may contain credentials: never echo the input or parser exception.
            raise ValueError("expected socks5://host:port with optional credentials") from None


def proxy_udp_socket(proxy, target, *, source=("0.0.0.0", 0), timeout=5):
    """Create a connected, select/fork-compatible UDP socket through one relay.

    The caller resolves the ePDG to IPv4 just as existing SWu does. PySocks keeps
    the SOCKS TCP association open and filters replies to this peer. The socket
    must be recreated when the relay restarts; reconnect never chooses direct.
    """
    endpoint = ProxyEndpoint.parse(proxy)
    address, port = target
    ipaddress.IPv4Address(address)
    if not 0 < int(port) < 65536 or timeout <= 0:
        raise ValueError("invalid UDP destination or timeout")
    udp = socks.socksocket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.set_proxy(socks.SOCKS5, endpoint.host, endpoint.port, rdns=False,
                      username=endpoint.username, password=endpoint.password)
        udp.settimeout(timeout)
        udp.bind(source)
        udp.connect((address, int(port)))
        return udp
    except BaseException:
        udp.close()
        raise
