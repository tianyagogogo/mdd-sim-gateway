#!/bin/sh
# Rebuild the DS1621+ modem driver pack from its public inputs and package it reproducibly.
#
# Every input is pinned in runtime/synology-v1000-7.4-modules.json: Synology's v1000 DSM 7.4
# toolkit and six unmodified Linux v4.4.302 source files. The modules are built at the same
# paths as the ones validated on hardware (those paths end up in the debug info), and the
# build fails unless every .ko matches the checksum recorded for the validated modules.
# The tarball is written with fixed ordering, owners and times, so its own SHA-256 is stable
# and the compatibility catalogue can pin it.
#
# Usage (Linux x86_64, root or passwordless sudo for /work):  tools/drivers/build-synology-pack.sh [outdir]
set -eu

repo=$(cd "$(dirname "$0")/../.." && pwd)
manifest="$repo/runtime/synology-v1000-7.4-modules.json"
pack_src="$repo/drivers/packs/synology-ds1621plus-dsm7.4.1"
out=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
cache=${MDD_DRIVER_CACHE:-$repo/.driver-cache}
mkdir -p "$cache"

j() { python3 -c "import json,sys; d=json.load(open('$manifest')); print(eval(sys.argv[1]))" "$1"; }
verify() {  # file sha256
    actual=$(sha256sum "$1" | cut -d' ' -f1)
    [ "$actual" = "$2" ] || { echo "checksum mismatch: $1 is $actual, expected $2" >&2; exit 1; }
}

fetch() {  # url file sha256
    [ -f "$2" ] && [ "$(sha256sum "$2" | cut -d' ' -f1)" = "$3" ] && return 0
    curl -fsSL --retry 3 -o "$2.tmp" "$1"
    verify "$2.tmp" "$3"
    mv "$2.tmp" "$2"
}

dev="$cache/ds.v1000-7.4.dev.txz"
env="$cache/ds.v1000-7.4.env.txz"
fetch "$(j 'd["inputs"]["synology_dev"]["url"]')" "$dev" "$(j 'd["inputs"]["synology_dev"]["sha256"]')"
fetch "$(j 'd["inputs"]["synology_env"]["url"]')" "$env" "$(j 'd["inputs"]["synology_env"]["sha256"]')"

toolchain=$(j 'd["build"]["toolchain_root"]')
kbuild=$(j 'd["build"]["kernel_build_dir"]')
moddir=$(j 'd["build"]["module_dir"]')
work=$(dirname "$moddir")
SUDO=; [ "$(id -u)" = 0 ] || SUDO=sudo
$SUDO rm -rf "$work"
$SUDO install -d -o "$(id -u)" -g "$(id -g)" "$work"
mkdir -p "$toolchain" "$moddir"

dev_dir=$(j 'd["inputs"]["synology_dev"]["kernel_build_dir"]')
env_dir=$(j 'd["inputs"]["synology_env"]["toolchain_dir"]')
tar -xJf "$dev" -C "$work" "$dev_dir"
mv "$work/$dev_dir" "$kbuild"
rm -rf "$work/usr"
tar -xJf "$env" -C "$toolchain" "$env_dir"

base=$(j 'd["inputs"]["linux_source"]["base_url"]')
tag=$(j 'd["inputs"]["linux_source"]["tag"]')
j 'chr(10).join(p + " " + s for p, s in d["inputs"]["linux_source"]["files"].items())' |
while read -r path digest; do
    fetch "$base$path?h=$tag" "$cache/$(basename "$path")" "$digest"
    cp "$cache/$(basename "$path")" "$moddir/"
done
cp "$pack_src/Makefile" "$moddir/Makefile"

PATH="$toolchain/$env_dir/bin:$PATH" \
    make -C "$kbuild" M="$moddir" ARCH=x86_64 CROSS_COMPILE=x86_64-pc-linux-gnu- modules

name=$(j 'd["pack"]["name"]')
stage="$work/pack/$name"
mkdir -p "$stage/source"
j 'chr(10).join(n + " " + s for n, s in sorted(d["modules"].items()))' |
while read -r module digest; do
    verify "$moddir/$module" "$digest"   # must reproduce the modules validated on hardware
    cp "$moddir/$module" "$stage/$module"
    printf '%s  %s\n' "$digest" "$module" >> "$stage/SHA256SUMS"
done
python3 - "$manifest" "$stage/compatibility.env" <<'PY'
import json, shlex, sys
m = json.load(open(sys.argv[1])); c = m["compatibility"]
open(sys.argv[2], "w").write("".join(f"{k}={shlex.quote(v)}\n" for k, v in (
    ("MDD_ARCH", c["architecture"]), ("MDD_KERNEL", c["kernel_release"]),
    ("MDD_DSM", c["dsm"]), ("MDD_PLATFORM_MARKER", m["pack"]["platform_marker"]))))
PY
cp "$manifest" "$stage/manifest.json"
cp "$repo/runtime/synology-load-modules.sh" "$stage/loader.sh"
cp "$pack_src/install.sh" "$pack_src/uninstall.sh" "$pack_src/README.md" "$stage/"
# GPL-2.0 corresponding source: the exact pinned files and Makefile the modules were built
# from, not the *.mod.c files the build generates beside them.
j 'chr(10).join(p.rsplit("/", 1)[-1] for p in d["inputs"]["linux_source"]["files"])' |
while read -r file; do cp "$cache/$file" "$stage/source/$file"; done
cp "$pack_src/Makefile" "$stage/source/Makefile"
fetch "${base}COPYING?h=$tag" "$cache/COPYING" "$(j 'd["inputs"]["linux_source"]["copying_sha256"]')"
cp "$cache/COPYING" "$stage/COPYING"
chmod 755 "$stage/install.sh" "$stage/uninstall.sh" "$stage/loader.sh"
find "$stage" -type f ! -name '*.sh' -exec chmod 644 {} +
find "$stage" -type d -exec chmod 755 {} +

tar --sort=name --mtime='1970-01-01 00:00:00Z' --owner=0 --group=0 --numeric-owner \
    --format=gnu -C "$work/pack" -cf - "$name" | gzip -n -9 > "$out/$name.tar.gz"
sha256sum "$out/$name.tar.gz"
