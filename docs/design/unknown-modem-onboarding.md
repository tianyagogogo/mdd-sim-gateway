# 未识别模块的探测与添加

状态：已实现（本 PR），依赖 PR #187（内置型号与保存列表合并）。

## 背景

模块靠 `settings.hardware.modem_profiles` 里的 `vid/pid/at_interface` 识别，清单外的设备
被静默丢弃（`host/mdd_orchestrator.py` `usb_modems`、`runtime/hardware.py`
`discover_modems`）。Discussion #104 的用户为了用经典 EC20 只能手改配置文件。

目标：设备页列出「像模块但不认识」的 USB 设备，用户点一下，由宿主机侧自动找出 AT 口、
验证 SIM 访问能力，通过后保存为自定义型号。全程不要求用户知道 VID/PID/接口号，
也不按品牌预设。

## 能力判定：只看标准指令

SIM 桥（`host/vpcd_modem_bridge.py`）访问 SIM 只用 3GPP TS 27.007 的 `AT+CSIM`：
逻辑通道的开/关是经 CSIM 发送的 MANAGE CHANNEL APDU（`0070000001` / `007080xx00`），
不使用 `AT+CCHO`/`AT+CGLA`/`AT+CRSM`，也不使用任何 `AT+Q...` 私有指令。
身份读取用 `AT+CGSN`/`AT+GSN` 与 `AT+CCID`/`AT+ICCID`。

因此判定标准与品牌无关：

| 结果 | 条件 | 处理 |
| --- | --- | --- |
| 可用 | 找到应答 `AT` 的口，且插卡时 `AT+CSIM` 能打开并关闭一个逻辑通道 | 保存为自定义型号 |
| 待验证 | AT 口可用、`AT+CSIM=?` 或 `AT+CSIM` 有正常响应，但未插卡，无法验证逻辑通道 | 允许保存，标注「插卡后自动复核」 |
| 不可用 | 没有口应答 `AT`，或 `AT+CSIM` 返回 ERROR | 不保存，说明原因 |

移远专属能力（彩信 `AT+QI*`、通话音频 `AT+QPCMV`、IMS 开关 `AT+QCFG="ims"`）
本来就是按指令能否应答降级的，不按 VID 判断，无需改动；页面对自定义型号说明
「保证 VoWiFi 与短信，彩信/通话录音/IMS 开关视模块而定」。

## 候选设备

由宿主机侧（local：orchestrator；容器：Hardware）在 `/sys/bus/usb/devices` 枚举，
与现有发现逻辑同一遍完成，结果写入新文件 `orchestrator/usb-candidates.json`：

- 排除已在 `modem_profiles` 中的 vid/pid；排除 hub 与无 tty 接口的设备。
- 只列满足任一条件的设备，避免把 USB 转串口线、GPS、UPS 当成模块：
  - ModemManager 已为它建立 modem 对象；
  - 至少有 2 个带 `ttyUSB*`/`ttyACM*` 的接口。
- 每项字段：`vid`、`pid`、`usb_path`、`manufacturer`/`product`（sysfs 字符串）、
  `interfaces`（接口号 → tty 名）、`mm_object`（如有）、`mm_at_ports`（MM 报告的 AT 口）。

Control 看不到 `/sys`，只读这个文件；`GET /api/devices` 追加 `unrecognized_usb` 字段。

## 探测流程

沿用 bridge-restart 的请求/状态文件模式：

1. Control 写 `orchestrator/modem-probe-requests/<id>.json`
   （`{request_id, usb_path, vid, pid, requested_at}`，0600，原子写）。
2. 宿主机侧在巡检开头处理（local 加入 `_input_mtimes`，容器加入 `wake_signature`），
   写 `orchestrator/modem-probe-status/<id>.json`：`probing` → `done`/`failed`，
   带 `result`（可用/待验证/不可用）、`at_interface`、`evidence`（逐步指令与响应摘要）。
3. Control 轮询状态（同 `_esim_restart_modem_bridge`），60 秒超时。

找 AT 口，按优先级：

- **ModemManager 已建对象**：取 `mmcli -m <obj>` 的 AT 端口，经 sysfs 反查接口号；
  所有指令走 `mmcli -m <obj> --command=`，与 SIM 桥的 MM 模式一致，不与 MM 抢口。
  容器模式 MM 始终运行，走这一条。
- **无 MM 对象**（local 串口模式、或 MM 已放弃该设备）：按接口号从小到大逐个以
  `exclusive=True` 打开 tty，发 `AT`，1 秒超时；`EBUSY` 视为被占用并在结果中注明。
  只在用户点击后执行，从不自动探测陌生串口。

验证 SIM：`ATE0`、`AT+CMEE=2`；`AT+CSIM=10,"0070000001"` 打开通道，成功则立即
`AT+CSIM=10,"007080xx00"` 关闭（与桥的 EC25 兼容写法一致）。探测结束后不留任何通道。

## 保存与后续

- 「可用」「待验证」经 `PUT /api/settings` 写入 `modem_profiles`：
  `{vid, pid, at_interface, name: product 字符串, source: "probe", verified: bool}`。
  现有巡检随即按新型号创建读卡通道，无需重启。
- 「待验证」不回写配置：设备页显示时，只要该型号设备的桥已建好逻辑通道（`channel_status == ready`）即视为已验证。
- 设备页对 `source: probe` 的型号显示「自定义型号（实验性）」，提供「移除」。
  内置型号不可移除（合并逻辑会补回）。
- 高级选项：可手动改 `at_interface` 后重新探测。

## 界面

设备页设备列表下方，仅当 `unrecognized_usb` 非空时显示一块：

> 发现 1 个未识别的 USB 设备
> Qualcomm Android（`05c6:9215`，USB 3-2）　[试用这个设备]

点击后显示进度和结果；不可用时给出具体原因（没有 AT 口 / 不支持 CSIM / 端口被占用），
并提示可上传诊断包反馈。探测结果与 `evidence` 进入诊断包，便于判断是否值得内置。

## 测试

- 候选筛选：已知型号、单口串口线、hub 被排除；MM 对象或多 tty 设备入选。
- 探测：MM 路径与直连路径各覆盖可用/待验证/不可用；`EBUSY`；超时。
- 探测后不残留逻辑通道（打开成功必关闭）。
- 保存后两种运行模式都能识别新型号；移除后不再识别；内置型号不可移除。

## 不做

- 不按品牌预置参数表。
- 不自动探测、不自动保存。
- 不为非移远模块适配彩信/通话音频/IMS 开关。
