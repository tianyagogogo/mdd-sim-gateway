#!/bin/sh
# Run on the Docker host, directly or through SSH stdin. Read-only: does not
# open modem ports, send AT commands, load modules, alter rules or restart services.
# Reports topology and VID/PID, never USB serial numbers or SIM identifiers.
set -eu
printf 'Architecture: '; uname -m
printf 'Kernel: '; uname -r
if [ -c /dev/net/tun ]; then
    printf 'TUN device: present\n'
else
    printf 'TUN device: missing\n'
fi
printf '\nUSB devices (port, VID:PID, product):\n'
for device in /sys/bus/usb/devices/*; do
    [ -f "$device/idVendor" ] || continue
    printf '%s %s:%s ' "${device##*/}" "$(cat "$device/idVendor")" "$(cat "$device/idProduct")"
    if [ -f "$device/product" ]; then cat "$device/product"; else printf '\n'; fi
done
printf '\nUSB interface bindings (unbound interfaces cannot expose kernel modem ports):\n'
for interface in /sys/bus/usb/devices/*:*; do
    [ -f "$interface/bInterfaceClass" ] || continue
    driver=unbound
    if [ -L "$interface/driver" ]; then
        driver=$(readlink "$interface/driver")
        driver=${driver##*/}
    fi
    printf '%s class=%s subclass=%s protocol=%s driver=%s\n' \
        "${interface##*/}" "$(cat "$interface/bInterfaceClass")" \
        "$(cat "$interface/bInterfaceSubClass")" "$(cat "$interface/bInterfaceProtocol")" "$driver"
done
printf '\nModem port candidates (not proof of supported hardware):\n'
found=0
for port in /sys/class/tty/ttyUSB* /sys/class/tty/ttyACM* /sys/class/usbmisc/cdc-wdm* /sys/class/wwan/*; do
    [ -e "$port" ] || continue
    found=1
    name=${port##*/}
    printf '%s sysfs=%s\n' "$name" "$(readlink -f "$port")"
    if [ -e "/dev/$name" ]; then
        ls -l "/dev/$name"
    else
        printf '  device node missing\n'
    fi
done
[ "$found" = 1 ] || printf 'No serial/QMI/MBIM port candidates found.\n'
printf '\nRelevant loaded drivers:\n'
if [ -r /proc/modules ]; then
    awk '$1 ~ /^(tun|usbserial|usb_wwan|option|qmi_wwan|cdc_wdm|cdc_mbim|cdc_ncm|cdc_acm)$/ {print $1}' /proc/modules
fi
printf '\nAvailable driver files (presence alone does not prove compatibility):\n'
for directory in /lib/modules /usr/lib/modules; do
    [ -d "$directory" ] || continue
    find "$directory" -type f \( -name 'usbserial.ko*' -o -name 'usb_wwan.ko*' \
        -o -name 'option.ko*' -o -name 'qmi_wwan.ko*' -o -name 'cdc_wdm.ko*' \
        -o -name 'cdc_mbim.ko*' -o -name 'cdc_ncm.ko*' -o -name 'cdc_acm.ko*' \)
done
printf '\nPotential device owners (PID only, not stopped by this probe):\n'
if command -v pgrep >/dev/null 2>&1; then
    for service in ModemManager NetworkManager pcscd; do
        printf '%s: ' "$service"
        pgrep -x "$service" || true
        printf '\n'
    done
fi
printf '\nMissing ports or unloaded drivers require investigation; USB enumeration alone is insufficient.\n'
