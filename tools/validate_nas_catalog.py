#!/usr/bin/env python3
"""Validate the dependency-free subset of the public NAS compatibility catalog."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "drivers" / "catalog"
STATUSES = {"reported", "host-native", "driver-verified", "release-ready", "unsupported"}
PACK_STATUSES = {"not-needed", "packaging-pending", "published", "unavailable"}
ARCHITECTURES = {"x86_64", "aarch64"}
NODE_KINDS = {"ttyUSB", "ttyACM", "cdc-wdm", "wwan", "usb-bus"}
USB_KINDS = {"cellular-modem", "pcsc-reader", "other"}
USB_RESULTS = {"working", "partial", "not-working"}
FORBIDDEN_KEYS = {
    "serial", "serial_number", "mac", "mac_address", "imei", "imsi", "iccid", "eid",
    "phone", "phone_number", "pin", "token", "subscription_url", "private_key",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def validate(path: Path) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    require(record.get("schema") == 1, f"{path}: schema must be 1")
    identifier = record.get("id")
    require(isinstance(identifier, str) and re.fullmatch(r"[a-z0-9][a-z0-9._+-]*", identifier),
            f"{path}: invalid id")
    require(path.stem == identifier, f"{path}: filename must equal id")
    require(record.get("architecture") in ARCHITECTURES, f"{path}: invalid architecture")
    require(record.get("validation", {}).get("status") in STATUSES,
            f"{path}: invalid validation status")
    dt.date.fromisoformat(record.get("validation", {}).get("date", ""))
    require(record.get("driver_pack", {}).get("status") in PACK_STATUSES,
            f"{path}: invalid driver pack status")
    require(set(record.get("expected_nodes", [])) <= NODE_KINDS,
            f"{path}: invalid expected node")
    for device in record.get("tested_usb", []):
        require(re.fullmatch(r"[0-9a-f]{4}", device.get("vid", "")) is not None,
                f"{path}: invalid USB VID")
        require(re.fullmatch(r"[0-9a-f]{4}", device.get("pid", "")) is not None,
                f"{path}: invalid USB PID")
        require(device.get("kind") in USB_KINDS, f"{path}: invalid USB kind")
        require(device.get("result") in USB_RESULTS, f"{path}: invalid USB result")
    forbidden = FORBIDDEN_KEYS.intersection(key.lower() for key in walk_keys(record))
    require(not forbidden, f"{path}: forbidden sensitive keys: {', '.join(sorted(forbidden))}")
    pack = record["driver_pack"]
    if record["validation"]["status"] == "release-ready":
        require(pack["status"] == "published", f"{path}: release-ready requires a published pack")
    if pack["status"] == "published":
        require(bool(pack.get("asset")), f"{path}: published pack has no asset")
        require(re.fullmatch(r"[0-9a-f]{64}", pack.get("sha256") or "") is not None,
                f"{path}: published pack has no valid SHA-256")


def main() -> None:
    paths = sorted(CATALOG.glob("*.json"))
    paths = [path for path in paths if path.name != "schema.json"]
    require(bool(paths), "NAS compatibility catalog is empty")
    for path in paths:
        validate(path)
    print(f"validated {len(paths)} NAS compatibility record(s)")


if __name__ == "__main__":
    main()
