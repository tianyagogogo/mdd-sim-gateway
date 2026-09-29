# 发布检查清单

## 全容器版本启用门槛

- 按[全容器版本发布方案](design/container-release-plan.md)发布 Control、Hardware、Egress、
  Engine 的 amd64/arm64 镜像、离线镜像包、版本化 Compose 项目文件和统一 `SHA256SUMS`。
  当前 workflow 已生成四种镜像、八个离线镜像包和可导入容器管理器的 Compose YAML；
  DS1621+ 驱动包已接入 Release：CI 从固定输入重建，与实机验证过的模块逐字节核对，并纳入
  `SHA256SUMS`；驱动目录本身尚未作为 Release 资产发布。驱动包的输入变化时，兼容目录里固定的
  包 SHA-256 必须同步更新，否则 Driver packs 与 Release 工作流都会失败。
- 普通 Linux/Pi 以宿主已有设备节点完成零驱动安装；DS1621+ 分别验证已有节点和精确驱动包
  两条路径。缺少精确兼容项时必须报告 `driver_required`，不得尝试相近 DSM 或内核包。
- Hardware 镜像中的实体读卡器完成标准 CCID、HSIC `1d99:0016` 与 SCR Prime
  `04d9:c001` 验收；宿主不安装 pcscd/libccid，已有宿主 pcscd 占用设备时安全停止安装。
- 通过更新失败回滚、DSM 冷启动恢复、默认路由保持、NetworkManager 接管范围检查，再将
  RC 提升为正式版本。

## 代码与版本

- `VERSION`、WebUI `package.json` 与标签保持一致（例如 `1.0.0` / `v1.0.0`）。
- `CHANGELOG.md` 将目标版本从 `Unreleased` 改为发布日期。
- CI 的 Python 测试、WebUI 构建、生产依赖审计和脚本语法检查全部通过。
- Release 必须包含 Control、Hardware、Egress 与 Engine 的 `arm64`、`amd64` 八个镜像资产，
  且 `SHA256SUMS` 同时覆盖源码包和八个镜像。发布前分别用 `docker load` 验证架构、版本、
  组件和项目归属 label 与 `VERSION` 一致；不得只发源码包或漏发一个组件、架构。
- Release 工作流必须分别在原生 `ubuntu-24.04-arm` 与 `ubuntu-latest` runner 无缓存构建
  Engine，通过架构、各自的精确模块数（ARM64、amd64 均为 126）、Python 依赖和 Asterisk
  版本检查，并把架构标签合成为
  `ghcr.io/mddidd/mdd-sim-gateway-engine:vX.Y.Z` 多架构清单；package job 必须等待四种组件的
  两种架构资产均成功，并为 Control、Hardware、Egress、Engine 分别生成多架构清单。
- 依赖版本、源码提交与二进制 SHA-256 已复核；不得临时改成浮动分支或 `latest`。

## ARM64 与 amd64 实机验收

- 在目标 ARM64 设备通过一键更新下载本次 Engine Release 资产，确认直连失败或过慢时能
  切换到代理，核对架构、版本和源码指纹，并完成一次真实 SIM 线路重建与注册；编译由原生
  ARM64 CI 完成，设备本身不得重新编译镜像。另行抽查同版本 GHCR 镜像身份一致。
- 在 amd64 设备分别验证正式源码包全新安装和一键升级：只下载 `amd64` Engine，Docker
  控制面模式再下载 `amd64` Control；安装日志不得出现 Asterisk 或 Control 镜像源码构建，
  导入后归档被删除，旧 dangling build cache 被回收。抽查原生控制面模式不下载 Control。
- 必须另从仍运行 `v1.4.1` 旧升级器的 ARM64 设备直接升级到本版本，确认源码包内的一次性
  镜像接力清单生效：不要求先安装桥接版本，不直连 GHCR，也不能因旧升级器的
  `--no-engines` 参数留下旧 Engine。
- 在 amd64 + Docker 控制面的 `v1.4.x` 设备按安装文档执行一次模式标记引导后直接升级，确认
  旧升级器不再请求 ARM64 Control；目标安装器必须从在位容器识别 Docker 模式，沿原升级线路
  导入 amd64 Engine 与 Control，且成功后 `install-mode` 恢复为 `docker`。另验证接管前失败时
  可从 `install-mode.pre-v1.5.3` 恢复，不得宣称此类旧安装无需引导即可全自动跨级。
- 在 iPad Safari 横屏下打开菜单较多的管理面，确认左栏可触摸滚动到底部，版本、仓库操作和
  退出按钮均可见；浏览器工具栏伸缩及安全区变化后不得再次截断。
- 全新安装与重复安装均成功，断电重启后管理面自动启动。
- ModemManager/NetworkManager、pcscd、sing-box、lpac 状态符合预期。
- 已有 Docker 与外部容器保持不变；MDD 容器均带归属标签，端口冲突会安全中止。
- 至少验证一个蜂窝模块的 4G 开/关、VoWiFi 开/关、通话与短信。
- 至少验证一个 PC/SC 读卡器仅显示 VoWiFi，不显示虚假的 4G 能力。
- 多模块时逐台切换能力，确认不会改动另一台模块。
- Clash 订阅国家出口通过 UDP 测试，界面显示实际节点名称；无健康节点时故障关闭。
- Webhook GET/POST、Telegram 代理与 PushPlus 测试按钮均验证一次。
- Telegram 仅发送通知，设置页和后端均不存在远程拨号、短信或挂断指令入口；
  直接回复来信通知能给该号码回短信；相关操作出现在审计记录中。
- 自有 TLS 证书、首次管理员设置、修改密码、备份和脱敏支持包均验证一次。

## 隐私与发布

- 订阅者标识符（IMSI、ICCID、IMEI、号码）由 `tools/check-subscriber-identifiers.sh` 自动扫描，
  CI 与发布流程均已强制执行，无需手工核对。
- 仍需人工检查脚本覆盖不到的部分：EID、PIN、Token、订阅地址、私钥，以及截图内容。
- `data/`、`.env`、证书、pcap、数据库、构建目录和本机日志未被 Git 跟踪。
- 截图仅使用空状态、虚构数据，或已经逐项遮挡设备、线路、运营商、国家出口、号码与消息内容并经人工复核的真实页面。
- 先创建私有仓库完成内部验收；最终确认后再决定是否公开。
- 推送已签名的 `vX.Y.Z` 标签；Release 工作流会生成源码包、版本化 Compose YAML、四种组件
  各两种架构的镜像、DS1621+ 驱动包及同时覆盖这十一个文件的 `SHA256SUMS`，并把四种组件的
  多架构清单发布到 GHCR。
- **提交 `.github/release-notes/vX.Y.Z.md`，正文为简短的中英双语，中文在前、英文在后，
  两者内容一致。**按「0. 重要更新说明 → 1. 本次小版本更新 → 2. 当前大版本更新」组织，
  每条按「症状 → 原因 → 现在的行为」写一到两句。工作流直接用这个文件创建 Release，
  文件缺失会停止发布，不再先显示自动生成的开发提交列表。若本版未重建引擎镜像，在结尾
  注明，免得对方做多余的构建。
- 发布 Release 后，在 `update-policy.json` 的 `release` 中填写该版本，并按发布内容明确标记为
  `main`（包含功能变化）或 `patch`（仅修复）。先完成观察和实机验证，再更新自动安装通道：
  `channels.all` 必须指向当前最新正式 Release；发布主版本时同时推进 `channels.main`，发布补丁时
  保留既有主版本目标。两个通道各自填写 UTC `not_before`。过渡期的 `auto_update` 必须镜像
  `channels.all`，供 v1.5.3 及更早客户端读取。发现回归时清空对应通道和兼容字段，即可阻止
  尚未开始的设备安装。
- 两种架构均在各自原生 runner 构建；不得为了省一个 job 又让 x86 Runner 通过 QEMU 编译
  ARM64 Engine。WebUI 是架构无关的静态产物，但 Control 镜像仍需分别验证架构。
