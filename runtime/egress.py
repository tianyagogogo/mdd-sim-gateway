#!/usr/bin/env python3
"""Experimental container-only country SOCKS service; never owns host routes.

Run as `python -m runtime.egress --data /data`. Consumers must opt into the
explicit SOCKS transport. Do not use its status as host-routing readiness.
"""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from host.mdd_orchestrator import Orchestrator, atomic_json, read_json
from control.app.egress_contract import VERSION, proxy_fingerprint


class ListenAddressError(RuntimeError):
    """The isolated Engine-network listener address could not be selected safely."""


class SocksEgress(Orchestrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Separate contract: existing Control must never mistake SOCKS readiness
        # for installed ePDG routes and accidentally start a direct Engine.
        self.status_path = self.root / "socks-egress-status.json"
        self._listen_address = ""


    def build_proxy_config(self, proxy):
        config, states = super().build_proxy_config(proxy)
        # Host TUN mode binds proxy sockets to the detected uplink to avoid loops.
        # SOCKS-only containers have no TUN route loop; SO_BINDTODEVICE would also
        # require CAP_NET_RAW on older NAS kernels. Use normal socket routing.
        config.get("route", {}).pop("auto_detect_interface", None)
        config["inbounds"] = [item for item in config["inbounds"] if item["type"] != "tun"]
        for inbound in config["inbounds"]:
            if inbound["type"] != "socks":
                raise ValueError("isolated egress supports only SOCKS inbounds")
            inbound["listen"] = self.listen_address()
        for rule in config.get("route", {}).get("rules", []):
            if "inbound" in rule:
                rule["inbound"] = [tag for tag in rule["inbound"] if not tag.startswith("tun-")]
        for state in states.values():
            state.pop("interface", None)
            if state.get("mode") != "direct":
                state["proxy_host"] = os.environ.get("MDD_EGRESS_HOST", "mdd-egress")
        return config, states

    def listen_address(self):
        """The internal Engine-network address this service binds SOCKS to.

        Resolved once and kept: a container's own addresses are fixed for its lifetime,
        while this used to run once per enabled country on every pass — each one a DNS
        round trip to the Docker resolver.
        """
        if self._listen_address:
            return self._listen_address
        configured = os.environ.get("MDD_EGRESS_LISTEN", "").strip()
        if configured:
            self._listen_address = configured
            return configured
        # The service has an uplink and an internal Engine interface. Resolve the Compose
        # alias which exists only on the internal network so SOCKS is not exposed on uplink.
        host = os.environ.get("MDD_EGRESS_HOST", "mdd-egress")
        answers = socket.getaddrinfo(host, 0, socket.AF_INET, socket.SOCK_STREAM)
        addresses = {item[4][0] for item in answers}
        if len(addresses) != 1:
            raise ListenAddressError(
                f"Engine network alias resolved to {len(addresses)} IPv4 addresses")
        self._listen_address = addresses.pop()
        return self._listen_address

    def apply_routes(self, wanted):
        raise RuntimeError("SOCKS egress must never install network routes")

    def reconcile_socks(self, desired):
        proxy = desired.get("proxy") or {}
        contract = {"version": VERSION, "config_fingerprint": proxy_fingerprint(proxy)}
        states = {}
        try:
            if not proxy.get("enabled"):
                self.stop_proxy()
                atomic_json(self.status_path, {**contract, "transport": "socks5", "enabled": False,
                                              "updated_at": int(time.time()), "exits": {}})
                return
            config, states = self.build_proxy_config(proxy)
            if config["inbounds"]:
                self.apply_xray(self.next_xray_config)
                self.apply_singbox(config)
                self.process_reselect_requests(states)
                self.process_stalled_reports(states)
                self.update_selected_nodes(states)
            else:
                self.stop_proxy()
            for state in states.values():
                # Base configuration errors can embed upstream URLs/passwords.
                if state.get("error"):
                    state["error"] = "country proxy configuration rejected"
                # Process readiness is not end-to-end UDP/carrier health.
                state["transport"] = "direct" if state.get("mode") == "direct" else "socks5"
            atomic_json(self.status_path, {**contract, "transport": "socks5", "enabled": True,
                                          "updated_at": int(time.time()), "exits": states})
        except Exception as exc:
            # A rejected config must not leave a stale listener serving a previously
            # selected country. Close listeners until a valid desired state arrives.
            self.stop_proxy()
            atomic_json(self.status_path, {**contract, "transport": "socks5", "enabled": True,
                                          "updated_at": int(time.time()), "exits": {},
                                          "error_type": type(exc).__name__,
                                          "error_code": ("listen_address_unavailable"
                                                         if isinstance(exc, ListenAddressError)
                                                         else "configuration_rejected")})

    def stop_proxy(self):
        for attr in ("singbox", "xray"):
            process = getattr(self, attr)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            setattr(self, attr, None)
        self.last_proxy_fingerprint = self.last_xray_fingerprint = ""

    def loop(self):
        self.root.mkdir(parents=True, exist_ok=True)
        while not self.stop:
            self.reconcile_socks(read_json(self.desired_path))
            deadline = time.monotonic() + self.interval
            while not self.stop and time.monotonic() < deadline:
                time.sleep(0.1)

    def close(self):
        self.stop_proxy()
        atomic_json(self.status_path, {"transport": "socks5", "enabled": False,
                                      "updated_at": int(time.time()), "exits": {}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    app = SocksEgress(args.data.resolve(), Path(__file__).resolve().parents[1],
                      interval=max(0.2, args.interval))
    def stop(*_):
        app.stop = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        app.loop()
    finally:
        app.close()


if __name__ == "__main__":
    main()
