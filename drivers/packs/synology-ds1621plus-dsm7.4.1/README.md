# MDD Sim Gateway modem drivers — Synology DS1621+, DSM 7.4.1-90080

This pack adds the five kernel modules the DS1621+ kernel lacks for Quectel EC25-class modems:
`mii`, `cdc-wdm`, `qmi_wwan`, `usb_wwan` and `option`. It is only for **DS1621+ (v1000),
DSM 7.4.1-90080, kernel 4.4.302+, x86_64**. The installer and the boot hook refuse any other
combination; never load these modules on another model or DSM build.

## Install

```sh
tar -xzf mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64.tar.gz
cd mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64
sudo sh install.sh
```

Verify the tarball against the Release `SHA256SUMS` first. The installer copies the modules to
`/usr/local/lib/mdd-sim-gateway-modules`, installs a boot hook in `/usr/local/etc/rc.d`, and loads
the modules. At every boot the hook checks the architecture, kernel, DSM build, platform and every
module checksum again, and loads nothing if any of them changed (for example after a DSM update).
Re-running the installer is harmless.

## Remove

```sh
sudo sh uninstall.sh
```

Modules that are in use stay loaded until the next reboot.

## Source and licence

The modules are licensed under the GNU General Public License version 2 (`COPYING`). They are built
from **unmodified** Linux v4.4.302 source files, included in `source/` together with the Makefile,
using Synology's public v1000 DSM 7.4 toolkit. `manifest.json` pins every input by SHA-256 and records
the exact build command. The project's release workflow rebuilds this pack from those inputs and
fails unless every module matches the checksum of the modules validated on hardware.

---

# 中文说明

本驱动包为 DS1621+ 补充移远 EC25 类模块所需、而群晖内核缺失的 5 个内核模块。**仅适用于 DS1621+（v1000）、
DSM 7.4.1-90080、内核 4.4.302+、x86_64**，安装脚本和开机加载脚本都会拒绝其他组合。

安装：解压后在目录中执行 `sudo sh install.sh`（先用 Release 的 `SHA256SUMS` 校验压缩包）。之后每次开机都会重新
核对架构、内核、DSM 版本、平台和模块校验值，任一不符（例如 DSM 升级后）就不加载。卸载：`sudo sh uninstall.sh`。

模块采用 GPL-2.0 许可（见 `COPYING`），由 `source/` 中**未经修改**的 Linux v4.4.302 源码和群晖公开的
v1000 DSM 7.4 工具链构建，所有输入都在 `manifest.json` 中以 SHA-256 固定。
