#!/usr/bin/env python3
"""Build the Hardware image remotely from a small, explicit context.

Only the supervisor, bridge and Dockerfile are sent. The build downloads pinned
PC/SC sources and distribution NetworkManager packages, and requires the
released Control image to exist on the Docker host already.
"""
import argparse
import io
from pathlib import Path
import shlex
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--base-image", default="mdd-sim-gateway/control:v1.11.0")
    parser.add_argument("--tag", default="mdd-sim-gateway/hardware:dev")
    parser.add_argument("--debian-mirror", default="")
    parser.add_argument("--debian-security-mirror", default="")
    args = parser.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]
    inspect = ["sudo", "-n", "/usr/local/bin/docker", "image", "inspect", args.base_image,
               "--format", "{{.Id}} {{.Architecture}}"]
    base, arch = subprocess.check_output(ssh + [shlex.join(inspect)], text=True).strip().split()
    if arch != "amd64":
        raise SystemExit(f"NAS hardware image requires amd64 base, got {arch}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in ("runtime/Dockerfile.hardware", "runtime/hardware.py",
                     "host/vpcd_modem_bridge.py",
                     "patches/ccid/01_hsic_slot_status.patch",
                     "patches/ccid/02_hsic_malformed_atr.patch",
                     "patches/ccid/03_scr_prime_reader.patch"):
            archive.add(ROOT / name, arcname=name)
    build = ["sudo", "-n", "/usr/local/bin/docker", "build", "--network", "bridge",
             "--build-arg", "BASE_IMAGE=" + base, "-f", "runtime/Dockerfile.hardware",
             "-t", args.tag, "-"]
    if args.debian_mirror:
        build[build.index("-f"):build.index("-f")] = [
            "--build-arg", "DEBIAN_MIRROR=" + args.debian_mirror]
    if args.debian_security_mirror:
        build[build.index("-f"):build.index("-f")] = [
            "--build-arg", "DEBIAN_SECURITY_MIRROR=" + args.debian_security_mirror]
    subprocess.run(ssh + [shlex.join(build)], input=payload.getvalue(), check=True)


if __name__ == "__main__":
    main()
