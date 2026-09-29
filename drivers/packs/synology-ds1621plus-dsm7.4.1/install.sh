#!/bin/sh
# Install the MDD cellular modem kernel modules on the exact DSM build they were built for.
# Run from the unpacked driver pack:  sudo sh install.sh
set -eu
PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH

here=$(cd "$(dirname "$0")" && pwd)
module_dir=/usr/local/lib/mdd-sim-gateway-modules
boot_hook=/usr/local/etc/rc.d/mdd-sim-gateway-modules.sh

[ "$(id -u)" = 0 ] || { echo "Run as root: sudo sh install.sh" >&2; exit 1; }

# Refuse before touching anything. A module built for another kernel can crash it, so the
# same checks the boot hook repeats on every start are made here first.
. "$here/compatibility.env"
fail() { echo "Not installed: $1" >&2; exit 1; }
[ "$(uname -m)" = "$MDD_ARCH" ] || fail "this pack is for $MDD_ARCH, the host is $(uname -m)"
[ "$(uname -r)" = "$MDD_KERNEL" ] || fail "this pack is for kernel $MDD_KERNEL, the host runs $(uname -r)"
[ -r /etc.defaults/VERSION ] || fail "this is not Synology DSM"
product=$(sed -n 's/^productversion="\(.*\)"/\1/p' /etc.defaults/VERSION)
build=$(sed -n 's/^buildnumber="\(.*\)"/\1/p' /etc.defaults/VERSION)
[ "$product-$build" = "$MDD_DSM" ] || fail "this pack is for DSM $MDD_DSM, the host runs $product-$build"
uname -a | grep -q "$MDD_PLATFORM_MARKER" || fail "this pack is for $MDD_PLATFORM_MARKER"
(cd "$here" && sha256sum -c SHA256SUMS >/dev/null) || fail "a module does not match SHA256SUMS"

install -d -m 755 "$module_dir"
while read -r _digest name; do
    install -m 644 "$here/$name" "$module_dir/$name"
done < "$here/SHA256SUMS"
for file in SHA256SUMS compatibility.env manifest.json; do
    install -m 644 "$here/$file" "$module_dir/$file"
done
install -d -m 755 "$(dirname "$boot_hook")"
install -m 755 "$here/loader.sh" "$boot_hook"

# Loads only what is not loaded yet, so re-running the installer is harmless.
"$boot_hook" start
"$boot_hook" status
echo "MDD cellular modules installed; they will load again at every boot after the same checks."
