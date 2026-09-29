#!/usr/bin/env python3
"""Overlay cellular support onto an already validated NAS Hardware image."""
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
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--tag", default="mdd-sim-gateway/hardware:dev")
    parser.add_argument("--version", default="dev")
    parser.add_argument("--debian-mirror", default="")
    parser.add_argument("--debian-security-mirror", default="")
    args = parser.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]
    inspect = ["sudo", "-n", "/usr/local/bin/docker", "image", "inspect", args.base_image,
               "--format", "{{.Id}} {{.Architecture}}"]
    base, arch = subprocess.check_output(ssh + [shlex.join(inspect)], text=True).strip().split()
    if arch != "amd64":
        raise SystemExit(f"NAS hardware overlay requires amd64 base, got {arch}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in ("runtime/Dockerfile.hardware-overlay", "runtime/hardware.py"):
            archive.add(ROOT / name, arcname=name)
    build = ["sudo", "-n", "/usr/local/bin/docker", "build", "--network", "bridge",
             "--build-arg", "BASE_IMAGE=" + base,
             "--build-arg", "MDD_VERSION=" + args.version]
    if args.debian_mirror:
        build += ["--build-arg", "DEBIAN_MIRROR=" + args.debian_mirror]
    if args.debian_security_mirror:
        build += ["--build-arg", "DEBIAN_SECURITY_MIRROR=" + args.debian_security_mirror]
    build += ["-f", "runtime/Dockerfile.hardware-overlay", "-t", args.tag, "-"]
    subprocess.run(ssh + [shlex.join(build)], input=payload.getvalue(), check=True)


if __name__ == "__main__":
    main()
