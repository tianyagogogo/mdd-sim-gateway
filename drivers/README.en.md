# NAS compatibility and driver catalogue

[中文](README.md)

This directory records reviewable combinations of NAS hardware, operating-system builds, kernels
and USB devices. Each record answers two questions:

1. Does the host kernel create every required device node after the USB device is inserted
   (`host-native`)?
2. If it does not, is there a driver package that exactly matches the model, OS build, platform,
   architecture and kernel release?

Compatibility records live in [`catalog/`](catalog/). Each JSON file describes one exact system
combination. Validate a contribution with:

```bash
python3 tools/validate_nas_catalog.py
```

You may also submit the GitHub **NAS hardware compatibility** issue template for a maintainer to
turn into a catalogue entry.

## Accepted information

- NAS vendor and exact model;
- CPU platform and architecture;
- OS version and complete build number;
- `uname -r` output;
- USB VID:PID, device class, and whether `ttyUSB`, `ttyACM`, `cdc-wdm`, `wwan` and
  `/dev/bus/usb` nodes appear;
- deployment version and the features that were actually verified.

Do not submit serial numbers, MAC or public addresses, usernames, IMSI, ICCID, IMEI, EID, phone
numbers, SIM PINs, subscription URLs, tokens, keys or unreviewed full logs. `lsusb -v` commonly
contains serial numbers and must not be pasted without redaction.

## Driver admission

A compatibility report is not a driver package. Unknown `.ko`, `.spk` or other binaries are not
accepted into a Release. A formal driver asset must be reproducibly built with the vendor's exact
toolchain and matching kernel sources, with all of the following reviewed:

- the complete vendor/model/platform/architecture/OS-build/kernel-release compatibility key;
- module names, load order, architecture, `vermagic` and SHA-256 for every file;
- source, patches, toolchain provenance and licences;
- installation, boot loading, OS-update mismatch protection and removal behaviour.

| State | Meaning |
| --- | --- |
| `reported` | Community report not yet reproduced by a maintainer |
| `host-native` | The host creates all required nodes without an MDD driver |
| `driver-verified` | An exact driver was verified on hardware; release packaging may be pending |
| `release-ready` | A checksummed formal driver asset is attached to a Release |
| `unsupported` | The exact combination is known to be unsafe or unsupported, with a reason |

Only `release-ready` records may be presented by the product as installable drivers. Other states
are diagnostic and evidence-collection records.

## Published driver packs

| Combination | Release asset |
| --- | --- |
| DS1621+ / DSM 7.4.1-90080 / 4.4.302+ / x86_64 | `mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64.tar.gz` |

Every build input is pinned in `runtime/synology-v1000-7.4-modules.json`; CI rebuilds the pack with `tools/drivers/build-synology-pack.sh` and fails if any module differs from the one validated on hardware. Installation is described in section 2 of the [full container deployment guide](../docs/CONTAINER_DEPLOYMENT.en.md).
