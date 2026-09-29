"""How call media reaches a line's Asterisk from the browser softphone and native clients.

Two modes, each with its own code path:

``direct`` (the default)
    Every line publishes its own RTP range on the host and Asterisk rewrites its ICE host
    candidate to the host's LAN address (engine/templates/rtp.conf.j2). Nothing in this module
    runs, and the provisioning below hands clients no ICE servers.

``relay``
    No engine publishes a port. One TURN relay (coturn) publishes a single port, and it reaches
    the engines only over the media network, an internal Docker network holding the engines and
    the relay::

        client --TURN (one port, UDP or TCP)--> relay --media network--> engine RTP

    Every engine uses the same fixed RTP range (``RTP_PORTS``) on its own media address, so
    nothing depends on how many lines exist. Two layers keep the relay to call media:

    * coturn relays UDP only (``no-tcp-relay``) and only to addresses on the media network,
      the host's gateway address excluded;
    * each engine drops everything arriving on its media interface except UDP to
      ``RTP_PORTS`` (engine/render.py writes the ruleset, the entrypoint loads it). The engine
      already holds NET_ADMIN; the relay, which faces the internet, holds no capability at all.
      With nftables only packets for the browser leg's sockets get in; on a kernel without it
      the iptables-legacy fallback goes by the port range alone (see IPTABLES_LEGACY).

The mode is switched by ``python -m app.media`` (install.sh ``media`` for a host install,
``docker exec`` for the container stack). Enabling prepares and verifies everything before it
records the mode, and cleans up after itself on any failure. The control plane then rebuilds
the running lines one at a time (main.media_converge), since port publishing is fixed when a
container is created.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time

import docker

from . import config as cfg
from .version import VERSION

log = logging.getLogger("mdd.media")

DIRECT = "direct"
RELAY = "relay"
MODES = (DIRECT, RELAY)

NETWORK = os.environ.get("MDD_MEDIA_NETWORK", "mdd-sim-gateway-media")
CONTAINER = "mdd-sim-gateway-relay"
MANAGED_LABEL = "io.mdd-sim-gateway.managed"
COMPONENT_LABEL = "io.mdd-sim-gateway.component"
CONFIG_LABEL = "io.mdd-sim-gateway.relay-config"
# Carried by an engine container started in relay mode. An engine without it runs in direct
# mode, which keeps every container created before this existed where it is. RELAY_PENDING marks
# one started in relay mode without the media network (it could not be prepared): it matches
# neither mode, so the control plane rebuilds it once the network can be had.
MODE_LABEL = "io.mdd-sim-gateway.media-mode"
RELAY_PENDING = "relay-pending"

# The relay is the unmodified upstream coturn image, pinned by its multi-arch index digest so a
# re-tagged upstream cannot change what runs. Releases ship this exact image as an asset and in
# their registry under LOCAL_IMAGE's name (.github/workflows/release.yml reads it from here).
UPSTREAM_IMAGE = ("coturn/coturn:4.17.2-alpine@sha256:"
                  "771a95d04cb97bbc5bfc672e5fdf455591c7d2b2a15f02bb9ceda3e27561695f")
LOCAL_IMAGE = "mdd-sim-gateway/relay"

DEFAULT_PORT = 8478
LISTEN_PORT = 3478                  # inside the relay container
# Every engine's Asterisk RTP pool in relay mode. Engines have their own addresses on the media
# network, so the range is shared rather than staggered per line. One call uses one port.
RTP_PORTS = (10000, 10199)
# The relay's own allocations, on the media network only; one call holds one.
RELAY_PORTS = (49152, 49251)
REALM = "mdd-sim-gateway"
CREDENTIAL_TTL = 12 * 60 * 60
HEALTH_TIMEOUT = 20


class MediaError(RuntimeError):
    pass


# ------------------------------------------------------------------ recorded mode
def _state_path() -> str:
    return os.path.join(cfg.DATA_DIR, "media", "state.json")


def load_state() -> dict:
    """The recorded mode and relay settings. Missing or unreadable means direct."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    # Its own file rather than config.json: the switch runs in a separate process, and the
    # control plane rewrites config.json under an in-process lock only.
    path = _state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


@contextlib.contextmanager
def switch_lock(blocking: bool = True):
    """Serialise the switch (python -m app.media, its own process) with the control plane's
    supervision. Yields False when not blocking and a switch holds it: without this, the
    supervisor could see direct mode while ``enable`` was still waiting for its new relay to
    answer, and remove it; or write back a mode the switch had just changed."""
    path = os.path.join(cfg.DATA_DIR, "media", ".lock")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def update_state(**fields) -> dict:
    """Change only ``fields``, on the state as it is now. Call with switch_lock held."""
    state = {**load_state(), **fields}
    save_state(state)
    return state


def mode(state: dict | None = None) -> str:
    state = load_state() if state is None else state
    return RELAY if state.get("mode") == RELAY else DIRECT


def default_image() -> str:
    """The local tag the relay runs from. Every source below is the same upstream image."""
    override = os.environ.get("MDD_RELAY_IMAGE", "").strip()
    if override:
        return override
    return f"{LOCAL_IMAGE}:v{VERSION}"


def image_sources(reference: str) -> list[str]:
    """Where a missing relay image may be fetched from, after the Release asset the installer
    and the container updater load (host/mdd_update.py): the copy each release pushes to its
    registry, then the upstream image itself. An override is fetched only as named."""
    if reference != default_image() or os.environ.get("MDD_RELAY_IMAGE", "").strip():
        return [reference]
    return [f"ghcr.io/mddidd/mdd-sim-gateway-relay:v{VERSION}", UPSTREAM_IMAGE]


# ------------------------------------------------------------------ media network
def _client():
    from . import engine
    return engine._client()


def _managed(attrs: dict) -> bool:
    labels = (attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {})
    return labels.get(MANAGED_LABEL) == "true"


def ensure_network(client):
    """The internal media network. Docker picks its subnet, so it cannot collide with one the
    host already uses."""
    try:
        network = client.networks.get(NETWORK)
    except docker.errors.NotFound:
        network = client.networks.create(
            NETWORK, driver="bridge", internal=True,
            labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "media-network"})
        network.reload()
    attrs = network.attrs or {}
    if not _managed(attrs):
        raise MediaError(f"Docker network {NETWORK} exists but was not created by MDD")
    if not attrs.get("Internal"):
        raise MediaError(f"Docker network {NETWORK} is not internal; remove it and try again")
    return network


def network_addressing(network) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]:
    """(subnet, gateway) of the media network's IPv4 pool."""
    for entry in ((network.attrs or {}).get("IPAM") or {}).get("Config") or []:
        try:
            subnet = ipaddress.ip_network(entry.get("Subnet") or "")
        except ValueError:
            continue
        if subnet.version != 4:
            continue
        gateway = entry.get("Gateway") or str(subnet.network_address + 1)
        return subnet, ipaddress.ip_address(gateway.split("/")[0])
    raise MediaError(f"Docker network {NETWORK} has no IPv4 subnet")


def peer_ranges(subnet: ipaddress.IPv4Network,
                gateway: ipaddress.IPv4Address) -> list[tuple[str, str]]:
    """The addresses the relay may send to: the media network's hosts except the gateway,
    which is the host itself."""
    first = subnet.network_address + 1
    last = subnet.broadcast_address - 1
    ranges = []
    if first < gateway:
        ranges.append((first, gateway - 1))
    if gateway < last:
        ranges.append((max(first, gateway + 1), last))
    if not first <= gateway <= last:
        ranges = [(first, last)]
    return [(str(low), str(high)) for low, high in ranges]


# ------------------------------------------------------------------ relay configuration
def render_config(secret: str, subnet: ipaddress.IPv4Network,
                  gateway: ipaddress.IPv4Address) -> str:
    """coturn's configuration. The entrypoint adds the addresses it only knows once started
    (relay-ip on the media network, listening-ip everywhere else)."""
    lines = [
        "# Written by the MDD control plane; the relay container is recreated when it changes.",
        f"listening-port={LISTEN_PORT}",
        f"min-port={RELAY_PORTS[0]}",
        f"max-port={RELAY_PORTS[1]}",
        f"realm={REALM}",
        # TURN REST credentials: short-lived, signed by the control plane, nothing to revoke.
        "use-auth-secret",
        f"static-auth-secret={secret}",
        "fingerprint",
        # Media is DTLS-SRTP end to end and TURN authenticates with HMAC, so the relay holds
        # nothing a TLS listener would protect; a self-signed one would not be trusted anyway.
        # (DTLS listeners are off unless asked for.)
        "no-tls",
        # UDP media only: no TCP relay (RFC 6062), so AMI, SIP over TCP and every other TCP
        # listener stay out of reach whatever the peer list says.
        "no-tcp-relay",
        "no-multicast-peers",
        # The admin CLI is off unless asked for (--cli) in 4.17, and the old STUN
        # compatibility switch no longer exists.
        "no-rfc5780",
        "no-software-attribute",
        "stale-nonce=600",
        "user-quota=10",
        "total-quota=50",
        "max-bps=64000",
        # Deny everything, then allow only the media network. allowed-peer-ip wins over
        # denied-peer-ip; the IPv6 range also covers IPv4-mapped addresses, a known bypass of
        # IPv4-only deny lists.
        "denied-peer-ip=0.0.0.0-255.255.255.255",
        "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        *(f"allowed-peer-ip={low}-{high}" for low, high in peer_ranges(subnet, gateway)),
        "userdb=/tmp/turndb",
        "pidfile=/tmp/turnserver.pid",
        "log-file=stdout",
        "simple-log",
    ]
    return "\n".join(lines) + "\n"


# The configuration holds the TURN secret, so it reaches the relay as a read-only file rather
# than an environment variable `docker inspect` would show. It lives in the private data
# directory; the file itself is world-readable so the relay's nobody user can read it through
# the bind mount.
RELAY_CONFIG_MOUNT = "/etc/mdd-relay/turnserver.conf"

# Run by the stock image's sh before turnserver: adds what is only known once the container is
# attached. relay-ip is its address on the media network (MDD_MEDIA_SUBNET), where allocations
# live; every other address becomes a listening-ip, so a client cannot use the relay to reach
# the relay itself. Loopback serves the health check.
ENTRYPOINT = r"""set -eu
: "${MDD_MEDIA_SUBNET:?}"
test -r /etc/mdd-relay/turnserver.conf
addresses=$(ip -4 -o addr show | awk -v subnet="$MDD_MEDIA_SUBNET" '
  function number(address,  part) {
    split(address, part, ".")
    return ((part[1] * 256 + part[2]) * 256 + part[3]) * 256 + part[4]
  }
  BEGIN { split(subnet, s, "/"); first = number(s[1]); size = 2 ^ (32 - s[2]) }
  {
    for (i = 1; i < NF; i++) if ($i == "inet") { split($(i + 1), a, "/"); address = a[1] }
    if (address == "" || address ~ /^127\./) next
    n = number(address)
    if (n >= first && n < first + size) print "relay-ip=" address
    else print "listening-ip=" address
    address = ""
  }')
case "$addresses" in
  *relay-ip=*) ;;
  *) echo "mdd-relay: no address on the media network $MDD_MEDIA_SUBNET" >&2; exit 1 ;;
esac
{
  cat /etc/mdd-relay/turnserver.conf
  printf '%s\n' "$addresses"
  printf 'listening-ip=127.0.0.1\n'
} > /tmp/turnserver.conf
exec turnserver -c /tmp/turnserver.conf
"""


def credentials(principal: str, secret: str, ttl: int = CREDENTIAL_TTL,
                now: float | None = None) -> dict:
    """TURN REST API credentials: ``<expiry>:<principal>`` signed with the shared secret.
    They expire on their own, so a copied one stops working without any revocation step."""
    expiry = int(now if now is not None else time.time()) + int(ttl)
    username = f"{expiry}:{principal}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return {"username": username, "credential": base64.b64encode(digest).decode(),
            "expires": expiry}


def provisioning(principal: str, request_host: str, state: dict | None = None) -> dict:
    """What a softphone or client needs for media, one shape for both modes."""
    state = load_state() if state is None else state
    if mode(state) != RELAY:
        return {"media_mode": DIRECT, "ice_servers": [], "ice_transport_policy": "all",
                "relay_ready": None}
    host = str(state.get("public_host") or request_host or "").strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int(state.get("public_port") or state.get("port") or DEFAULT_PORT)
    creds = credentials(principal, str(state.get("secret") or ""))
    return {
        "media_mode": RELAY,
        "ice_servers": [{"urls": [f"turn:{host}:{port}?transport=udp",
                                  f"turn:{host}:{port}?transport=tcp"],
                         "username": creds["username"], "credential": creds["credential"]}],
        "ice_transport_policy": "relay",
        "relay_ready": relay_status().get("state") == "ready",
    }


# ------------------------------------------------------------------ relay container
def ensure_image(client, reference: str):
    """The relay image under ``reference``, fetched and tagged when it is not here yet."""
    try:
        return client.images.get(reference)
    except docker.errors.ImageNotFound:
        pass
    failures = []
    for source in image_sources(reference):
        try:
            client.images.pull(source)
            image = client.images.get(source)
        except Exception as exc:  # noqa: BLE001 - offline, registry refused: try the next
            failures.append(f"{source}: {exc}")
            continue
        if source != reference:
            repository, _, tag = reference.rpartition(":")
            image.tag(repository, tag)
        return image
    raise MediaError(f"cannot fetch relay image {reference}: " + "; ".join(failures))


def _container(client):
    try:
        return client.containers.get(CONTAINER)
    except docker.errors.NotFound:
        return None


def _config_paths() -> tuple[str, str]:
    """(path this process writes, path Docker mounts). They differ when the control plane runs
    in a container: MDD_HOST_DATA names the data directory as the Docker host sees it."""
    from . import engine
    return (os.path.join(cfg.DATA_DIR, "media", "relay.conf"),
            os.path.join(engine.HOST_DATA_DIR, "media", "relay.conf"))


def _write_config(path: str, text: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def ensure_relay(client, state: dict, network=None):
    """Make the relay container match ``state``. It is replaced only when what it runs would
    change, so calling this repeatedly never drops a live call."""
    network = network or ensure_network(client)
    subnet, gateway = network_addressing(network)
    secret = str(state.get("secret") or "")
    if not secret:
        raise MediaError("relay secret is missing")
    config = render_config(secret, subnet, gateway)
    image = ensure_image(client, str(state.get("image") or default_image()))
    port = int(state.get("port") or DEFAULT_PORT)
    bind = str(state.get("bind") or "")
    fingerprint = hashlib.sha256("\0".join(
        [config, image.id, str(port), bind, str(subnet), ENTRYPOINT]).encode()).hexdigest()[:32]

    current = _container(client)
    if current is not None:
        labels = (current.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true":
            raise MediaError(f"refusing to replace foreign container {CONTAINER}")
        if labels.get(CONFIG_LABEL) == fingerprint:
            if current.status != "running":
                current.start()
            return current
        current.remove(force=True, v=True)

    local_path, host_path = _config_paths()
    _write_config(local_path, config)
    publish = (bind, port) if bind else port
    container = client.containers.create(
        image.id,
        name=CONTAINER,
        # The published port lives on the default bridge; the media network is internal.
        network="bridge",
        ports={f"{LISTEN_PORT}/udp": publish, f"{LISTEN_PORT}/tcp": publish},
        entrypoint=["sh", "-c", ENTRYPOINT],
        environment={"MDD_MEDIA_SUBNET": str(subnet)},
        volumes={host_path: {"bind": RELAY_CONFIG_MOUNT, "mode": "ro"}},
        # The image runs as nobody. Upstream's turnserver carries a file capability to bind
        # ports below 1024; the kernel refuses to execute it when that capability is outside
        # the bounding set. With no-new-privileges it is never granted: the relay runs with an
        # empty effective set and binds only unprivileged ports.
        cap_drop=["ALL"],
        cap_add=["NET_BIND_SERVICE"],
        security_opt=["no-new-privileges:true"],
        read_only=True,
        # Upstream declares VOLUME /var/lib/coturn; without a mount there, every relay container
        # would leave an anonymous volume behind. The relay keeps nothing there (userdb is in
        # /tmp, credentials are computed).
        tmpfs={"/tmp": "rw,nosuid,nodev,size=8m",
               "/var/lib/coturn": "rw,nosuid,nodev,size=1m"},
        pids_limit=64,
        mem_limit="128m",
        restart_policy={"Name": "unless-stopped"},
        labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "relay", CONFIG_LABEL: fingerprint},
        log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}},
    )
    try:
        network.connect(container)
        container.start()
    except Exception:
        container.remove(force=True, v=True)
        raise
    log.info("started media relay %s on port %s", CONTAINER, port)
    return container


def check_relay(client) -> tuple[bool, str]:
    """(ready, reason). Ready means turnserver answers a STUN binding request."""
    container = _container(client)
    if container is None:
        return False, "relay_missing"
    if container.status != "running":
        return False, "relay_stopped"
    try:
        result = container.exec_run(
            ["turnutils_stunclient", "-p", str(LISTEN_PORT), "127.0.0.1"], demux=False)
    except Exception:  # noqa: BLE001 - Docker busy or the container going away
        return False, "relay_unreachable"
    return (True, "") if result.exit_code == 0 else (False, "relay_not_answering")


def wait_ready(client, timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while True:
        ready, reason = check_relay(client)
        if ready or time.monotonic() >= deadline:
            return ready, reason
        time.sleep(1)


def remove_relay(client) -> None:
    container = _container(client)
    if container is not None:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) == "true":
            container.remove(force=True, v=True)


def remove_network(client) -> bool:
    """Remove the media network once nothing is attached. False while engines still are."""
    try:
        network = client.networks.get(NETWORK)
    except docker.errors.NotFound:
        return True
    network.reload()
    if not _managed(network.attrs or {}):
        return True
    if (network.attrs or {}).get("Containers"):
        return False
    try:
        network.remove()
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError as exc:  # something joined it meanwhile
        log.debug("media network still in use: %s", exc)
        return False
    return True


# ------------------------------------------------------------------ supervision
_status_lock = threading.Lock()
_status: dict = {"state": "off", "reason": ""}
IMAGE_RETRY_SECONDS = 3600
_image_retry_at = 0.0


def relay_status() -> dict:
    with _status_lock:
        return dict(_status)


def _set_status(state: str, reason: str = "") -> None:
    with _status_lock:
        _status.update({"state": state, "reason": reason, "checked_at": int(time.time())})


def _refresh_image(client, state: dict) -> dict:
    """After an update, move the relay to this version's image. A fetch failure keeps the
    image already in use (the update itself has succeeded and the old relay still works) and
    is tried again an hour later. Call with switch_lock held."""
    global _image_retry_at
    wanted = default_image()
    if state.get("image") == wanted or time.monotonic() < _image_retry_at:
        return state
    try:
        ensure_image(client, wanted)
    except MediaError as exc:
        _image_retry_at = time.monotonic() + IMAGE_RETRY_SECONDS
        log.warning("keeping relay image %s: %s", state.get("image"), exc)
        return state
    return update_state(image=wanted)


# The state file's mtime when direct mode was last found clean: nothing to check again until
# the state changes (a switch) or the control plane restarts.
_direct_clean_mtime: float | None = None


def _remove_leftovers(client) -> bool:
    """Direct mode: nothing of the relay may stay. A relay that outlived a switch (the switch
    and this loop raced, or a rollback left it) would keep its port open with nothing behind
    it, and would hold the media network. True once neither is left."""
    remove_relay(client)
    if attached_engines(client):
        return False
    try:
        return remove_network(client)
    except Exception as exc:  # noqa: BLE001 - removed on a later pass
        log.debug("media network not removed yet: %s", exc)
        return False


def _state_mtime() -> float | None:
    try:
        return os.stat(_state_path()).st_mtime
    except OSError:
        return None


def supervise(client=None) -> dict:
    """One pass, called periodically by the control plane: in relay mode bring the relay
    back if it is gone or stopped and record whether it answers; in direct mode remove what
    is left of it. Skipped while a switch is in progress."""
    with switch_lock(blocking=False) as held:
        if not held:
            return relay_status()
        global _direct_clean_mtime
        state = load_state()
        if mode(state) != RELAY:
            _set_status("off")
            # Only a gateway that has ever used relay mode has anything to remove, and only
            # until it has been found clean; after that no Docker call is made here.
            mtime = _state_mtime()
            if state and mtime != _direct_clean_mtime:
                try:
                    if _remove_leftovers(client or _client()):
                        _direct_clean_mtime = mtime
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not remove the media relay: %s", exc)
            return relay_status()
        _direct_clean_mtime = None
        try:
            client = client or _client()
            state = _refresh_image(client, state)
            ensure_relay(client, state)
            ready, reason = check_relay(client)
            _set_status("ready" if ready else "unavailable", reason)
        except Exception as exc:  # noqa: BLE001 - reported, retried on the next pass
            log.warning("media relay not ready: %s", exc)
            _set_status("unavailable", "relay_error")
    return relay_status()


def engine_attachment(client) -> dict | None:
    """In relay mode: the network an engine joins and what its instance.json needs to know.
    None in direct mode. A media network that cannot be prepared leaves the line without
    browser media, never without registration and SMS."""
    if mode() != RELAY:
        return None
    try:
        network = ensure_network(client)
        subnet, _gateway = network_addressing(network)
    except Exception as exc:  # noqa: BLE001
        log.error("media network unavailable, starting the line without it: %s", exc)
        return {"network": None, "instance": {"mode": RELAY, "subnet": "",
                                              "rtp_start": RTP_PORTS[0], "rtp_end": RTP_PORTS[1]}}
    return {"network": network,
            "instance": {"mode": RELAY, "subnet": str(subnet),
                         "rtp_start": RTP_PORTS[0], "rtp_end": RTP_PORTS[1]}}


# ------------------------------------------------------------------ switching
# How an engine filters its media interface (engine/entrypoint.sh reports it in media.json).
# nftables tells the browser leg from the IMS leg by the socket a packet lands on;
# iptables-legacy, the fallback for kernels without nf_tables or its socket match, can only go
# by the port range, which the IMS leg's RTP shares on an IPv4 PDN.
NFT = "nft"
IPTABLES_LEGACY = "iptables-legacy"


def filter_separates_legs(kind: str) -> bool:
    return kind == NFT


def recorded_filter(state: dict) -> str:
    """What the probe found when relay mode was enabled. A state recorded before the fallback
    existed has none, and could only have been enabled with nftables."""
    return str(state.get("filter") or NFT)


def describe_filter(kind: str) -> str:
    if filter_separates_legs(kind):
        return f"{kind} (browser leg only)"
    return (f"{kind} (RTP port range only: does not tell the browser leg from the IMS leg, "
            "whose RTP on an IPv4 PDN is in the same range)")


# What engine/render.py's rulesets rely on, tried once in a throwaway engine container before
# any line is moved, in the order the entrypoint tries them: nf_tables with its socket match on
# the input hook, else iptables-legacy (IPv4, and IPv6 unless the kernel has none). A kernel
# that takes neither would leave every line's media interface down.
PROBE_RULESET = (
    "table inet mdd_media_probe {\n"
    "  chain input {\n"
    "    type filter hook input priority filter; policy accept;\n"
    f"    udp dport {RTP_PORTS[0]}-{RTP_PORTS[1]} socket wildcard 0 accept\n"
    "  }\n"
    "}\n")
# engine/render.py's media_legacy_ruleset for the probe's only interface. The probe's own
# namespace goes away with it, so the DROP never touches a line.
PROBE_LEGACY_RULESET = (
    "*filter\n"
    ":INPUT ACCEPT [0:0]\n"
    ":FORWARD ACCEPT [0:0]\n"
    ":OUTPUT ACCEPT [0:0]\n"
    f"-A INPUT -i eth0 -p udp -m udp --dport {RTP_PORTS[0]}:{RTP_PORTS[1]} -j ACCEPT\n"
    "-A INPUT -i eth0 -j DROP\n"
    "COMMIT\n")
# Prints the filter it loaded as its last line; fails, with each tool's own message on
# stderr, when neither loads.
PROBE_SCRIPT = r"""
if printf '%s' "$RULES" | nft -f -; then echo "filter=nft"; exit 0; fi
echo "nftables ruleset refused, trying iptables-legacy" >&2
printf '%s' "$LEGACY_RULES" | iptables-legacy-restore || exit 1
if [ -e /proc/net/if_inet6 ]; then
  printf '%s' "$LEGACY_RULES" | ip6tables-legacy-restore || exit 1
fi
echo "filter=iptables-legacy"
"""


PROBE_CONTAINER = "mdd-sim-gateway-media-probe"


def _remove_probe(client) -> None:
    """A probe that failed to start is not removed by Docker's auto-remove."""
    try:
        probe = client.containers.get(PROBE_CONTAINER)
    except Exception:  # noqa: BLE001 - absent is the normal case
        return
    labels = (probe.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get(MANAGED_LABEL) == "true":
        try:
            probe.remove(force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove %s: %s", PROBE_CONTAINER, exc)


def probe_engine_firewall(client, network) -> str:
    """The filter engines will use on this host (NFT or IPTABLES_LEGACY), found by loading it.
    MediaError when neither loads."""
    from . import engine
    try:
        image = engine.ensure_image(client)
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"engine image unavailable: {exc}") from exc
    _remove_probe(client)
    try:
        output = client.containers.run(
            image.id,
            name=PROBE_CONTAINER,
            entrypoint=["sh", "-c", PROBE_SCRIPT],
            environment={"RULES": PROBE_RULESET, "LEGACY_RULES": PROBE_LEGACY_RULESET},
            network=network.name,
            cap_add=["NET_ADMIN"],
            labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "media-probe"},
            # docker-py returns a finished container's output only for the json-file and
            # journald drivers, and None otherwise. Synology's daemon defaults to its own `db`
            # driver, so without this the probe ran, its answer was lost, and relay mode was
            # refused with "gave no result" on every DSM host.
            log_config={"type": "json-file", "config": {}},
            remove=True, stdout=True, stderr=True)
    except Exception as exc:  # noqa: BLE001 - ContainerError carries the tools' own messages
        detail = getattr(exc, "stderr", b"") or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise MediaError("engines cannot filter the media network on this host (the engine "
                         "image needs nftables with the kernel's nf_tables and its socket "
                         "match, or iptables-legacy with x_tables): "
                         f"{detail.strip()}") from exc
    finally:
        _remove_probe(client)
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    kind = ""
    for line in str(output or "").splitlines():
        if line.startswith("filter="):
            kind = line.partition("=")[2].strip()
    if kind not in (NFT, IPTABLES_LEGACY):
        raise MediaError(f"engine firewall probe gave no result: {str(output or '').strip()}")
    if kind == IPTABLES_LEGACY:
        log.warning("this kernel refuses the nftables media ruleset; engines will filter the "
                    "media network with iptables-legacy, by port range only (the IMS leg's "
                    "RTP on an IPv4 PDN shares that range)")
    return kind


def enable(client, *, port: int, bind: str = "", public_host: str = "",
           public_port: int | None = None, image: str = "") -> dict:
    """Prepare and verify the relay, then record relay mode. On any failure everything this
    call created is removed and the recorded mode is left as it was."""
    with switch_lock():
        return _enable(client, port=port, bind=bind, public_host=public_host,
                       public_port=public_port, image=image)


def _enable(client, *, port: int, bind: str, public_host: str,
            public_port: int | None, image: str) -> dict:
    previous = load_state()
    had_network = True
    try:
        client.networks.get(NETWORK)
    except docker.errors.NotFound:
        had_network = False
    state = {
        "mode": RELAY,
        "port": int(port),
        "bind": bind,
        "public_host": public_host,
        "public_port": int(public_port or port),
        "image": image or str(previous.get("image") or "") or default_image(),
        "secret": str(previous.get("secret") or "") or secrets.token_urlsafe(32),
    }
    try:
        network = ensure_network(client)
        state["filter"] = probe_engine_firewall(client, network)
        ensure_relay(client, state, network)
        ready, reason = wait_ready(client)
        if not ready:
            raise MediaError(f"relay did not become ready ({reason})")
    except Exception:
        remove_relay(client)
        if mode(previous) == RELAY:
            try:
                ensure_relay(client, previous)
            except Exception as exc:  # noqa: BLE001 - supervision retries it
                log.warning("could not restore the previous relay: %s", exc)
        elif not had_network:
            remove_network(client)
        raise
    save_state(state)
    return state


def disable(client, wait: float = 600) -> bool:
    """Record direct mode and remove the relay. The control plane rebuilds the lines; the
    media network goes once the last one has left it. False if that did not happen in time
    (the network is harmless and is removed by the next ``disable``)."""
    with switch_lock():
        if load_state():
            update_state(mode=DIRECT)
        remove_relay(client)
    deadline = time.monotonic() + wait
    while True:
        if remove_network(client):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


def attached_engines(client) -> list[str]:
    try:
        network = client.networks.get(NETWORK)
        network.reload()
    except docker.errors.NotFound:
        return []
    return sorted(str(entry.get("Name") or "")
                  for entry in ((network.attrs or {}).get("Containers") or {}).values()
                  if str(entry.get("Name") or "") != CONTAINER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.media", description="Show or switch how call media is carried.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show the current media mode")
    relay = sub.add_parser("relay", help="carry media through the built-in TURN relay")
    relay.add_argument("--port", type=int, default=None,
                       help=f"host port the relay publishes, UDP and TCP (default {DEFAULT_PORT})")
    relay.add_argument("--bind", default=None, help="host address to publish it on (default: all)")
    relay.add_argument("--public-host", default=None,
                       help="host name clients use for the relay (default: the one they reached "
                            "the WebUI by)")
    relay.add_argument("--public-port", type=int, default=None,
                       help="port clients use, when a router forwards a different one")
    relay.add_argument("--image", default="", help="relay image to use (default: this version's)")
    direct = sub.add_parser("direct", help="publish each line's RTP ports (the default)")
    direct.add_argument("--wait", type=float, default=600,
                        help="seconds to wait for the lines to leave the media network")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = docker.from_env(timeout=60)
    state = load_state()
    if args.command in (None, "status"):
        current = mode(state)
        print(f"media mode: {current}")
        if current == RELAY:
            ready, reason = check_relay(client)
            print(f"relay: {'ready' if ready else 'unavailable (' + reason + ')'}; "
                  f"port {state.get('port')}"
                  + (f", clients use {state.get('public_host') or '<WebUI host>'}:"
                     f"{state.get('public_port')}"))
            print(f"engine media filter: {describe_filter(recorded_filter(state))}")
            print(f"lines on the media network: {', '.join(attached_engines(client)) or 'none'}")
        return 0
    if args.command == "relay":
        port = args.port if args.port is not None else int(state.get("port") or DEFAULT_PORT)
        if not 1 <= port <= 65535:
            parser.error("--port must be 1-65535")
        try:
            new = enable(
                client, port=port,
                bind=args.bind if args.bind is not None else str(state.get("bind") or ""),
                public_host=(args.public_host if args.public_host is not None
                             else str(state.get("public_host") or "")),
                public_port=(args.public_port if args.public_port is not None
                             else (state.get("public_port") if args.port is None else None)),
                image=args.image)
        except Exception as exc:  # noqa: BLE001 - the operator reads this, not a traceback
            print(f"media mode unchanged: {exc}", file=sys.stderr)
            return 1
        print(f"media mode: relay (port {new['port']}/udp+tcp)."
              + ("" if mode(state) == RELAY else
                 " Running lines are rebuilt one at a time and register again."))
        print(f"engine media filter: {describe_filter(recorded_filter(new))}")
        return 0
    if args.command == "direct":
        if mode(state) == DIRECT and not attached_engines(client) and _container(client) is None:
            remove_network(client)
            print("media mode: direct (unchanged)")
            return 0
        print("media mode: direct. Running lines are rebuilt one at a time and register again.")
        if disable(client, wait=args.wait):
            print("relay and media network removed")
            return 0
        print(f"still on the media network: {', '.join(attached_engines(client))}; "
              "run this again once they have been rebuilt", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
