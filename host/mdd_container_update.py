#!/usr/bin/env python3
"""Detached, transactional updater for the three-service container deployment.

Control launches this file in a short-lived sibling made from the currently running Control
image.  The sibling survives replacement of Control, owns no host namespace, and can touch only
the project data directory and Docker socket.  Release image archives are verified against the
Release SHA256SUMS before they are loaded.  The Compose file is changed only after all four
images pass their architecture/component/version checks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docker

try:
    import mdd_update
except ModuleNotFoundError:  # Imported as host.mdd_container_update by tests.
    from host import mdd_update


COMPONENTS = ("control", "hardware", "egress", "engine")
BASE_COMPONENTS = ("hardware", "egress", "control")
# Hardware may have to reset the modem to recover a stale QMI session before it is healthy.
WAIT_SECONDS = {"hardware": 300}
MANAGED = "io.mdd-sim-gateway.managed"
COMPONENT = "io.mdd-sim-gateway.component"
VERSION = "org.opencontainers.image.version"
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024
# How much larger an image is in Docker's store than its Release archive. Measured on the
# v1.13.0-rc1 arm64 assets: engine 152 MB -> 918 MB, control 131 -> 774, hardware 139 -> 824,
# egress 82 -> 512, so about six times; layers shared with the running release make it less.
IMAGE_EXPANSION = 6
STAGING_MARGIN = 512 * MIB
IMAGE_STORE_MARGIN = 1 * GIB
# When the Release did not report its asset sizes. This used to be a flat 6 GiB, sized for
# images before they were slimmed, which refused a Raspberry Pi with over 5 GiB free.
FALLBACK_REQUIRED = 4 * GIB
# Stable aliases the host install and the Settings page treat as current; never pruned here.
PROTECTED_TAGS = {"mdd-sim-gateway/engine:latest", "mdd-sim-gateway/control:latest",
                  "mdd-sim-gateway/engine-base:trusted"}
IMAGE_PREFIXES = ("mdd-sim-gateway/", "ghcr.io/mddidd/mdd-sim-gateway-")


def space_required(sizes: dict, names: list[str]) -> tuple[int, int]:
    """(bytes the staging directory needs, bytes Docker's image store needs) for this update.

    Every archive is downloaded before the first is loaded, so staging holds all of them at
    once. The image store then receives each image unpacked while its archive is still on disk,
    which is the same filesystem on a Pi or a NAS, so the store is asked for both.
    """
    archive_sizes = [sizes.get(name) for name in names]
    if not all(isinstance(size, int) and size > 0 for size in archive_sizes):
        return FALLBACK_REQUIRED, FALLBACK_REQUIRED
    archives = sum(archive_sizes)
    return (archives + STAGING_MARGIN,
            archives * (IMAGE_EXPANSION + 1) + IMAGE_STORE_MARGIN)


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def _managed_image(image) -> bool:
    tags = [str(tag) for tag in (getattr(image, "tags", None) or [])]
    labels = (((getattr(image, "attrs", None) or {}).get("Config") or {}).get("Labels") or {})
    return labels.get(MANAGED) == "true" or any(tag.startswith(IMAGE_PREFIXES) for tag in tags)


def previous_image_ids(project: Path) -> set[str]:
    """Image IDs of the installed release, from update/installed-images.json."""
    try:
        installed = json.loads((project / "update" / "installed-images.json").read_text(
            encoding="utf-8"))
        images = installed.get("images") if isinstance(installed, dict) else None
    except (OSError, ValueError):
        return set()
    if not isinstance(images, dict):
        return set()
    return {str(item.get("image_id")) for item in images.values()
            if isinstance(item, dict) and item.get("image_id")}


def prune_superseded_images(client, keep_ids: set[str]) -> int:
    """After a successful update, delete MDD images older than the release just replaced.

    The new release and the one it replaced (the rollback) stay, as does anything a container
    uses and the stable host-install aliases. Without this every release left its images
    behind: a Pi still held the release candidates of the version before last. Best effort --
    a failure is reported and never fails an update that has already succeeded.
    """
    removed = 0
    try:
        keep = {str(image_id) for image_id in keep_ids if image_id}
        keep.update(str(container.image.id) for container in client.containers.list(all=True)
                    if getattr(container, "image", None) is not None)
        for image in client.images.list(all=True):
            tags = set(image.tags or [])
            if tags & PROTECTED_TAGS or str(image.id) in keep or not _managed_image(image):
                continue
            client.images.remove(str(image.id), force=True, noprune=False)
            removed += 1
    except Exception as exc:  # noqa: BLE001 - reported, never fails the update
        print(f"superseded images not removed: {exc}", file=sys.stderr)
    return removed


def host_arch() -> str:
    return mdd_update.host_arch()


def find_compose(project: Path) -> Path:
    configured = os.environ.get("MDD_COMPOSE_FILE", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = project / candidate
        candidate = candidate.resolve()
        if candidate.parent != project.resolve() or candidate.name not in COMPOSE_NAMES:
            raise mdd_update.UpdateError("MDD_COMPOSE_FILE must name a Compose file in /data")
        if not candidate.is_file():
            raise mdd_update.UpdateError(f"Compose file does not exist: {candidate.name}")
        return candidate
    matches = [project / name for name in COMPOSE_NAMES if (project / name).is_file()]
    if len(matches) != 1:
        raise mdd_update.UpdateError(
            "container update requires exactly one docker-compose.yml/compose.yaml in /data")
    return matches[0]


def canonical_images(repository: str, version: str) -> dict[str, str]:
    owner = repository.split("/", 1)[0].lower()
    return {component: f"ghcr.io/{owner}/mdd-sim-gateway-{component}:v{version}"
            for component in COMPONENTS}


def rewrite_compose(source: str, images: dict[str, str]) -> str:
    """Replace only the four release image references, preserving user edits and comments."""
    changed: set[str] = set()
    output = []
    image_line = re.compile(
        r"^(?P<indent>\s*)image:\s*(?P<quote>['\"]?)"
        r"ghcr\.io/[^/\s'\"]+/mdd-sim-gateway-(?P<component>control|hardware|egress)"
        r"(?::[^\s'\"]+|@sha256:[0-9a-f]{64})(?P=quote)"
        r"(?P<suffix>\s*(?:#.*)?)$")
    engine_line = re.compile(
        r"^(?P<indent>\s*)MDD_ENGINE_IMAGE:\s*(?P<quote>['\"]?)"
        r"ghcr\.io/[^/\s'\"]+/mdd-sim-gateway-engine"
        r"(?::[^\s'\"]+|@sha256:[0-9a-f]{64})(?P=quote)"
        r"(?P<suffix>\s*(?:#.*)?)$")
    for line in source.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        newline = line[len(raw):]
        match = image_line.match(raw)
        if match:
            component = match.group("component")
            changed.add(component)
            quote = match.group("quote")
            line = (f"{match.group('indent')}image: {quote}{images[component]}{quote}"
                    f"{match.group('suffix')}{newline}")
        else:
            match = engine_line.match(raw)
            if match:
                changed.add("engine")
                quote = match.group("quote")
                line = (f"{match.group('indent')}MDD_ENGINE_IMAGE: "
                        f"{quote}{images['engine']}{quote}{match.group('suffix')}{newline}")
        output.append(line)
    if changed != set(COMPONENTS):
        missing = ", ".join(sorted(set(COMPONENTS) - changed))
        raise mdd_update.UpdateError(f"Compose image references are incomplete: {missing}")
    return "".join(output)


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 600,
        env: dict | None = None) -> str:
    completed = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=timeout, env=env)
    if completed.returncode:
        raise mdd_update.UpdateError(
            f"{' '.join(command[:3])} failed: {completed.stdout[-2000:].strip()}")
    return completed.stdout


def local_loaded_image(component: str, version: str, repository: str) -> str:
    if component == "engine":
        owner = repository.split("/", 1)[0].lower()
        return f"ghcr.io/{owner}/mdd-sim-gateway-engine:v{version}"
    return f"mdd-sim-gateway/{component}:v{version}"


def verify_and_tag_image(client, component: str, version: str, repository: str,
                         target: str) -> str:
    image = client.images.get(local_loaded_image(component, version, repository))
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    actual_arch = str(image.attrs.get("Architecture") or "")
    if actual_arch != host_arch() or labels.get(MANAGED) != "true" \
            or labels.get(VERSION) != version:
        raise mdd_update.UpdateError(
            f"Release {component} image identity mismatch: "
            f"{actual_arch or 'unknown'}|{labels.get(VERSION) or 'unknown'}")
    if component in {"control", "hardware", "egress"} \
            and labels.get(COMPONENT) != component:
        raise mdd_update.UpdateError(f"Release {component} image has the wrong component label")
    if component == "engine" and "socks5" not in str(
            labels.get("io.mdd-sim-gateway.egress-transports") or "").split(","):
        raise mdd_update.UpdateError("Release Engine image lacks the container egress capability")
    repository_name, tag = target.rsplit(":", 1)
    if not image.tag(repository_name, tag):
        raise mdd_update.UpdateError(f"could not tag the verified {component} image")
    return image.id


def wait_container(client, name: str, image_id: str, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    last = "missing"
    while time.monotonic() < deadline:
        try:
            container = client.containers.get(name)
            container.reload()
            labels = (container.attrs.get("Config") or {}).get("Labels") or {}
            health = ((container.attrs.get("State") or {}).get("Health") or {}).get("Status")
            last = health or (container.attrs.get("State") or {}).get("Status") or "unknown"
            if labels.get(MANAGED) == "true" and container.image.id == image_id \
                    and last in {"healthy", "running"}:
                return
        except docker.errors.NotFound:
            last = "missing"
        time.sleep(3)
    raise mdd_update.UpdateError(f"{name} did not become healthy ({last})")


# Only what the Docker CLI itself needs. Compose interpolates ${VAR:-default} from its own
# environment, and this helper runs in the Control image, whose ENV includes
# MDD_HTTP_PORT=8443 — the port Control listens on *inside* its container. The Compose
# file uses the same name for the *host* port, so an inherited environment published the
# web console on host port 8443: on a NAS where that port belongs to another service the
# update and its rollback both failed, and elsewhere the console would silently have moved.
COMPOSE_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONFIG",
                       "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


def compose_environment() -> dict:
    """The environment an operator's own `docker compose up` would interpolate with."""
    return {name: os.environ[name] for name in COMPOSE_ENVIRONMENT if name in os.environ}


def compose_location(compose: Path) -> tuple[Path, Path]:
    """The Compose file and project directory as the Docker host names them.

    Compose stamps every container with its project directory and file path, and Synology
    Container Manager uses those labels to tie containers to a project. Run from this
    helper's /data mount they read `/data/compose.yaml`, a path that exists nowhere on the
    NAS: the containers still showed the project name, but every stop or delete in the UI
    was sent for container "undefined", and the operator had to clean up over SSH. The
    launcher also mounts the data directory at its own host path; use it when present.
    """
    host = os.environ.get("MDD_HOST_DATA", "").strip()
    if host.startswith("/") and (Path(host) / compose.name).is_file():
        return Path(host) / compose.name, Path(host)
    return compose, compose.parent


# How long a base container may take to exit once asked to stop. Docker's own stop gives up
# about ten seconds after SIGKILL, and Compose then aborts the recreate.
STOP_SECONDS = 120


def settle_services(client, services) -> None:
    """Stop the named base services ourselves and wait until they have really exited.

    On the DS1621+ a Hardware container once took 32 s to exit, 22 s of them after SIGKILL
    (a process stuck in the kernel on modem I/O). Docker reported "tried to kill container,
    but did not receive an exit event", Compose aborted with the new container left under a
    temporary `<id>_` name, and the rollback then failed because the old container still held
    the service name. Waiting here, and removing temporaries a failed recreate left behind,
    lets Compose find only stopped containers, which it replaces without killing anything.
    """
    for service in services:
        name = f"mdd-sim-gateway-{service}"
        found = client.containers.list(all=True, filters={"label": [
            "com.docker.compose.project=mdd-sim-gateway",
            f"com.docker.compose.service={service}"]})
        for container in found:
            if container.name != name:
                container.remove(force=True)
                continue
            try:
                container.stop(timeout=30)
            except docker.errors.APIError:
                pass  # the deadline below decides whether it actually exited
            deadline = time.monotonic() + STOP_SECONDS
            while True:
                try:
                    container.reload()
                except docker.errors.NotFound:
                    break
                if not (container.attrs.get("State") or {}).get("Running"):
                    break
                if time.monotonic() >= deadline:
                    raise mdd_update.UpdateError(
                        f"{name} did not exit within {STOP_SECONDS} s of being stopped")
                time.sleep(2)


def compose_up(compose: Path, wait, client=None) -> None:
    """Recreate the base services in dependency order, waiting on this helper's clock.

    Control declares `depends_on: {condition: service_healthy}` on Hardware, and Compose
    gives up the moment Hardware first reports unhealthy. A freshly recreated Hardware
    usually inherits a stale QMI session from the container it replaced, and recovering
    it (a modem reset plus re-enumeration) outlasts the image's health-check grace
    period. Leaving the ordering to Compose therefore failed the update, and then the
    rollback, on the very restart it was performing. `--no-deps` hands the ordering to
    wait(), whose deadline is long enough for that recovery, including when rolling back
    to an image whose own grace period is still the short one.
    """
    compose_file, project_dir = compose_location(compose)
    command = ["docker", "compose", "-p", "mdd-sim-gateway", "-f", str(compose_file),
               "--project-directory", str(project_dir),
               "up", "-d", "--no-build", "--force-recreate", "--no-deps"]
    environment = compose_environment()
    if client is not None:
        settle_services(client, ("hardware", "egress"))
    run([*command, "hardware", "egress"], cwd=project_dir, timeout=600, env=environment)
    wait("hardware")
    wait("egress")
    if client is not None:
        settle_services(client, ("control",))
    run([*command, "control"], cwd=project_dir, timeout=600, env=environment)


def docker_root_free_bytes(client) -> int:
    """Measure the daemon's image store from a read-only bind in a disposable container."""
    root = str((client.info() or {}).get("DockerRootDir") or "").strip()
    if not root.startswith("/"):
        raise mdd_update.UpdateError("Docker did not report an absolute image-store path")
    current = client.containers.get(socket.gethostname())
    # The Control image's ENTRYPOINT is `python run.py`, so a bare command would be
    # appended to it and start the whole control plane instead of this one-liner.
    output = client.containers.run(
        current.image.id,
        ["import os; s=os.statvfs('/docker-root'); print(s.f_bavail*s.f_frsize)"],
        entrypoint=["python", "-c"],
        # docker-py returns a finished container's output only for the json-file and
        # journald drivers and None otherwise. Synology's daemon defaults to its own `db`
        # driver, so without this the measurement ran and was then lost.
        log_config={"type": "json-file", "config": {}},
        remove=True, network_disabled=True, read_only=True, cap_drop=["ALL"],
        volumes={root: {"bind": "/docker-root", "mode": "ro"}},
    )
    try:
        return int(output.decode().strip() if isinstance(output, bytes) else str(output).strip())
    except ValueError as exc:
        raise mdd_update.UpdateError("could not measure free Docker image-store space") from exc


def recreate_engine(client, name: str) -> None:
    """Ask the freshly started Control container to recreate one previously running line."""
    prefix = "mdd-sim-gateway-engine-"
    iid = name.removeprefix(prefix)
    if name == iid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", iid):
        raise mdd_update.UpdateError(f"invalid managed Engine name: {name}")
    control = client.containers.get("mdd-sim-gateway-control")
    command = [
        "python", "-c",
        ("import os,sys; from app import config as cfg, engine; "
         "i=cfg.get_instance(sys.argv[1]); "
         "assert i is not None, 'saved line is missing'; "
         "engine.start(i, cfg.get_settings(), "
         "dev_mounts=os.environ.get('MDD_DEV_MOUNTS','') == '1', "
         "reason='release_update')"),
        iid,
    ]
    result = control.exec_run(command)
    if hasattr(result, "exit_code"):
        exit_code, output = int(result.exit_code), result.output
    else:
        exit_code, output = int(result[0]), result[1]
    if exit_code:
        detail = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
        raise mdd_update.UpdateError(f"could not recreate {name}: {detail[-1000:].strip()}")


def roll_engines(client, target_image_id: str, status: mdd_update.Status) -> None:
    engines = [item for item in client.containers.list(filters={"label": [
        f"{MANAGED}=true", f"{COMPONENT}=engine"]})]
    for index, old in enumerate(sorted(engines, key=lambda item: item.name), 1):
        name = old.name
        status.publish("running", "engine_rollout", artifact=name,
                       engine_index=index, engine_total=len(engines))
        old.remove(force=True)
        recreate_engine(client, name)
        wait_container(client, name, target_image_id, timeout=240)


RELAY_CONTAINER = "mdd-sim-gateway-relay"


def relay_mode(project: Path) -> bool:
    """Whether this gateway carries call media through the relay (control/app/media.py)."""
    try:
        state = json.loads((project / "media" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and state.get("mode") == "relay"


def fetch_relay_image(base_url: str, version: str, arch: str, sums: Path, routes: list,
                      active: int, sizes: dict, staging: Path) -> int:
    """Load this release's relay image beside the one in use. It is optional and outside the
    rollback transaction: failing leaves the relay on the image it runs, and the new Control
    moves it over once the image is here (or fetched from a registry)."""
    name = f"mdd-sim-gateway-relay-v{version}-{arch}.tar.gz"
    archive = staging / name
    try:
        active = mdd_update.fetch_release_asset(
            f"{base_url}/{name}", archive, name, routes, active,
            asset_sizes=sizes, phase="relay_image")
        mdd_update.verify_release_file(archive, sums, f"{arch} relay image")
        mdd_update.load_relay_image(archive, version)
    except Exception as exc:  # noqa: BLE001 - reported, never fails the update
        print(f"media relay image not updated: {exc}", file=sys.stderr)
    finally:
        archive.unlink(missing_ok=True)
    return active


def remove_relay(client) -> None:
    """After a rollback: the Control rolled back to may not know relay mode, and would leave
    the relay's port open with nothing behind it. One that does recreates it within a minute.
    Best effort: it must never turn a successful rollback into a failed one."""
    try:
        relay = client.containers.get(RELAY_CONTAINER)
        labels = (relay.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED) == "true" and labels.get(COMPONENT) == "relay":
            relay.remove(force=True, v=True)
    except docker.errors.NotFound:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"could not remove the media relay after rollback: {exc}", file=sys.stderr)


def perform(project: Path, version: str, repository: str, network_path: Path,
            status: mdd_update.Status) -> None:
    staging = Path(tempfile.mkdtemp(prefix="container-update.", dir=str(project / "update")))
    client = None
    compose = None
    compose_backup = project / "update" / "compose.previous.yaml"
    switched = False
    old_base_ids = {}
    old_engine_ids = {}
    try:
        compose = find_compose(project)
        # The release being replaced, read before it is overwritten below. Its images are the
        # rollback, including an Engine no line happened to be running at update time.
        previous_ids = previous_image_ids(project)
        request_network = mdd_update.read_network_config(network_path)
        fallback = str(request_network.get("proxy_url") or "")
        routes = mdd_update.validated_download_routes(
            fallback,
            route=str(request_network.get("route") or ("library" if fallback else "direct")),
            route_name=str(request_network.get("route_name") or ""),
            routes=request_network.get("routes")
            if isinstance(request_network.get("routes"), list) else None)
        sizes = request_network.get("asset_sizes") if isinstance(
            request_network.get("asset_sizes"), dict) else {}
        arch = host_arch()
        names = {component: f"mdd-sim-gateway-{component}-v{version}-{arch}.tar.gz"
                 for component in COMPONENTS}
        needed = list(names.values())
        if relay_mode(project):
            needed.append(f"mdd-sim-gateway-relay-v{version}-{arch}.tar.gz")
        staging_needed, store_needed = space_required(sizes, needed)
        staging_free = shutil.disk_usage(project / "update").free
        if staging_free < staging_needed:
            raise mdd_update.UpdateError(
                "not enough persistent disk space for a transactional container update: "
                f"needs {_gib(staging_needed)}, {_gib(staging_free)} free")
        base_url = f"https://github.com/{repository}/releases/download/v{version}"
        client = docker.from_env()
        store_free = docker_root_free_bytes(client)
        if store_free < store_needed:
            raise mdd_update.UpdateError(
                "not enough Docker image-store space for a transactional container update: "
                f"needs {_gib(store_needed)}, {_gib(store_free)} free")
        old_base_ids = {
            component: client.containers.get(f"mdd-sim-gateway-{component}").image.id
            for component in BASE_COMPONENTS
        }
        # Only lines which are running at the start of the transaction belong in the rollout.
        # Disabled, PIN-frozen and manually stopped lines must remain stopped.
        old_engine_ids = {
            item.name: item.image.id
            for item in client.containers.list(filters={"label": [
                f"{MANAGED}=true", f"{COMPONENT}=engine"]})
        }
        status.publish("running", "downloading", install_mode="container",
                       engine_image_required=True)
        sums = staging / "SHA256SUMS"
        active = mdd_update.fetch_release_asset(
            f"{base_url}/SHA256SUMS", sums, "SHA256SUMS", routes,
            asset_sizes=sizes, status=status)
        archives = {}
        archive_digests = {}
        for component in COMPONENTS:
            name = names[component]
            archive = staging / name
            active = mdd_update.fetch_release_asset(
                f"{base_url}/{name}", archive, name, routes, active,
                asset_sizes=sizes, status=status, phase=f"{component}_image")
            archive_digests[component] = mdd_update.verify_release_file(
                archive, sums, f"{arch} {component} image")
            archives[component] = archive

        targets = canonical_images(repository, version)
        image_ids = {}
        for component in COMPONENTS:
            status.publish("running", f"{component}_image", artifact=names[component],
                           detail=f"importing verified {arch} {component} image")
            run(["docker", "load", "--input", str(archives[component])], timeout=1800)
            image_ids[component] = verify_and_tag_image(
                client, component, version, repository, targets[component])
        if relay_mode(project):
            active = fetch_relay_image(base_url, version, arch, sums, routes, active, sizes,
                                       staging)
        verified_images = {
            component: {"reference": targets[component], "image_id": image_ids[component],
                        "archive_sha256": archive_digests[component]}
            for component in COMPONENTS
        }

        # Use the application's SQLite snapshot backup while the old Control is still running.
        sys.path.insert(0, "/app/control")
        try:
            from app import operations  # type: ignore  # pylint: disable=import-outside-toplevel
        except ModuleNotFoundError:
            from control.app import operations  # pylint: disable=import-outside-toplevel
        status.publish("running", "backup")
        saved = operations.create_local_backup("pre-container-update")

        original = compose.read_text(encoding="utf-8")
        updated = rewrite_compose(original, targets)
        status.publish("running", "applying", backup=saved.get("name", ""))
        compose_backup.write_text(original, encoding="utf-8")
        os.chmod(compose_backup, 0o600)
        temporary = compose.with_suffix(compose.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.chmod(temporary, compose.stat().st_mode & 0o777)
        os.replace(temporary, compose)
        switched = True

        status.publish("running", "reloading", backup=saved.get("name", ""))
        def wait_new(component):
            wait_container(client, f"mdd-sim-gateway-{component}", image_ids[component],
                           timeout=WAIT_SECONDS.get(component, 180))

        compose_up(compose, wait_new, client)
        wait_new("control")
        roll_engines(client, image_ids["engine"], status)
        # Release validation: `touch <data>/update/fail-after-switch` makes the next update
        # fail once every container already runs the new release, so the whole-stack
        # rollback can be exercised on real hardware. It must live in the helper that
        # performs the update, i.e. in the release being updated *from*. One-shot: the
        # marker is consumed when it fires, so a forgotten file cannot block later updates.
        drill = project / "update" / "fail-after-switch"
        if drill.exists():
            drill.unlink(missing_ok=True)
            raise mdd_update.UpdateError("rollback drill requested by update/fail-after-switch")
        mdd_update.atomic_json(project / "update" / "installed-images.json", {
            "version": version, "architecture": arch, "installed_at": int(time.time()),
            "images": verified_images})
        # One generation back is the rollback; anything older is only taking space.
        prune_superseded_images(client, {*image_ids.values(), *old_base_ids.values(),
                                         *old_engine_ids.values(), *previous_ids})
        status.publish("success", "done", backup=saved.get("name", ""),
                       elapsed_seconds=int(time.time()) - status.started)
    except Exception as exc:
        rollback_ok = False
        rollback_error = ""
        if switched and compose is not None and compose_backup.is_file() and client is not None:
            try:
                status.publish("running", "rollback", error=str(exc)[:1000])
                shutil.copy2(compose_backup, compose)
                def wait_old(component):
                    wait_container(client, f"mdd-sim-gateway-{component}",
                                   old_base_ids[component],
                                   timeout=WAIT_SECONDS.get(component, 180))

                compose_up(compose, wait_old, client)
                wait_old("control")
                for name, old_image_id in old_engine_ids.items():
                    try:
                        current = client.containers.get(name)
                        current.reload()
                        if current.image.id == old_image_id \
                                and (current.attrs.get("State") or {}).get("Status") == "running":
                            continue
                        current.remove(force=True)
                    except docker.errors.NotFound:
                        pass
                    recreate_engine(client, name)
                    wait_container(client, name, old_image_id, timeout=240)
                remove_relay(client)
                rollback_ok = True
            except Exception as rollback_exc:  # preserve both causes in the private status
                rollback_error = str(rollback_exc)[:1000]
        # Before the Compose switch nothing was changed, so there is no rollback to report;
        # "rollback_succeeded: false" there read as if the stack had been left broken.
        rollback = ({"rollback_succeeded": rollback_ok, "rollback_error": rollback_error}
                    if switched else {})
        status.publish("failed", "rollback" if switched else status.phase,
                       error=str(exc)[:2000], **rollback)
        raise
    finally:
        if client is not None:
            client.close()
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--network-config", required=True, type=Path)
    args = parser.parse_args()
    if not mdd_update.VERSION_RE.fullmatch(args.version) \
            or not mdd_update.REPOSITORY_RE.fullmatch(args.repository):
        raise SystemExit("invalid update target")
    project = args.data.resolve()
    (project / "update").mkdir(mode=0o700, parents=True, exist_ok=True)
    status = mdd_update.Status(project / "orchestrator" / "update-status.json", args.version)
    try:
        perform(project, args.version, args.repository, args.network_config.resolve(), status)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
