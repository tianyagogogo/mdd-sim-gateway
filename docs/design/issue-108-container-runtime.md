# Issue #108：NAS 全容器化验证

状态：开发验证阶段，Hardware、Egress、完整 Compose、真实 VoWiFi 注册与 4G 数据已完成 NAS 实机验证。分支 `feat/108-container-runtime`
已在 2026-09-24 合并当时最新的 `origin/develop`，完整测试与 WebUI 构建通过；提交 PR 前仍需
再次获取远端并通过 CI。

## 目标与容器数量

在 NAS 上不安装项目软件包或宿主机服务，保留现有业务能力。
国家出口不得向宿主机主路由表写入 ePDG 路由，不接管 NAS 默认出口或 DNS。
Docker 创建专用 bridge、发布端口产生的常规网络规则不属于上述禁止范围。

拟采用三个基础容器：

| 容器 | 责任 |
| --- | --- |
| Control | WebUI、API、持久化、动态创建和管理 Engine |
| Hardware | 私有 D-Bus、ModemManager、PC/SC、VPCD、SIM bridge、硬件编排 |
| Egress | sing-box、Xray、每国独立 SOCKS5 TCP/UDP 入口，不创建国家 TUN |

每条运行线路再使用一个独立 Engine。因此常驻运行数量为 `3 + N`：
一条线路 4 个、两条 5 个、五条 8 个。停止的 Engine 可能保留为已停止容器。
将来的升级任务可以使用临时容器，不计入常驻数量。

共享 Egress 已完成显式 SOCKS5 UDP 传输验证，Hardware 已完成 EC25、SIM bridge
和跨容器 PC/SC 验证，Engine 的 SWu 也已接入显式代理传输。
完整 Compose 已在实体 NAS 上运行。Hardware 的 NetworkManager 使用接口白名单并
禁止蜂窝 profile 提供默认路由；当前实现与 Pi 的 host-network 模型一致。

## 验证主机（2026-09-22）

- Synology DS1621+，x86_64，DSM 7.4.1，内核 4.4.302+。
- Docker 24.0.2，Compose 2.20.1；32 GiB 内存。
- 已有多网卡、Open vSwitch、VLAN、DSM 策略路由和多个业务容器。
- `/dev/net/tun` 可用；插入模块并加载匹配驱动后已有 ttyUSB、cdc-wdm 与 wwan 节点。
- 既有 Docker 子网占用 `172.29.0.0/16`，与当前国家 TUN 使用的
  `172.29.20.1/30` 起始地址重叠，不能将当前出口配置直接放进 host 网络。
- `--cpus` 被 Docker 拒绝：内核不支持 CPU CFS scheduler 或对应 cgroup 不可用。
  正式配置不得将 CFS 配额作为必选项。

此文不保存 SSH 地址、账号、密钥或其他部署凭据。

## 已完成：独立网络空间内 TUN 与路由验证

脚本：`tools/container-runtime/probe_netns.py`。

```sh
python3 tools/container-runtime/probe_netns.py \
  --host USER@NAS --port SSH_PORT --identity /path/to/key \
  --image EXISTING_LOCAL_IMAGE
```

镜像必须已有 `python3` 和 `ip`。本次复用 NAS 本地 Home Assistant 镜像，
仅执行覆盖后的 Python 入口，不启动 Home Assistant，不挂载任何业务数据。
镜像 ID：`sha256:5157d3b45f2333749443ae7fb2c655b3ac7954018761c3f01144bd971030968f`。
这只是复用测试工具，不代表产品镜像将依赖 Home Assistant。

实验使用 `--network none`、只读根文件系统、`--cap-drop ALL`、
`--cap-add NET_ADMIN`、`/dev/net/tun` 设备映射和 `no-new-privileges`。
不使用 privileged、host 网络、host PID、Docker socket 挂载或宿主目录挂载。

结果：

- 容器网络命名空间与宿主机不同。
- 成功创建 `mdd-proof0`，配置文档用地址 `192.0.2.1/30`。
- `198.51.100.42/32` 路由在容器内指向 `mdd-proof0`。
- 运行前、运行中、删除后：宿主机 IPv4/IPv6 所有路由表、策略规则、
  网络接口一致。比较时仅归一化路由 `expires` 倒计时，保留其他字段。
- 临时容器已删除，不留下测试网络、卷或拉取的新镜像。

首次比较因 IPv6 RA 路由寿命自然递减而报告差异；确认该字段后归一化倒计时，
重新执行实验通过。初次使用 CPU 配额被拒绝后也已清理测试容器。

这项验证只证明内核与 Docker 能提供所需的 TUN/路由隔离，
没有验证 sing-box、UDP 转发、DNS、VoWiFi、SIM、音频或硬件热插拔。

## 第二阶段结果：无网络权限的国家出口（2026-09-22）

进一步检查发现已有每国 SOCKS 入口可以复用。实验采用显式 SOCKS5 UDP，
因此不需要让 Egress 转发 Engine 原始 IP 包，也不需要国家 TUN、NET_ADMIN、
NET_RAW、host 网络或设备映射。Engine 内原有 IMS TUN 仍需保留。

新增内容：

- `runtime/egress.py`：复用国家配置、代理协议、节点选择和子进程管理，
  只运行出口逻辑，不运行 ModemManager、systemd 或硬件编排。
- `runtime/Dockerfile.egress`：Python 3.12 + PyYAML + 已核验的 sing-box/Xray。
- `runtime/compose.egress-lab.yaml`：开发用出口骨架，三个基础服务中的一个，
  不是完整应用部署文件。
- `engine/outer_transport.py`：显式 SOCKS5 UDP socket 工厂，无直连重试。
- Control 增加 `MDD_ENGINE_NETWORK` 和 `MDD_HOST_PCSCD_DIR`，准备专用网络
  与容器 PC/SC 挂载。AMI 按指定网络选择 Engine 地址，缺失时不猜其他网络。

状态独立发布到 `socks-egress-status.json`，不覆盖旧 `proxy-status.json`。
这是必要的兼容边界：旧 Control 不能把“SOCKS 进程可用”误认为“ePDG 路由已安装”。
服务 ready 仅代表代理进程运行，不代表 UDP 或运营商连通性。

NAS 实验拓扑：

```text
模拟 Engine ──内部网络 A── Egress ──内部网络 B── 上游 SOCKS + UDP 回显端
```

三个实验容器全部 `cap_drop: ALL`，没有对外端口、TUN 或设备映射。
两个网络均为 Docker internal 网络。Engine 无法直接访问回显端。

已通过：

1. 两条独立 UDP association，1/500/1200/1400 字节双向传输。
2. `select` 可读通知，fork 子进程发送、父进程接收。
3. Egress 停止后，新 association 和已建立 association 都不能继续传输。
4. Egress 重启后，重新建立 association 可恢复通信。
5. 仅停止上游 SOCKS、保留 UDP 回显端：Egress 仍能直接访问回显端，
   但代理请求失败，未回退直连。上游恢复后新 association 恢复。
6. 删除实验容器、网络、临时文件后，宿主机所有 IPv4/IPv6 路由表及策略规则
   与实验前一致（只归一化 expires 倒计时）。

发现并处理的兼容问题：

- 上游 sing-box 二进制依赖 glibc；Alpine 的 Home Assistant 镜像不适合作为其基底。
  实测使用 NAS 缓存中的 `python:3.12.11-slim-bookworm` 构建，无联网构建步骤。
- 旧 host 模式的 `auto_detect_interface` 会在本机内核上触发需要额外权限的
  socket 接口绑定；SOCKS-only 模式无需避开 TUN 环路，已去掉此选项。
- DSM legacy builder 无法正确识别首次传输的未压缩 PAX 构建上下文，
  构建脚本改用 gzip + GNU tar。

实验仅使用合成回显流量，没有接触 SIM、发送短信或发起运营商通话。

本轮验证还包括：出口镜像默认入口与健康检查、sing-box/Xray 可执行性、
DSM Compose 配置校验，以及 223 项针对性测试（新出口 9、原国家出口 96、
Engine 路径 21、运行注册表 4、线路生命周期 93）。实验容器、网络和暂存目录已清理，
NAS 只保留开发镜像及其构建缓存，未启动常驻开发服务。

## 第三阶段：Control 启动契约（2026-09-23）

新增 `MDD_EGRESS_TRANSPORT=socks5`，默认仍走现有 host 协议。
新状态包含版本号与代理配置 SHA256 指纹，Control 仅接受 15 秒内的状态，
拒绝明显超前的时间戳、过期状态、旧配置、其他国家的 ready 和不合法 endpoint。
指纹仅用于配置一致性判断，不是身份认证或凭据加密。
国家错误信息已脱敏，避免底层解析错误将上游密码写入共享状态文件。

Control 将选中的内部 SOCKS endpoint 作为 `SWU_EGRESS_PROXY` 传给支持它的 Engine。
启动前检查镜像能力标签并锁定已检查的镜像 ID，防止旧镜像忽略新环境变量后直连。
不支持时在写配置、删除既有容器之前返回错误。当前 Engine 已带能力标签，
可以启用容器 SOCKS 链路。
禁用全局代理或明确配置国家 direct 时继续遵守用户的直连选择。

同日再次执行只读硬件探测，仍未发现蜂窝 USB、串口、QMI/MBIM 节点。
出口开发镜像已在 NAS 重建（`bfcd49307a05`），8 项 UDP 成功/故障/恢复实验再次通过；
实验清理无错误，宿主路由与策略规则恢复一致。未部署常驻服务。
本轮 231 项针对性测试通过：容器出口与契约 19、国家出口 96、
Engine 路径 23、线路生命周期 93。

## 第四阶段：Hardware 容器与实体 SIM（2026-09-23）

用户插入模块后，NAS 新增 USB `1-3`，VID:PID 为 `2c7c:0125`，
产品字符串为 `Baiwang`。只读 AT 探测确认固件标识为 QDC507，SIM 已就绪。
设备提供五个接口（`1-3:1.0` 至 `1-3:1.4`），均为 vendor-specific class `ff`，
DSM 缺少所需模块。使用 Synology 官方 v1000 DSM 7.4 工具链、与运行内核一致的
Linux 4.4.302 源码构建并临时加载 `mii`、`cdc-wdm`、`qmi_wwan`、`usb_wwan`
和 `option`；DSM 自带的 `usbnet`、`usbserial` 继续复用。所有模块 vermagic 均为
`4.4.302+ SMP mod_unload`。这些外部模块未签名，内核记录 `OE` taint；DSM 当前配置
没有强制模块签名。兼容范围与输入、输出校验值记录在
`runtime/synology-v1000-7.4-modules.json`，不能拿到其他平台或内核版本使用。

新增 Hardware 镜像和主管进程，运行私有 D-Bus、ModemManager、pcsc-lite 2.3.3、
四槽 VPCD 驱动与现有 SIM bridge。容器为只读根文件系统、非 privileged、
`cap_drop: ALL`，只增加 `SETUID`、`SETGID`、`DAC_OVERRIDE`，没有 `NET_ADMIN`。
使用 host 网络只是为了让 ModemManager 看到宿主 `wwan0`；它没有权限配置接口或路由，
也不启动 NetworkManager。

实体模块验证结果：

1. ModemManager 识别 1 个模块、`cdc-wdm0 (qmi)`、`wwan0 (net)` 和 4 个串口。
2. SIM bridge 分配逻辑通道 1/2/3，分别用于 PIN、SWu 和 IMS。
3. pcscd 枚举 4 个 VPCD reader，其中前三个由 bridge 提供卡通道。
4. 容器内 PC/SC `SELECT MF` APDU 返回 `9000`。
5. 第二个无网络客户端容器经共享 pcscd volume 完成相同 APDU 往返。
6. 受控 USB 重新枚举后 Hardware 自动恢复健康，QMI 与 SIM bridge 均恢复。
7. `wwan0` 全程保持 DOWN，测试前后宿主 IPv4 路由摘要一致。

本阶段结束时 Hardware lab 容器保留运行供后续 SWu 联调。随后完整 Compose 已替换它，
驱动也在第六阶段安装了严格匹配 DSM 型号、版本与内核的启动检查。

Control 镜像还修复了 Debian `/usr/lib64` 未进入动态链接器搜索路径的问题；独立
Control 客户端已通过设置等价搜索路径验证共享 PC/SC socket。

## 第五阶段：真实 Engine 的 SWu 显式出口（2026-09-23）

SWu 在代理模式下分别为 IKE/500 与 NAT-T/4500 建立 SOCKS5 UDP association，
完成 IKE AUTH 后强制使用 RFC 3948 UDP 封装，不创建 raw ESP socket。内层 MTU 同时扣除
ESP 与 SOCKS5 IPv4 目标头开销。发行版 v1.11.0 amd64 Engine 作为受信基础层，当前运行文件
以离线 overlay 构建为 `mdd-sim-gateway/engine:issue-108-dev`，镜像 ID 为
`sha256:34dc6c4fca5c`（前缀）。

NAS 隔离实验直接导入镜像中的 `swu_ike.py` 并调用实际 socket 方法。两条 association
均完成双向传输；停止 Egress、停止上游代理时均失败关闭，没有直接访问回显端；恢复后新
association 可用。实验清理无残留，宿主路由与策略规则前后一致。全量 1304 项测试通过。

这证明真实 Engine 的 socket 与进程模型可经容器出口工作；合成 UDP 回显仍不能替代运营商
ePDG 的完整 IKE AUTH、ESP/NAT-T 与 IMS 注册验证。

## 第六阶段：三基础容器与实体 SWu（2026-09-23）

新增 `runtime/compose.yaml` 和离线 Control overlay 构建器。实际部署运行 Control、Hardware、
Egress 三个基础容器；插入的 SIM 自动生成一条 Engine。Hardware、Control、Engine 通过固定
命名 PC/SC 卷共享虚拟读卡器，Control 与 Engine 通过固定内部 bridge 通信。NAS 的 8443 已由
UniFi 使用，开发 Control 映射到 9443；RTP 从发生冲突的 10000 段迁移到 30000 段。

实体链路结果：Hardware 与三个逻辑通道健康，Engine 成功建立 SWu 隧道并得到内层 IPv6 与
P-CSCF，Engine 回调成功到达 Control。首次 IMS REGISTER 未应答的原因是 IPv4-only Docker
bridge 把 Engine 网络命名空间的 IPv6 设为 disabled，导致 `ipsec0` 无法绑定运营商分配的
IPv6。Engine 创建参数现在只在自身网络命名空间启用 IPv6；重新连接后实测状态为 SWu
`CONNECTED`、USIM `AUTH_OK`、IMS `Registered`。这一设置不修改宿主机 IPv6。测试没有
发起电话、短信或蜂窝数据。

国家订阅由 NAS 自身的 `nas.izztt.com:8066` 提供。Egress 使用 Compose `host-gateway`
别名在容器内把该主机名解析到 Docker 宿主网关，保留原 URL 与 Host 头并避开公网回流。
美国出口解析出 11 个候选，其中 6 个因不支持 UDP 被排除，最终出口状态为 ready。由于代理
Engine 只加入 internal 网络，无法使用公共 DNS，Control 在删除旧 Engine 之前解析 ePDG
IPv4，并仅把结果写入本次运行配置；保存的线路仍保留运营商域名。解析失败会关闭启动流程，
不会给 Engine 添加直连网络或回退到 NAS 出口。

直连线路只在用户配置为 direct 或全局出口关闭时加入 uplink；SOCKS 线路不加入该网络，
且应用传输没有直连回退。整个过程中 `wwan0` 保持 DOWN，NAS 默认路由始终为原有网关。
Docker 按预期增加自身 bridge 路由，因此“全部路由表哈希不变”只适用于临时实验清理后，
不适用于常驻 Compose。

Hardware 发布兼容的设备状态供 WebUI 使用，并声明 `cellular_supported=true`。Control
通过只读共享的私有 D-Bus socket 使用既有蜂窝短信、USSD、通话和 MMS 路径。

五个外部模块已安装到 `/usr/local/lib/mdd-sim-gateway-modules`，并安装 DSM rc.d 启动脚本。
脚本在每次加载前核对 x86_64、4.4.302+、DSM 7.4.1-90080、v1000 平台标记和清单内全部
SHA256，不匹配即停止；卸载入口只删除持久文件，不强拆正在使用的驱动。当前开机的状态检查
通过，尚未为验证脚本而重启承载其他业务的 NAS。

## 第七阶段：Pi 等价的 4G 能力（2026-09-23）

Hardware 镜像加入 NetworkManager 与运营商 APN 数据库，Control 和 Hardware 共享私有
D-Bus。NetworkManager 配置为只管理 `wwan*`、`cdc-wdm*`；主管每次拨号前枚举其设备，
只要任一 NAS 物理网口、Open vSwitch、VLAN、Docker bridge 或 loopback 不是 unmanaged，
就失败关闭。每个模块使用稳定的 `mdd-cell-*` GSM profile，强制关闭 autoconnect，IPv4 和
IPv6 均设置 never-default。

DSM 4.4 的 qmi_wwan 在承载建立前要求 ModemManager 写 `raw_ip`。Hardware 保持只读根文件
系统，只将 `/sys/devices` 作为可写子树，并增加显式 NET_ADMIN、NET_RAW、SYS_ADMIN；
Docker 默认 AppArmor 会无条件禁止该 sysfs 写入，因此只对 Hardware 使用 unconfined profile。
容器仍为非 privileged，其他容器的 AppArmor 与 capability 不变。Hardware 重启留下旧 QMI
session 时，主管终止残留 qmi-proxy，通过仍可用的 AT modem 请求一次 firmware reset，并以
5 分钟限频等待完整 QMI 重新枚举。

实体模块实测：LTE 漫游注册到 CHINA MOBILE，数据开关建立 `cdc-wdm0` 承载并获得 IPv4、
IPv6；profile 显示 autoconnect=no。NAS 主路由仍为 `default via 10.0.0.2 dev ovs_eth0`，只新增
metric 700 的 wwan0 直连前缀。关闭数据后承载与 wwan0 路由消失，再开启后自动恢复。
同时 SWu `CONNECTED`、USIM `AUTH_OK`、IMS `Registered`，证明 4G 与 VoWiFi 可并行。

## 第八阶段：热插拔、实体读卡器与 eSIM 恢复（2026-09-24）

Hardware 镜像启用 libusb 热插拔，实体 SCR Prime 读卡器在插入后无需重建容器即可枚举；
Control 镜像内置修补后的 lpac，实机读取到 1 个 eUICC、3 个 profile。容器硬件运行时还补齐
了两项 Pi 编排器已有的恢复能力：

- 无 USB 序列号的百望/Quectel 模块改变 USB 路径后，以相同 15 位硬件 IMEI 迁移设备偏好并
  删除旧身份记录；不同 IMEI 或不明确的一对多情况不自动合并。NAS 实测从
  `2c7c-0125-1-3` 迁移至 `2c7c-0125-1-3.1`，页面不再保留离线副本。
- eSIM 操作完成后，Hardware 消费 Control 的按设备 bridge 重启请求，只停止目标 bridge；
  新进程 PID、逻辑通道 ready 和目标 ICCID 哈希全部匹配后才返回 `channels_ready`。使用当前
  profile 做无变更实测，3 个逻辑通道在 3 秒内恢复。

Hardware 现在也发布 `host-diagnostics.json` 的容器等价数据，支持包能看到 Docker 运行环境、
ModemManager、USB modem、VPCD 监听端口和 bridge 身份健康，不再误报“宿主编排器未运行”。
诊断文件只保存 IMEI/ICCID 有效性，不保存这两个标识符本身。NAS 实测 1 个 modem、1 个
bridge 和 3 个 VPCD 端口均健康。

国家出口设备展示已同时适配 Pi 的按线路状态和容器 Egress 的按国家状态。NAS 当前英国、
美国出口均能显示实际节点，Engine 的 SOCKS endpoint 与所选国家一致。

## 第九阶段：容器服务重启（2026-09-24）

容器部署现在直接消费 WebUI 的服务重启请求。Control 与“全部服务”两种范围都先按固定
容器名查找目标，再核对项目归属和组件标签；名称相同但没有正确标签的容器不会被操作。
“全部服务”依次重启 Egress、Hardware，最后重启 Control；重启 Hardware 前写入 PC/SC
维护标记，沿用 Pi 编排器对读卡器短暂离线的处理。

Control 不能同步重启自己，否则 Docker 会在停止调用方后取消尚未执行的 start。实现使用
Control 同一镜像启动一个无网络、仅挂载 Docker socket 的一次性辅助容器，重新核对目标
标签后执行最后一次重启。新 Control 进程启动时把共享状态从 `running` 落为 `success`，
辅助容器随后自动删除。NAS 实测仅 Control 的启动时间发生变化，Hardware、Egress 与三条
Engine 均未重启。容器模式不提供“重启主机”：该操作明确返回不可用，DSM 重启仍由宿主
管理界面或 SSH 完成，不向容器开放宿主 PID/特权逃逸能力。

## 第十阶段：完整发行镜像输入（2026-09-24）

Release workflow 已从 Control、Engine 两种镜像扩展为四种运行镜像。amd64 与 arm64 都在
原生 runner 构建 Control、Hardware、Egress、Engine，逐个核对架构、组件归属、项目归属和
版本标签，发布带架构的离线归档，并合成四个 GHCR 多架构版本标签。源码包内的受保护清单
和 Release 顶层 `SHA256SUMS` 同时覆盖全部八个架构镜像资产。

现有离线镜像安装器增加 `container` 模式：只接受当前机器架构，要求清单同时包含四个镜像，
校验后导入；Hardware 与 Egress 在替换本地标签前保留 `:previous`，导入后的架构、组件、项目
归属和版本任一不匹配都会恢复旧标签。这提供了一键更新所需的完整、可回滚镜像输入。

## 第十阶段：容器一键更新与整组回滚（2026-09-24）

容器 Control 现在会把 WebUI 更新请求交给一个由当前 Control 镜像启动的一次性助手。助手在
停止 Control 前下载并校验四种原生 Release 镜像、创建一致备份、保存 Compose，随后重建三个
基础容器并逐条更换 Engine。实际镜像 ID 与归档 SHA-256 持久化到数据目录。任何基础服务或
线路未按目标镜像恢复时，助手自动恢复上一份 Compose、基础容器和更新前 Engine 镜像。助手
仅挂载项目数据目录和 Docker socket，不使用 privileged、宿主 PID 或宿主网络；完成后自动删除。

## 下一阶段

### 已知限制

- Control 位于 Docker bridge 命名空间，无法在删除旧 Engine 前可靠探测宿主命名空间中的
  RTP 发布端口。容器模式只检查项目已分配端口，最终冲突由 Docker 在创建新 Engine 时报告；
  发生冲突时该线路会保持停止并在页面显示启动错误。部署前应确保配置的 RTP 范围未被宿主
  或其他 Compose 项目占用。
- 新增或移除蜂窝模块会改变 VPCD reader 布局，当前 Hardware 会重启私有 pcscd 和 bridge，
  已运行线路会出现一次短暂 SIM 通道中断。后续需引入持久化 reader 槽位和增量生命周期，
  才能在不重排既有 reader 的情况下完成多模块热插拔。

正式镜像、实体读卡器、可选宿主驱动目录、安装、更新与回滚的发布边界见
[全容器版本发布方案](container-release-plan.md)。

### 后续开发

1. 在计划维护窗口验证 DSM 重启后的驱动与完整栈自动恢复。
2. 在实体运营商条件下分别完成蜂窝短信、USSD、通话、MMS 与真实 eSIM profile 切换。
3. 在 ARM64/Pi 上验收同一套全容器镜像和无需项目宿主驱动的 `host-native` 路径。

所有测试使用独立名称和数据目录，不修改现有业务容器或重启 Container Manager。
