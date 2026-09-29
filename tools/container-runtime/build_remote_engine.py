#!/usr/bin/env python3
"""Build the current Engine runtime overlay on a remote Docker host.

The released Engine supplies the compiled telephony stack.  This sends only the
runtime-owned files, pins FROM to the inspected local image ID and builds offline.
"""
import argparse
import io
from pathlib import Path
import shlex
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]
ENGINE_FILES = (
    "Dockerfile.overlay", "pin_keeper.py", "ami_usim.py", "swu_ike.py",
    "outer_transport.py", "log_capture.py", "render.py", "notify.py",
    "templates", "entrypoint.sh",
)


def fingerprint(kind):
    return subprocess.check_output(
        [str(ROOT / "tools" / "engine-fingerprint.sh"), kind], text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--base-image", default="ghcr.io/mddidd/mdd-sim-gateway-engine:v1.11.0")
    parser.add_argument("--tag", default="mdd-sim-gateway/engine:dev")
    parser.add_argument("--version", default="dev")
    args = parser.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]
    inspect = ["sudo", "-n", "/usr/local/bin/docker", "image", "inspect", args.base_image,
               "--format", "{{.Id}} {{.Architecture}}"]
    base, arch = subprocess.check_output(ssh + [shlex.join(inspect)], text=True).strip().split()
    if arch not in {"amd64", "arm64"}:
        raise SystemExit(f"unsupported Engine architecture: {arch}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in ENGINE_FILES:
            archive.add(ROOT / "engine" / name, arcname=name)
    build = ["sudo", "-n", "/usr/local/bin/docker", "build", "--network", "none",
             "--build-arg", "BASE_IMAGE=" + base,
             "--build-arg", "RUNTIME_FP=" + fingerprint("runtime"),
             "--build-arg", "BASE_FP=" + fingerprint("base"),
             "--build-arg", "MDD_VERSION=" + args.version,
             "-f", "Dockerfile.overlay", "-t", args.tag, "-"]
    subprocess.run(ssh + [shlex.join(build)], input=payload.getvalue(), check=True)


if __name__ == "__main__":
    main()
