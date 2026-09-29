#!/usr/bin/env python3
"""Build the current Control code as an offline overlay on a remote Docker host."""
import argparse
import io
from pathlib import Path
import shlex
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[2]


def include(member):
    parts = Path(member.name).parts
    if "__pycache__" in parts or member.name.endswith((".pyc", ".pyo")):
        return None
    return member


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--base-image", default="mdd-sim-gateway/control:v1.11.0")
    parser.add_argument("--tag", default="mdd-sim-gateway/control:dev")
    parser.add_argument("--version", default="dev")
    args = parser.parse_args()

    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=6", "-i", args.identity, "-p", str(args.port), args.host]
    inspect = ["sudo", "-n", "/usr/local/bin/docker", "image", "inspect", args.base_image,
               "--format", "{{.Id}} {{.Architecture}}"]
    base, arch = subprocess.check_output(ssh + [shlex.join(inspect)], text=True).strip().split()
    if arch not in {"amd64", "arm64"}:
        raise SystemExit(f"unsupported Control architecture: {arch}")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in ("runtime/Dockerfile.control-overlay", "control/app", "control/run.py",
                     "control/requirements.txt", "host/mdd_update.py",
                     "host/mdd_container_update.py", "webui/dist",
                     "patches/lpac/01_pcsc_reader_selection.patch", "VERSION"):
            archive.add(ROOT / name, arcname=name, filter=include)
    build = ["sudo", "-n", "/usr/local/bin/docker", "build", "--network", "bridge",
             "--build-arg", "BASE_IMAGE=" + base,
             "--build-arg", "MDD_VERSION=" + args.version,
             "-f", "runtime/Dockerfile.control-overlay", "-t", args.tag, "-"]
    subprocess.run(ssh + [shlex.join(build)], input=payload.getvalue(), check=True)


if __name__ == "__main__":
    main()
