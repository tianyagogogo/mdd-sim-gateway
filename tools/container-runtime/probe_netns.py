#!/usr/bin/env python3
"""Verify TUN/route isolation on a Linux Docker host using a disposable container.

Uses an existing image with python3 and ip. No image pull, host network, host PID,
Docker socket mount, package installation, or host route mutation is performed.
SSH host identity must already be trusted. The SSH user needs passwordless sudo
for Docker and access to the read-only ip commands used for snapshots.
"""
import argparse
import json
import re
import shlex
import subprocess
import time
import uuid


PROBE = r'''
import fcntl, json, os, struct, subprocess, time
def ip(*args):
    return subprocess.check_output(["ip", *args], text=True).strip()
fd = os.open("/dev/net/tun", os.O_RDWR)
fcntl.ioctl(fd, 0x400454ca, struct.pack("16sH", b"mdd-proof0", 0x0001 | 0x1000))
ip("link", "set", "mdd-proof0", "up")
ip("address", "add", "192.0.2.1/30", "dev", "mdd-proof0")
ip("route", "add", "198.51.100.42/32", "dev", "mdd-proof0", "proto", "186")
route = ip("route", "get", "198.51.100.42")
assert "dev mdd-proof0" in route, route
print(json.dumps({"ready": True, "netns": os.readlink("/proc/self/ns/net"),
                  "route": route, "routes": ip("-4", "route", "show")}), flush=True)
time.sleep(45)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH user@host")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--image", required=True, help="Existing image with python3 and ip")
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    args = parser.parse_args()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
           "-o", "StrictHostKeyChecking=yes", "-i", args.identity,
           "-p", str(args.port), args.host]

    def remote(argv):
        result = subprocess.run(ssh + [shlex.join(argv)], text=True,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def docker(*argv):
        return remote(["sudo", "-n", args.docker, *argv])

    def snapshot():
        def routes(family):
            # IPv6 RA route lifetimes count down even when no configuration changes.
            raw = remote(["ip", family, "route", "show", "table", "all"])
            return sorted(re.sub(r"\bexpires \d+sec\b", "expires <lifetime>", raw).splitlines())
        return {"netns": remote(["readlink", "/proc/self/ns/net"]),
                "routes_v4": routes("-4"),
                "rules_v4": remote(["ip", "-4", "rule", "show"]),
                "routes_v6": routes("-6"),
                "rules_v6": remote(["ip", "-6", "rule", "show"]),
                "links": remote(["ip", "-o", "link", "show"])}

    # Resolve a local immutable image ID so the probe never pulls or races a tag.
    image = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    name = "mdd-netns-proof-" + uuid.uuid4().hex[:12]
    before = snapshot()
    evidence = None
    during = None
    try:
        docker("run", "--detach", "--name", name, "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
               "--device", "/dev/net/tun:/dev/net/tun:rwm",
               "--security-opt", "no-new-privileges", "--pids-limit", "32",
               "--memory", "128m",
               "--label", "com.centurylinklabs.watchtower.enable=false",
               "--label", "io.mdd-sim-gateway.component=netns-proof",
               "--entrypoint", "python3", image, "-c", PROBE)
        for _ in range(10):
            logs = docker("logs", name)
            if logs:
                evidence = json.loads(logs.splitlines()[-1])
                break
            if docker("inspect", name, "--format", "{{.State.Running}}") != "true":
                raise RuntimeError("Probe exited before creating its TUN and route")
            time.sleep(0.5)
        if not evidence or not evidence.get("ready"):
            raise RuntimeError("Probe did not become ready")
        if evidence["netns"] == before["netns"]:
            raise RuntimeError("Probe unexpectedly shares the host network namespace")
        during = snapshot()
        if docker("inspect", name, "--format", "{{.State.Running}}") != "true":
            raise RuntimeError("Probe exited before the host snapshot completed")
    finally:
        # Only the randomly named container created by this invocation is removed.
        docker("rm", "--force", name)
    after = snapshot()
    report = {"image_id": image, "probe": evidence,
              "host_unchanged_during": before == during,
              "host_unchanged_after": before == after,
              "container_removed": not bool(docker("ps", "--all", "--quiet", "--filter", "name=" + name)),
              "changed_during": [k for k in before if before[k] != during[k]],
              "changed_after": [k for k in before if before[k] != after[k]]}
    print(json.dumps(report, indent=2))
    if not all(report[k] for k in ("host_unchanged_during", "host_unchanged_after", "container_removed")):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
