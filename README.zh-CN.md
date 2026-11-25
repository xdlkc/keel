# Keel

[English](./README.md) | 简体中文

Keel 是一个中文优先的本地控制台，用于管理一台现有 Google Compute Engine 虚拟机，并提供 Outline Shadowsocks、Prometheus 与 Grafana 的离线生成器。本地控制台与离线生成器使用 Python 标准库。主机安装和客户端验收使用独立的运维工具。

## 新客户端与新账号配置

新设备接入或新 Google 账号独立部署，请从 [AGENTS.md](./AGENTS.md) 进入[配置手册](./docs/agent-onboarding.md)。手册覆盖赠金核验、独立的 Google Cloud 登录环境、新加坡实例、HTTPS 订阅交付和客户端流量验证。全新的 Debian 13 x86-64 主机可用安装脚本配置 Outline、Prometheus 与需要登录的 Grafana。已有空槽位可直接分配，在脚本管理的主机上也可以新增槽位。普通代理客户端只需要共享订阅和分配给它的设备名。

## 双节点 443 TLS 架构

当前生产环境已从单机裸 Shadowsocks 高端口（`18443`）与 6 天 IP 证书升级为双节点 `443` TLS（Trojan + WebSocket）架构，并全面采用 90 天常规 Let's Encrypt ECDSA 域名证书。完整拓扑、主机路径与运维手册见 [双节点 443 TLS 架构与运维手册](./docs/tls-443-architecture.md)，脱敏配置模板位于 [`deploy/tls-gateway/`](./deploy/tls-gateway/)。

- **新加坡主力节点（`sg`）**：`nginx` 在 `80` 端口仅开放 ACME HTTP-01，在 `443` 端口将 `/keel/mobile-ws` 反代至仅监听回环的 `sing-box` 网关（`127.0.0.1:19080`，systemd 加固 `IPAddressDeny=any` 与 `IPAddressAllow=localhost`）。4 台设备（`computer-1`、`phone-1`、`computer-2`、`phone-2`）按 `auth_user` 分别路由至本机回环 `outline-ss-server` 出站，完整保留 Grafana 按设备流量与 TCP 链路诊断指标。独立订阅域名仅放行精确匹配的 256-bit 随机路径（`access_log off`、`Cache-Control: private, no-store`、`X-Robots-Tag: noindex, nofollow`），主力域名根路径 `/` 托管 Grafana。
- **美国俄勒冈备用节点（`us`）**：采用 GCP Always Free `e2-micro` 与 `STANDARD` 网络层级（前 200 GiB／月免出站流量费），对外仅放行 `80`/`443`（及 SSH），不暴露任何明文／裸代理端口。`nginx` 将 `/keel/mobile-ws` 反代至本机 `sing-box`（`direct` 出站并启用 `ip_is_private` 拒绝规则阻断私有网段与 `169.254.169.254` 元数据访问），同时镜像托管同一份 Clash 订阅，并启用 TCP BBR 与 `100M` 的 `journald` 日志上限。
- **统一双节点 Clash 订阅与动态流量条**：两机镜像维护 `/var/lib/keel-sub/profile.yaml`，由回环 `keel-sub-server`（`127.0.0.1:19090`，`www-data`）经 nginx 精确路径反代输出，客户端仅暴露 `新加坡 01` 与 `美国 01` 两个逻辑节点，内置 `🚀 节点选择`、新加坡优先的 `🛟 故障转移 (新加坡优先)`、`♻️ 自动测速`、国内外分流规则，并实时附加 `subscription-userinfo` 与 `profile-update-interval: 12` 响应头。
- **月度出站计量与自动熔断**：两机通过 `/usr/local/bin/keel-quota` 与 `keel-quota.timer`（每 `60s`）读取默认网卡 `/proc/net/dev` 出站字节（状态持久化在 `/var/lib/keel-quota/`，配置在 `0600` 的 `/etc/keel-quota/config.json`），经 HTTPS `/metrics-sync/<256-bit-token>.json` 每分钟互相同步月度用量。双机合并达 `200 GiB／月`（美国备用机另设本机 `190 GiB／月` 硬上限）时仅停止代理服务，不影响 SSH、nginx、Grafana 与证书续签；每月 1 日 `00:00 UTC` 自动清零并恢复代理。
- **简化换 IP 与防覆盖提醒**：任一节点更换临时公网 IP 后只需更新 DNS `A` 记录，90 天域名证书与客户端订阅地址均无需修改。**切勿直接对已升级的 `443` TLS 主机运行 `deploy/outline/` 下的旧版裸 IP／高端口安装脚本或 `generate.py` 覆盖配置**。

## 前置条件

- Python 3.9 或更高版本，无需 pip 依赖。
- 现有虚拟机及已登录的 `gcloud`，用于状态、指标、启动和停止。
- 代理主机上由主机负责人另行安装并配置 `outline-ss-server` v1.9 或更高版本、Prometheus、`node_exporter` 和 Grafana。
- 一个可供 Outline TCP 与 UDP 共用的公网端口。Prometheus、node_exporter 和 Outline 指标地址保持在本机回环地址。

## 生成 Outline 与共享订阅

从不含真实机密的模板创建私有清单，再把每个占位值替换为不同且密码学安全的 secret：

```bash
cp deploy/outline/inventory.example.json deploy/outline/inventory.local.json
chmod 600 deploy/outline/inventory.local.json
python3 deploy/outline/generate.py \
  --inventory deploy/outline/inventory.local.json \
  --output deploy/outline/generated
```

清单是一个 JSON 对象：

- `host`：服务器的 IPv4、IPv6 或 DNS 主机名，不带协议或路径。
- `port`：`1` 到 `65535` 的整数，所有设备的 TCP 与 UDP 共用此端口。
- `devices`：有序的显式设备槽位列表。每槽包含唯一且可打印的 `label`、稳定 `id` 和唯一 `secret`。`id` 长度为 1–64，由小写 ASCII 字母、数字和内部连字符组成，并用作 Outline 的 `access_key`。加密方式为 `chacha20-ietf-poly1305`。

清单必须是没有符号链接的本地文件，且组用户和其他用户无任何权限。生成器拒绝重复的 ID、标签或 secret，绝不打印 secret 值。它创建权限为 `0700` 的输出目录，并以原子方式替换以下两个确定性、权限为 `0600` 的文件：

- `outline.yml`：v1.9+ 的 `services` → `listeners`／`keys` 配置，TCP 与 UDP 使用同一端口。
- `clash.yaml`：一个名为 `Keel Outline` 的 `select` 组，每台设备一个命名 SS 节点，以及 `profile.store-selected: true`。没有自动选择组。直连规则指向所配置的服务器以避免 TUN 回环，其他流量使用已选节点。

两个文件均为 JSON 编码的 YAML，相应的 YAML 解析器均可读取。相同输入重复生成的字节完全一致。不得提交或记录私有清单、生成文件或订阅 URL。 使用仅限所有者访问的专用输出目录。同一目录已有生成任务时，第二次写入会被拒绝。

### 所有设备使用同一个 URL

通过主机负责人已有的静态托管，将同一份 `clash.yaml` 发布到一个 HTTPS URL。新主机安装脚本可通过 nginx 托管该订阅，本地控制台独立运行。每台 Clash／Mihomo 设备导入同一订阅，在 `Keel Outline` 中选择自己的命名节点一次，并保留客户端本地配置／缓存，让 `store-selected` 恢复选择。

**安全边界：**每份订阅都包含所有设备的 secret。持有 URL 的任何设备或人员都能读取全部凭据；这不是设备间隔离或单设备吊销机制。请将 URL 视为持有即用的机密，使用不可猜测的路径和已有分发控制；订阅泄漏后应轮换所有已暴露密钥。

## 连接主机服务

以下是供主机负责人使用的真实入口，Keel 不会代为执行：

```bash
outline-ss-server -config /etc/keel-outline/outline.yml -metrics 127.0.0.1:9091
node_exporter --web.listen-address=127.0.0.1:9100
prometheus --config.file=/etc/prometheus/keel.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.listen-address=127.0.0.1:9090
```

将生成的 `outline.yml` 以仅所有者可读的权限安装为 `/etc/keel-outline/outline.yml`，并将 [`prometheus.example.yml`](./deploy/outline/prometheus.example.yml) 复制为 `/etc/prometheus/keel.yml`。抓取示例使用 `127.0.0.1:9091` 的 Outline 指标和 `127.0.0.1:9100` 的 node_exporter，不虚构指标标签。主机防火墙应对所选 Outline 公网端口同时放行 TCP 和 UDP。

对于同主机上的 Grafana，将 [`grafana-datasource.yml`](./deploy/outline/grafana-datasource.yml) 安装到其数据源 provisioning 目录。它创建 UID 为 `keel-prom`、地址为 `http://127.0.0.1:9090` 的 Prometheus 数据源。导入 [`grafana-dashboard.json`](./deploy/outline/grafana-dashboard.json)；仪表盘 UID 为 `keel-outline`，通常路径为 `/d/keel-outline/keel-outline`。远程访问应使用 Grafana 已有登录机制和现有 HTTPS 反向代理；Keel 不增加认证或公网指标监听器。

主机安装脚本会同时部署 [Node Exporter Full 模板 1860，第 45 版](https://grafana.com/grafana/dashboards/1860-node-exporter-full/)。手动配置时导入[同版模板](https://grafana.com/api/dashboards/1860/revisions/45/download)，选择 Keel Prometheus 数据源。从 Keel 顶部打开模板会保留当前时间范围。

以下指标覆盖设备、TCP 链路、主机、磁盘和网络。

- 设备筛选器和 Outline 时段明细面板纳入在所选时段内历史 Outline 客户端流量或 `keel_tcp_download_bytes_total` 至少一项为正增量的密钥。首次长下载无需等待 Outline 关闭连接即可选中设备。活跃曲线保留中途空闲的时刻，但采样缺失仍可能导致设备不显示。
- 所有面板使用 Grafana 原生所选时间范围，滚动速率使用 `$__rate_interval`。字节速率以字节／秒显示，RTT 以秒为单位，时段流量是所选时间窗口内字节增量的估算值。
- 仅使用 `shadowsocks_data_bytes` 的 `c>p` 与 `c<p` 方向以及 `tcp` 与 `udp` 协议，按 `access_key`、`dir`、`proto` 分组。排除服务端方向以避免重复计量。这些 Outline TCP 字节计数器在连接结束时才更新，长连接的流量可能延后显示为速率峰值。
- 原生“TCP 链路诊断”行按 `access_key` 筛选。RTT 和下载序列仅增加 `leg` 标签（`source` 或 `device`），不使用来源 IP 标签。实时平均 RTT 以 `keel_tcp_rtt_seconds_sum` 总和除以正的 `keel_tcp_rtt_samples` 总和；两个 gauge 都只描述当前打开的套接字。
- 下载面板通过 `rate(keel_tcp_download_bytes_total)` 对比 source 链路从源站接收的 payload 与 device 链路已获客户端 ACK 的加密 TCP 字节。两者刻意采用不同的字节口径。
- 重传面板通过 `rate(keel_tcp_device_retrans_bytes_total)` 显示 VPS 发往客户端的加密 TCP 重传字节。积压面板对 `keel_tcp_device_notsent_bytes` gauge 求和，显示同方向当前尚未发送的字节。采样缺失保持缺口，不补零。
- 隧道时长按密钥和来源 IP 的活跃秒数估算，多个来源并发时可超过实际经过时间。TCP 关闭连接数按密钥和状态分组。
- 主机面板不随 `device` 筛选，覆盖主机运行时长、可见 vCPU 与内存、除以可见 vCPU 数的 1／5／15 分钟负载、包含 iowait 与 steal 的 CPU 模式、可用内存、CPU／内存／IO PSI、OOM 终止以及根文件系统空间与 inode。
- 磁盘面板分别显示读写吞吐、IOPS、以毫秒为单位的平均延迟和繁忙时间，不跨设备求和。通用过滤排除 loop、RAM、光驱、device-mapper、RAID、zram、NBD／RBD 及常见分区设备。窗口内对应读或写操作为零时省略该平均延迟序列，不伪造零值。
- 网络面板保留每网卡收发速率，并增加非回环网卡错误／丢包、TCP 重传以及可用时的 TCP inuse／time-wait 套接字状态。
- 协议健康面板展示 TCP 重传占比、SYN 重传、超时、监听溢出与丢弃，以及 UDP 数据报、缓冲区错误和 Outline UDP 映射增删速率。这些指标覆盖整台实例。
- 不写死抓取 job 或 instance 值，也不为缺失序列补零；可选采集器缺失的数据保持“无数据”。仪表盘不虚构来源 IP 清单、操作系统或硬件型号及物理设备在场状态。

当设备的发送队列有积压或重传字节在增加时，TCP 诊断仍保留该设备。RTT 是每次抓取时活跃连接的均值；短连接可能落在两次抓取之间，套接字关闭后才到达的 ACK 也可能超出最后一次采样。安装程序使用[审查过的 Outline 构建](./deploy/outline/tcpinfo/build.sh)。已有手工部署主机的升级步骤见[配置手册](./docs/agent-onboarding.md)。

## 本地虚拟机控制台

复制并编辑 [`vps.example.json`](./vps.example.json)：

```bash
cp vps.example.json vps.local.json
python3 keel.py serve
```

打开 `http://127.0.0.1:8765`，可用 `--port` 修改端口。服务仅绑定回环地址。中文主页只包含实例状态、项目、可用区、机器类型、启动磁盘、公网 IP、更新时间、刷新以及原有受保护的启动／停止确认。Rust 流量视图和 Cloud Monitoring 图表不属于当前界面。

`dashboardUrl` 为可选项。它必须是不含 user-info 凭据的 ASCII HTTPS URL；仅当地址为 `localhost` 或 IP 回环地址时才允许 HTTP。不要在 URL 中放入 API 令牌或密码。配置后主页显示一个 Grafana 链接。`/devices` 忽略所有请求参数，以 `Cache-Control: no-store` 重定向到固定的已配置 URL；未配置时重定向到 `/`，因此书签不会形成开放重定向。

远程和仅本机配置示例分别为：

```json
{
  "project": "your-gcp-project-id",
  "zone": "us-central1-a",
  "instance": "your-vm-instance-name",
  "dashboardUrl": "https://grafana.example.com/d/keel-outline/keel-outline"
}
```

```json
{
  "project": "your-gcp-project-id",
  "zone": "us-central1-a",
  "instance": "your-vm-instance-name",
  "dashboardUrl": "http://127.0.0.1:3000/d/keel-outline/keel-outline"
}
```

只有 `TERMINATED` 状态可启动，只有 `RUNNING` 状态可停止。两者都要求完整实例名、同源请求和每个进程独立的 CSRF 令牌。服务器在调用 gcloud 前重新加载目标配置，绝不自动重放已被拒绝的操作。

## 命令行与现有 API

```bash
python3 keel.py status
python3 keel.py metrics
python3 keel.py serve --port 8765
python3 keel.py start --confirm your-vm-instance-name
python3 keel.py stop --confirm your-vm-instance-name
```

`status` 报告当前虚拟机详情。`metrics` 输出最近一小时的 Cloud Monitoring CPU 百分比和出站网络速率，需要已有 Monitoring API、权限和网络访问。尽管主页不再绘制云端图表，`/api/status` 和 `/api/metrics` 仍然保留。启动／停止成功消息仅表示同步 gcloud 命令已退出，不表示已观察到目标状态；请运行 `status` 确认。

现有 `trafficProxyPort` 配置和旧流量 CLI／API 仍为回滚保持解析兼容，但不是当前 Outline 可观测性路径。

## 费用与边界

运行中的虚拟机、出站网络流量、持久磁盘以及静态或保留公网 IP 都可能计费。停止虚拟机不一定会停止磁盘或 IP 费用，临时外部 IPv4 地址可能在重启后改变。Outline、Prometheus、Grafana 和 node_exporter 也会消耗主机 CPU、内存、磁盘和网络；保留的 Prometheus 样本会增加存储占用。Keel 不显示虚构账单或配额，请使用云服务商账单控制台。

## 离线验证

```bash
python3 -m unittest discover -s tests -v
```

测试只使用本地伪数据、临时文件和回环 HTTP 服务，不需要凭据、gcloud、云资源、外部网络、Outline、Prometheus 或 Grafana。

## 目录结构

- [`keel.py`](./keel.py)：虚拟机命令行入口。
- [`keel/`](./keel/)：配置、受保护的 gcloud 操作、指标和本地 HTTP 服务。
- [`keel/static/`](./keel/static/)：精简后的中文控制台。
- [`docs/tls-443-architecture.md`](./docs/tls-443-architecture.md)：双节点 443 TLS 架构、DNS 拓扑、统一 Clash 订阅、月度计量与自动熔断及运维手册。
- [`docs/agent-onboarding.md`](./docs/agent-onboarding.md)：Google Cloud 与单机 Outline 接入／安装手册。
- [`deploy/tls-gateway/`](./deploy/tls-gateway/)：双节点 443 TLS 架构的脱敏 nginx、sing-box、systemd、sysctl、journald、计量／订阅服务与统一 Clash 订阅模板。
- [`deploy/outline/generate.py`](./deploy/outline/generate.py)：离线 Outline 清单生成器（旧版裸 Shadowsocks 布局）。
- [`deploy/outline/`](./deploy/outline/)：清单、Prometheus、Grafana 数据源、仪表盘与旧版主机安装脚本。
- [`tests/`](./tests/)：离线回归测试。

旧 `keel/traffic.py`、`keel/traffic_server.py` 和 `deploy/keel-traffic-*.service` 文件仅供手动旧版回滚；当前界面不链接或宣传这些文件。
