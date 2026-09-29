#!/usr/bin/env python3
"""Prepare verified runtime binaries and wheels locally, without building on NAS.

Versions and binary archive checksums are read from install.sh, so the container
uses the same reviewed dependencies as the existing installation path.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def pinned_value(source, name):
    match = re.search(r'^' + re.escape(name) + r'="([^"\n]+)"', source, re.M)
    if not match:
        raise ValueError("missing installer pin: " + name)
    value = match.group(1)
    if value.startswith("${"):
        value = value.split(":-", 1)[1].removesuffix("}")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    source = (ROOT / "install.sh").read_text()
    destination = ROOT / "runtime" / "vendor" / args.arch
    bins, wheels = destination / "bin", destination / "wheels"
    bins.mkdir(parents=True, exist_ok=True)
    wheels.mkdir(parents=True, exist_ok=True)
    manifest = {"arch": args.arch, "tools": {}}
    for tool, prefix in (("sing-box", "SINGBOX"), ("xray", "XRAY")):
        version = pinned_value(source, prefix + "_VERSION")
        digest = pinned_value(source, prefix + "_SHA256_" + args.arch.upper())
        if tool == "sing-box":
            archive = f"sing-box-{version}-linux-{args.arch}.tar.gz"
            url = f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{archive}"
        else:
            archive = "Xray-linux-64.zip" if args.arch == "amd64" else "Xray-linux-arm64-v8a.zip"
            url = f"https://github.com/XTLS/Xray-core/releases/download/v{version}/{archive}"
        with urllib.request.urlopen(url, timeout=120) as response:
            blob = response.read()
        if hashlib.sha256(blob).hexdigest() != digest:
            raise ValueError("archive checksum mismatch: " + tool)
        if tool == "sing-box":
            with tarfile.open(fileobj=io.BytesIO(blob)) as stream:
                member = stream.extractfile(f"sing-box-{version}-linux-{args.arch}/sing-box")
                binary = member.read()
        else:
            with zipfile.ZipFile(io.BytesIO(blob)) as stream:
                binary = stream.read("xray")
        path = bins / tool
        path.write_bytes(binary)
        path.chmod(0o755)
        manifest["tools"][tool] = {"version": version, "archive_sha256": digest,
                                   "binary_sha256": hashlib.sha256(binary).hexdigest()}
        print(f"verified {tool} {version} ({args.arch})", flush=True)
    platform = "manylinux2014_x86_64" if args.arch == "amd64" else "manylinux2014_aarch64"
    subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                    "--platform", platform, "--python-version", "312", "--implementation", "cp",
                    "--abi", "cp312", "--dest", str(wheels), "PyYAML==6.0.3"], check=True)
    manifest["wheels"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in wheels.glob("*.whl")}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
