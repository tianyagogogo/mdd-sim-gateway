#!/bin/sh
# Remove the boot hook and module files. Modules in use stay loaded until the next reboot:
# unplug the modem or stop the MDD containers first to unload them now.
set -eu
PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo sh uninstall.sh" >&2; exit 1; }
boot_hook=/usr/local/etc/rc.d/mdd-sim-gateway-modules.sh
[ -x "$boot_hook" ] && "$boot_hook" stop || true
rm -f "$boot_hook"
rm -rf /usr/local/lib/mdd-sim-gateway-modules
echo "MDD cellular modules removed. Any still loaded are released at the next reboot."
