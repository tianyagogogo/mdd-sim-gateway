#!/usr/bin/env python3
"""Build the experimental egress image remotely from a small, explicit context.

Only code and prepared artifacts are sent, never repository data or credentials.
The build has no network; BASE_IMAGE must already exist on the Docker host.
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
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--base-image", required=True, help="Locally available Python 3.12 glibc image")
    parser.add_argument("--tag", default="mdd-sim-gateway/egress:dev")
    parser.add_argument("--version", default="dev")
    args = parser.parse_args()
    artifacts = ROOT / "runtime" / "vendor" / args.arch
    if not (artifacts / "manifest.json").is_file():
        raise SystemExit("run prepare_egress_assets.py first")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=6",
           "-i", args.identity, "-p", str(args.port), args.host]
    inspect = ["sudo", "-n", "/usr/local/bin/docker", "image", "inspect", args.base_image,
               "--format", "{{.Id}} {{.Architecture}}"]
    base, arch = subprocess.check_output(ssh + [shlex.join(inspect)], text=True).strip().split()
    if arch != args.arch:
        raise SystemExit("base image architecture does not match prepared artifacts")
    payload = io.BytesIO()
    # gzip + GNU tar is recognised by DSM's legacy Docker builder; Python's
    # uncompressed PAX stream may be misdetected as a plain Dockerfile.
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in ("runtime/Dockerfile.egress", "runtime/egress.py", "host/mdd_orchestrator.py",
                     "control/app/egress_contract.py",
                     "runtime/vendor/" + args.arch):
            archive.add(ROOT / name, arcname=name)
    build = ["sudo", "-n", "/usr/local/bin/docker", "build", "--network", "none",
             "--build-arg", "BASE_IMAGE=" + base, "--build-arg", "TARGETARCH=" + args.arch,
             "--build-arg", "MDD_VERSION=" + args.version,
             "-f", "runtime/Dockerfile.egress", "-t", args.tag, "-"]
    subprocess.run(ssh + [shlex.join(build)], input=payload.getvalue(), check=True)


if __name__ == "__main__":
    main()
