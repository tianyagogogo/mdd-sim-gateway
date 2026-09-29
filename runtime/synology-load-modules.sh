#!/bin/sh
# DSM boot hook for the exact, pre-verified modem modules installed beside this script.
set -eu
PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH

module_dir=/usr/local/lib/mdd-sim-gateway-modules
compat="$module_dir/compatibility.env"
checksums="$module_dir/SHA256SUMS"

validate() {
    [ -r "$compat" ] && [ -r "$checksums" ] || {
        echo "MDD cellular module metadata is missing" >&2
        return 1
    }
    # This file is generated from the repository manifest and installed root-owned.
    . "$compat"
    [ "$(uname -m)" = "$MDD_ARCH" ] || {
        echo "MDD cellular modules: architecture mismatch" >&2; return 1; }
    [ "$(uname -r)" = "$MDD_KERNEL" ] || {
        echo "MDD cellular modules: kernel mismatch" >&2; return 1; }
    product=$(sed -n 's/^productversion="\(.*\)"/\1/p' /etc.defaults/VERSION)
    build=$(sed -n 's/^buildnumber="\(.*\)"/\1/p' /etc.defaults/VERSION)
    [ "$product-$build" = "$MDD_DSM" ] || {
        echo "MDD cellular modules: DSM version mismatch" >&2; return 1; }
    uname -a | grep -q "$MDD_PLATFORM_MARKER" || {
        echo "MDD cellular modules: Synology platform mismatch" >&2; return 1; }
    (cd "$module_dir" && sha256sum -c SHA256SUMS >/dev/null)
}

loaded() {
    grep -q "^$1 " /proc/modules
}

load_custom() {
    name=$1
    file=${2:-$1.ko}
    loaded "$name" || insmod "$module_dir/$file"
}

start() {
    validate
    load_custom mii
    modprobe usbnet
    load_custom cdc_wdm cdc-wdm.ko
    load_custom qmi_wwan
    modprobe usbserial
    load_custom usb_wwan
    load_custom option
}

stop() {
    # Never unload DSM's usbnet/usbserial. Custom modules are removed only when idle.
    for name in option usb_wwan qmi_wwan cdc_wdm mii; do
        loaded "$name" && rmmod "$name" || true
    done
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status)
        validate
        for name in mii cdc_wdm qmi_wwan usb_wwan option; do loaded "$name" || exit 1; done
        ;;
    *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
