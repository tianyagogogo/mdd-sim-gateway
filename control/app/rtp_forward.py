"""Publish the browser-call RTP ports of lines behind a SOCKS exit on the container stack.

On the container stack every Engine sits on the internal Engine network. A line that goes
direct also joins the uplink network, and Docker publishes its RTP ports there. A line behind
a country exit (SOCKS) joins nothing else, on purpose: nothing it sends may leave except
through its exit. Docker publishes no port of a container that is only on internal networks,
so the browser softphone's media never reached those lines and calls had no audio.

This module runs one small forwarder container, from the Control image, on the default bridge
and the Engine network. It publishes each such line's RTP range and, per browser address,
relays UDP between that port on the host and the same port on the line's Engine. The Engine
gains no route and no network: all it can do is answer the forwarder, and the forwarder sends
an answer only back to the browser address that opened that flow.

Relay media mode (media.py) needs none of this; a line in relay mode carries no forward label.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import time

log = logging.getLogger("mdd.rtp_forward")

CONTAINER = "mdd-sim-gateway-rtp-forward"
MANAGED_LABEL = "io.mdd-sim-gateway.managed"
COMPONENT_LABEL = "io.mdd-sim-gateway.component"
CONFIG_LABEL = "io.mdd-sim-gateway.rtp-forward-config"
# The ranges a running forwarder publishes, so a stopped line keeps its range across passes.
MAP_LABEL = "io.mdd-sim-gateway.rtp-forward-map"
# Carried by an Engine whose RTP range this forwarder publishes: "<first>-<last>".
FORWARD_LABEL = "io.mdd-sim-gateway.rtp-forward"
ENV_MAP = "MDD_RTP_FORWARD"

# A flow is one browser address on one port. Browsers use one per call leg; the cap only
# keeps a stray sender from making the forwarder open sockets without end.
FLOWS_PER_PORT = 8
FLOW_IDLE_SECONDS = 120.0


def label_value(first: int, last: int) -> str:
    return f"{int(first)}-{int(last)}"


def _parse_range(value: str) -> tuple[int, int] | None:
    try:
        first, last = (int(part) for part in str(value).split("-", 1))
    except (TypeError, ValueError):
        return None
    if not 1024 <= first <= last <= 65535 or last - first > 1000:
        return None
    return first, last


def desired_map(containers) -> list[dict]:
    """The ranges the Engines carrying the forward label ask for, sorted, overlaps dropped.

    Keyed by container name rather than address: the forwarder resolves it on the Engine
    network per flow, so rebuilding a line (new address, same name) needs no new forwarder.
    """
    entries = []
    for container in containers:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true":
            continue
        span = _parse_range(labels.get(FORWARD_LABEL, ""))
        if span:
            entries.append({"target": container.name, "first": span[0], "last": span[1]})
    return desired_map_from_entries(entries)


def fingerprint(mapping: list[dict], image_id: str, network: str) -> str:
    text = json.dumps({"map": mapping, "image": image_id, "network": network}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _own_image(client):
    """The image this Control runs, so the forwarder needs nothing fetched."""
    name = os.environ.get("MDD_CONTROL_CONTAINER", "mdd-sim-gateway-control")
    return client.containers.get(name).image


def _current(client):
    import docker
    try:
        return client.containers.get(CONTAINER)
    except docker.errors.NotFound:
        return None


def remove(client) -> None:
    current = _current(client)
    if current is None:
        return
    labels = (current.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get(MANAGED_LABEL) != "true":
        raise RuntimeError(f"refusing to remove foreign container {CONTAINER}")
    current.remove(force=True, v=True)
    log.info("removed %s: no line behind an exit needs it", CONTAINER)


def _current_map(container) -> list[dict]:
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    try:
        value = json.loads(labels.get(MAP_LABEL) or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def plan(client, configured: set[str], exclude: str = "") -> list[dict]:
    """What the forwarder should publish.

    A running Engine with the forward label is always in. A line whose Engine is gone (the
    health policy removed it and will rebuild it) keeps its range while it is still
    configured, so that rebuild does not replace the forwarder and drop the calls on every
    other line. A range goes when its line is deleted, or when its Engine is back without the
    label (it now goes direct, or uses the relay) -- and ``exclude`` drops one ahead of that,
    so its Engine can publish the same ports itself."""
    engines = client.containers.list(
        all=True, filters={"label": f"{COMPONENT_LABEL}=engine"})
    present = {container.name for container in engines}
    entries = [entry for entry in desired_map(engines) if entry["target"] != exclude]
    current = _current(client)
    if current is not None:
        for entry in _current_map(current):
            target = str(entry.get("target") or "")
            if target in configured and target != exclude and target not in present:
                entries.append(entry)
    return desired_map_from_entries(entries)


def desired_map_from_entries(entries: list[dict]) -> list[dict]:
    clean = []
    for entry in entries:
        span = _parse_range(label_value(entry.get("first", 0), entry.get("last", 0)))
        if span and entry.get("target"):
            clean.append({"target": str(entry["target"]), "first": span[0], "last": span[1]})
    clean.sort(key=lambda entry: (entry["first"], entry["target"]))
    kept, taken = [], set()
    for entry in clean:
        ports = set(range(entry["first"], entry["last"] + 1))
        if ports & taken:
            log.warning("RTP ports of %s overlap another line; not forwarded", entry["target"])
            continue
        taken |= ports
        kept.append(entry)
    return kept


def reconcile(client, engine_network: str, configured: set[str], exclude: str = "") -> None:
    """Make the forwarder match plan(). It is replaced only when the published ranges change
    (a line behind an exit added, deleted, or moved off the exit), which interrupts calls on
    the other such lines; rebuilding a line keeps its name and range and changes nothing."""
    mapping = plan(client, configured, exclude)
    if not mapping:
        remove(client)
        return
    image = _own_image(client)
    wanted = fingerprint(mapping, image.id, engine_network)
    current = _current(client)
    if current is not None:
        labels = (current.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true":
            raise RuntimeError(f"refusing to replace foreign container {CONTAINER}")
        if labels.get(CONFIG_LABEL) == wanted:
            if current.status != "running":
                current.start()
            return
        current.remove(force=True, v=True)
    ports = {f"{port}/udp": port
             for entry in mapping for port in range(entry["first"], entry["last"] + 1)}
    container = client.containers.create(
        image.id,
        name=CONTAINER,
        entrypoint=["python", "-m", "app.rtp_forward"],
        working_dir="/app/control",
        environment={ENV_MAP: json.dumps(mapping), "PYTHONUNBUFFERED": "1"},
        # The published ports live on the default bridge; the Engine network is internal.
        network="bridge",
        ports=ports,
        user="65534:65534",
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        read_only=True,
        pids_limit=16,
        mem_limit="64m",
        # The Control image's health check probes its web port, which this does not serve.
        healthcheck={"test": ["NONE"]},
        restart_policy={"Name": "unless-stopped"},
        labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "rtp-forward", CONFIG_LABEL: wanted,
                MAP_LABEL: json.dumps(mapping, sort_keys=True)},
        log_config={"Type": "json-file", "Config": {"max-size": "1m", "max-file": "2"}},
    )
    try:
        client.networks.get(engine_network).connect(container)
        container.start()
    except Exception:
        container.remove(force=True, v=True)
        raise
    log.info("started %s for %s", CONTAINER,
             ", ".join(f"{e['target']} {e['first']}-{e['last']}" for e in mapping))


# ------------------------------------------------------------------ the forwarder itself
class _Outside(asyncio.DatagramProtocol):
    """One published port. Each browser address gets its own socket towards the Engine, so
    the Engine sees a distinct source per flow and answers each on its own."""

    def __init__(self, port: int, target: str):
        self.port, self.target = port, target
        self.transport = None
        self.flows: dict = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        flow = self.flows.get(addr)
        if flow is None:
            if len(self.flows) >= FLOWS_PER_PORT:
                return
            flow = self.flows[addr] = _Flow(self, addr)
            asyncio.get_running_loop().create_task(flow.open())
        flow.send(data)


class _Flow(asyncio.DatagramProtocol):
    def __init__(self, outside: _Outside, peer):
        self.outside, self.peer = outside, peer
        self.transport = None
        self.pending: list[bytes] = []
        self.last = time.monotonic()

    async def open(self):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(self.outside.target, self.outside.port,
                                           family=socket.AF_INET, type=socket.SOCK_DGRAM)
            await loop.create_datagram_endpoint(lambda: self, remote_addr=infos[0][4])
        except OSError as exc:
            # The line is being rebuilt, or is stopped: drop this flow; the next packet
            # from the browser opens a new one.
            log.debug("%s:%s unreachable: %s", self.outside.target, self.outside.port, exc)
            self.outside.flows.pop(self.peer, None)

    def connection_made(self, transport):
        self.transport = transport
        for data in self.pending:
            transport.sendto(data)
        self.pending.clear()

    def send(self, data):
        self.last = time.monotonic()
        if self.transport is None:
            if len(self.pending) < 16:
                self.pending.append(data)
        else:
            self.transport.sendto(data)

    def datagram_received(self, data, _addr):
        # Only ever back to the browser address that opened this flow.
        self.last = time.monotonic()
        if self.outside.transport is not None:
            self.outside.transport.sendto(data, self.peer)

    def error_received(self, exc):
        log.debug("flow %s -> %s:%s: %s", self.peer, self.outside.target, self.outside.port, exc)

    def close(self):
        if self.transport is not None:
            self.transport.close()


async def serve(mapping: list[dict], host: str = "0.0.0.0") -> None:
    loop = asyncio.get_running_loop()
    listeners = []
    try:
        for entry in mapping:
            for port in range(int(entry["first"]), int(entry["last"]) + 1):
                _transport, protocol = await loop.create_datagram_endpoint(
                    lambda port=port, target=entry["target"]: _Outside(port, target),
                    local_addr=(host, port))
                listeners.append(protocol)
        log.info("forwarding %d ports", len(listeners))
        while True:
            await asyncio.sleep(FLOW_IDLE_SECONDS / 4)
            now = time.monotonic()
            for listener in listeners:
                for peer, flow in list(listener.flows.items()):
                    if now - flow.last > FLOW_IDLE_SECONDS:
                        flow.close()
                        del listener.flows[peer]
    finally:
        for listener in listeners:
            for flow in listener.flows.values():
                flow.close()
            if listener.transport is not None:
                listener.transport.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        mapping = json.loads(os.environ[ENV_MAP])
    except (KeyError, ValueError):
        print(f"{ENV_MAP} is missing or invalid", file=sys.stderr)
        return 2
    asyncio.run(serve(mapping))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
