"""Shared, dependency-free contract for container egress readiness."""
import hashlib
import json
import math
import re

VERSION = 1
MAX_AGE = 15
ENGINE_LABEL = "io.mdd-sim-gateway.egress-transports"


def proxy_fingerprint(proxy):
    payload = json.dumps(proxy, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def current_status(state, proxy, now):
    if not isinstance(state, dict):
        return False
    stamp = state.get("updated_at")
    return (state.get("version") == VERSION and state.get("transport") == "socks5"
            and state.get("enabled") is True and not state.get("error_type")
            and state.get("config_fingerprint") == proxy_fingerprint(proxy)
            and type(stamp) in (int, float) and math.isfinite(stamp)
            and -5 <= now - stamp <= MAX_AGE)


def socks_endpoint(state):
    """Accept only an uncredentialed internal IPv4/DNS endpoint, never a URL."""
    host, port = state.get("proxy_host"), state.get("proxy_port")
    if (not isinstance(host, str) or len(host) > 253
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("invalid internal SOCKS endpoint")
    return f"socks5://{host}:{port}"
