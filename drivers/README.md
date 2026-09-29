# NAS 兼容性与驱动目录

[English](README.en.md)

这个目录收集可以复核的 NAS 硬件、系统、内核与 USB 设备兼容性记录。记录用于回答两件事：

1. 该系统插入设备后是否由宿主内核直接生成所需设备节点（`host-native`）；
2. 如果没有，项目是否已有与型号、系统 build、平台、架构和内核完全匹配的驱动包。

兼容性记录位于 [`catalog/`](catalog/)，每个 JSON 文件只描述一个精确系统组合。提交前运行：

```bash
python3 tools/validate_nas_catalog.py
```

也可以使用 GitHub 的 “NAS hardware compatibility” Issue 模板提交信息，由维护者整理成记录。

## 接受的信息

- NAS 厂商和准确型号；
- CPU 平台与架构；
- 系统版本和完整 build；
- `uname -r` 输出；
- USB VID:PID、设备类别以及是否生成 `ttyUSB`、`ttyACM`、`cdc-wdm`、`wwan`、
  `/dev/bus/usb` 等节点；
- 使用的部署版本和经过验证的功能。

不要提交序列号、MAC 地址、公网地址、用户名、IMSI、ICCID、IMEI、EID、电话号码、SIM PIN、
订阅 URL、Token、密钥或未经人工检查的完整日志。`lsusb -v` 经常包含序列号，不应直接粘贴。

## 驱动包准入

兼容性报告不等于驱动包。项目不接受来源不明的 `.ko`、`.spk` 或其他二进制文件直接通过 PR
进入 Release。正式驱动包必须由维护者使用可复现的厂商工具链和对应内核源码构建，并核对：

- 精确兼容键：厂商、型号、平台、架构、系统 build、内核 release；
- 模块名、加载顺序、架构、`vermagic` 和每个文件的 SHA-256；
- 源码、补丁、工具链来源和许可证；
- 安装、开机加载、系统升级失配保护和卸载行为。

目录状态含义：

| 状态 | 含义 |
| --- | --- |
| `reported` | 社区报告，尚未由维护者复现 |
| `host-native` | 宿主无需项目驱动即可生成所需节点 |
| `driver-verified` | 精确驱动已在实机验证，发布包可能仍在制作 |
| `release-ready` | 对应 Release 已提供经过校验的正式驱动资产 |
| `unsupported` | 已确认当前组合无法安全支持，并记录原因 |

只有 `release-ready` 记录可以被产品界面展示为可安装驱动。其他状态只能用于诊断和收集信息。

## 已发布的驱动包

| 组合 | Release 资产 |
| --- | --- |
| DS1621+ / DSM 7.4.1-90080 / 4.4.302+ / x86_64 | `mdd-driver-synology-ds1621plus-dsm7.4.1-90080-k4.4.302plus-x86_64.tar.gz` |

驱动包的构建输入全部固定在 `runtime/synology-v1000-7.4-modules.json`，由 `tools/drivers/build-synology-pack.sh` 在 CI 中重新构建；任一模块与实机验证值不一致即构建失败。安装步骤见[全容器部署指南](../docs/CONTAINER_DEPLOYMENT.md)第 2 节。
