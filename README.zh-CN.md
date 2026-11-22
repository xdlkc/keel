# Keel

[English](./README.md) | 简体中文

Keel 是一个中文优先的本地控制台，用于管理一台现有 Google Compute Engine 虚拟机，并提供 Outline Shadowsocks、Prometheus 与 Grafana 的离线生成器。项目仅使用 Python 标准库和本地 HTML／CSS／JavaScript，不负责主机开通、网络部署、代理安装、订阅抓取、用户认证或控制平面。

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

通过主机负责人已有的静态托管，将同一份 `clash.yaml` 发布到一个 HTTPS URL。Keel 有意不提供此 URL 的服务或抓取功能。每台 Clash／Mihomo 设备导入同一订阅，在 `Keel Outline` 中选择自己的命名节点一次，并保留客户端本地配置／缓存，让 `store-selected` 恢复选择。

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

仪表盘按设备与密钥、主机概览、压力与磁盘、网络与 TCP 划分为原生分区：

- 支持 All 和多选的 `device` 变量列出指标中观察到的 `access_key`。配置密钥不等同于物理设备清单或设备数量。
- 客户端速率使用 `rate(...[$__rate_interval])`，单位为字节／秒；所选时间范围的估算流量使用 `increase(...[$__range])`，单位为字节。
- 仅使用 `shadowsocks_data_bytes` 的 `c>p` 与 `c<p` 方向以及 `tcp` 与 `udp` 协议，按 `access_key`、`dir`、`proto` 分组。排除服务端方向以避免重复计量。TCP 连接结束时才更新字节计数，长连接的流量可能延后显示为速率峰值。
- 对筛选后的非空认证密钥，使用 `increase(shadowsocks_tunnel_time_seconds{access_key=~"$device",access_key!=""}[$__range])` 估算所选时段的隧道时长，单位为秒；使用 `shadowsocks_tcp_connections_closed` 按 `access_key` 和 `status` 统计所选时段内关闭的 TCP 连接。原生隧道计数器累加活动密钥／来源 IP 的持续时间，并发来源可使累计值超过墙钟时间；它不是物理设备开机时间、在线状态或最后出现时间。
- 主机面板不随 `device` 筛选，覆盖主机运行时长、可见 vCPU 与内存、除以可见 vCPU 数的 1／5／15 分钟负载、包含 iowait 与 steal 的 CPU 模式、可用内存、CPU／内存／IO PSI、OOM 终止以及根文件系统空间与 inode。
- 磁盘面板分别显示读写吞吐、IOPS、以毫秒为单位的平均延迟和繁忙时间，不跨设备求和。通用过滤排除 loop、RAM、光驱、device-mapper、RAID、zram、NBD／RBD 及常见分区设备。窗口内对应读或写操作为零时省略该平均延迟序列，不伪造零值。
- 网络面板保留每网卡收发速率，并增加非回环网卡错误／丢包、TCP 重传以及可用时的 TCP inuse／time-wait 套接字状态。
- 不写死抓取 job 或 instance 值，也不为缺失序列补零；可选采集器缺失的数据保持“无数据”。仪表盘不虚构来源 IP 清单、操作系统或硬件型号及物理设备在场状态。

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
- [`deploy/outline/generate.py`](./deploy/outline/generate.py)：离线清单生成器。
- [`deploy/outline/`](./deploy/outline/)：清单、Prometheus、Grafana 数据源与仪表盘文件。
- [`tests/`](./tests/)：离线回归测试。

旧 `keel/traffic.py`、`keel/traffic_server.py` 和 `deploy/keel-traffic-*.service` 文件仅供手动旧版回滚；当前界面不链接或宣传这些文件。
