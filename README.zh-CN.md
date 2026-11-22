# Keel

[English](./README.md) | 简体中文

Keel 是一个中文优先的本地控制台，只管理一台已配置的 Google Compute Engine 虚拟机。它仅使用 Python 标准库和本地 HTML／CSS／JavaScript。主控制台不是多实例管理器、账单面板、部署工具、代理安装器或后台服务；可选的独立 ssmanager 采集器可提供设备流量视图，且不会创建 GCP 资源。

## 前置条件

- Python 3.9 或更高版本，无需安装 pip 依赖。
- 已安装 Google Cloud CLI（`gcloud`），并已通过 `gcloud auth login` 登录。
- 一台现有的 Compute Engine 虚拟机，以及其项目 ID、可用区和实例名。
- 可访问 Cloud Monitoring 的网络。Monitoring API 和所需权限必须预先配置，Keel 不会启用 API。

## 配置

在 `keel.py` 旁边复制示例文件，然后填写三个必填值：

```bash
cp vps.example.json vps.local.json
```

```json
{
  "project": "your-gcp-project-id",
  "zone": "us-central1-a",
  "instance": "your-vm-instance-name"
}
```

`vps.local.json`、常见凭据文件和 Python 缓存均已加入 Git 忽略规则。Keel 不创建服务账号密钥，也不保存访问令牌。配置必须是有效的 UTF-8 JSON。项目、可用区和实例标识符会在启动任何子进程前校验；类似命令选项的名称会被拒绝，普通项目 ID 和旧式域名作用域项目 ID 均可使用。

## 命令行

在仓库根目录运行：

```bash
python3 keel.py status
python3 keel.py metrics
python3 keel.py serve
python3 keel.py serve --port 8765
python3 keel.py start --confirm your-vm-instance-name
python3 keel.py stop --confirm your-vm-instance-name
```

- `status` 显示状态、可用区、机器类型、启动磁盘大小和公网 IP。
- `metrics` 输出最近一小时按分钟聚合的 CPU 百分比和出站网络速率（Mbit/s）。
- `start` 和 `stop` 要求 `--confirm` 完整匹配实例名；缺少该参数或名称不一致时，不会调用操作。
- 成功仅表示同步 gcloud 命令成功退出，不表示已经观察到目标状态。请运行 `status` 核实实际状态。

## 本地网页控制台

```bash
python3 keel.py serve
```

打开 `http://127.0.0.1:8765`。默认端口为 `8765`，可用 `--port` 修改。服务只绑定 `127.0.0.1`，不会监听所有网卡。服务在前台运行，按 Ctrl+C 退出。

中文界面提供刷新、真实的启动／停止确认对话框、一小时 SVG 趋势图、空数据与缺失数据状态，以及项目控制台、Monitoring 和固定账单控制台的官方链接。只有 `TERMINATED` 状态可启动，只有 `RUNNING` 状态可停止。每个对话框会显示并固定打开时所见的项目、可用区和实例。服务器会重新加载配置，在调用 gcloud 前拒绝任何目标不匹配，即使其他项目或可用区存在同名实例。操作必须携带每个服务进程独立的会话 CSRF 令牌和同源请求。令牌被拒绝后会清除缓存，并要求重新明确确认，绝不会自动重放操作。非法 `Host` 和跨源请求会被拒绝，以降低 CSRF 与 DNS 重绑定风险。

主页每次刷新后，紧凑的设备区域会独立请求同源 `/api/traffic?limit=1`。云端或会话错误不会阻止此请求，流量错误也不会影响云端图表。流量请求失败时，旧摘要值会被清除，并显示简短的不可用状态。完整只读视图位于 `/devices`。

## 可选的设备流量

Keel 可以轮询现有 shadowsocks-rust `ssmanager` 的 Unix 数据报 socket，并提供独立的只读设备页面。此功能使用现有流量节点和私有 SQLite 文件，不创建 GCP 虚拟机、服务账号、指标或其他资源，也不启用 API。它不会安装 ssmanager，也不会替换其配置。

### 计量边界

- 管理器的 `stat` 计数器按服务端端口采样。RX 与 TX 已合并求和：Keel 只显示合计字节数和每秒字节数，不拆分方向。
- 每台物理设备必须拥有自己的服务端端口和专用 Shadowsocks（SS）凭据。共享端口或凭据只能得到共享总量，无法识别各台设备。`shared: true` 会明确标记此类旧共享条目。
- `totalBytes` 是 Keel 首次观察该端口后累计的监测总量，不是管理器此前历史，也不是 GCP 账单。没有历史回填；缺失样本保持缺失，不会虚构速率。
- 对一个管理器及其数据库只能运行一个持续采集器（单个 watcher）。不要让多个采集器使用同一管理器映射或 SQLite 文件。
- 首次观察、采集中断、端口缺失或管理器／计数器重置后，当前速率不可用，直到新的连续样本建立基线。计数回落不会产生负用量。当前速率不可用时仍可保留最后的监测总量；样本过期（默认 30 秒）后同样没有当前速率。
- 采集器在权限为 `0700` 的状态目录中创建权限为 `0600` 的数据库。`--history` 限制每个端口保留的样本数（默认 `360`，允许范围 `10`–`100000`）。静态映射只含标签，不含凭据、客户端 IP 或客户端主机名。

### 部署到现有流量节点

示例使用以下路径和名称；请根据主机情况一致地修改：

- 代码：`/opt/keel`；映射：`/etc/keel/traffic.json`。
- 数据库：`/var/lib/keel-traffic/traffic.db`；专用客户端 socket 目录：`/run/keel-traffic`。
- 现有管理器：`ssmanager.service`，用户和组均为 `ssmanager`，权限为 `0700` 的 Unix socket 位于 `/run/ssmanager/manager.sock`。
- 只读 Web 监听地址：`127.0.0.1:8766`。

使用现有管理器的非特权账号，不要使用 `root`。请单独配置管理器 socket，不要放宽其 `0700` 权限。两个 Keel 单元有意使用同一账号，以便采集器访问 socket，并让两者访问私有数据库。`RuntimeDirectory=keel-traffic` 和 `RuntimeDirectoryMode=0700` 会创建采集器的专用客户端目录。

从当前检出目录运行以下示例配置命令：

```bash
sudo install -d -m 0755 /opt/keel /etc/keel
sudo cp -R keel /opt/keel/
sudo install -d -m 0700 -o ssmanager -g ssmanager /var/lib/keel-traffic
sudo install -m 0640 -o root -g ssmanager deploy/traffic.example.json /etc/keel/traffic.json
sudo install -m 0644 deploy/keel-traffic-collector.service deploy/keel-traffic-view.service /etc/systemd/system/
sudoedit /etc/keel/traffic.json
sudo systemctl daemon-reload
sudo systemctl enable --now keel-traffic-collector.service keel-traffic-view.service
```

在映射中填写实际的每设备端口和标签；映射不得包含 SS 密码或其他机密。如果管理器账号、单元、socket 或路径不同，请同时修改两个单元文件和映射。采集器仅限 `AF_UNIX` 及其状态／运行目录。查看器仅限 `AF_INET`，无法访问管理器或客户端 Unix socket，并使用 SQLite `mode=ro` 与 `PRAGMA query_only=ON` 打开数据库；systemd 还会以只读方式挂载数据库路径。查看器仅提供 `http://127.0.0.1:8766/devices`，不含实例管理或 gcloud 路由。

### 连接主控制台

从以下两种本机连接方式中选择一种。两种方式中，主控制台都保持在 `127.0.0.1`，只转发只读 `/api/traffic`，不会暴露管理器 socket。

**方式 A：现有本机 HTTP 代理。** 在 `vps.local.json` 中添加 `trafficProxyPort`：

```json
{
  "project": "your-gcp-project-id",
  "zone": "us-central1-a",
  "instance": "your-vm-instance-name",
  "trafficProxyPort": 17890
}
```

此值可选；如果提供，必须是 `1` 到 `65535` 的 JSON 整数。本示例指定监听本机 `17890` 端口的现有 HTTP 代理；Keel 不启动也不配置该代理。Keel 通过 `127.0.0.1:17890` 发送固定上游 `http://192.0.2.80/api/traffic` 的绝对形式请求（附带所请求的 `limit` 查询参数），并设置 `Host: 192.0.2.80`。请配置现有代理和节点路由，使该地址只到达只读监听器。

手机私有访问地址为 `http://192.0.2.80/devices`。请在手机现有代理客户端中为目标 `192.0.2.80` 添加路由规则，使用该手机自己的专用设备 SS 凭据，不要使用共享凭据或其他设备的凭据。在流量节点上，将 Rust 服务的专用 UID 映射到只读监听器。此路由只应用于设备视图；不要暴露管理器或主控制台。

在 Debian IPv4 节点上，可用 [专用路由示例](./deploy/ss-private-route.sh) 设置该私有地址，例如以 root 运行 `bash deploy/ss-private-route.sh ssmanager 8766`。参数分别是 Rust 服务的专用非特权账号和只读监听端口。该示例使用 iptables，允许 Google Cloud DNS 并限制该账号访问私网目标；重启后需在 Rust 服务启动前重新应用。首页设备摘要每 10 秒刷新一次，云端 CPU 等查询仍按需刷新。

**方式 B：安全的回环 SSH 转发。** 如果只读查看器监听节点的 `127.0.0.1:8766`，请在运行主控制台的同一台机器上保持以下 SSH 命令运行：

```bash
ssh -N -T -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18766:127.0.0.1:8766 your-ssh-user@your-vps.example
```

然后在另一个终端启动 Keel：

```bash
python3 keel.py serve --traffic-forward-port 18766
```

此参数将 `/api/traffic` 直接转发到 `127.0.0.1:18766`，并优先于 `trafficProxyPort`。它使用现有 SSH 访问，不增加公网监听器，也不转发管理器 Unix socket。请选择未使用且不同于主控制台 `--port` 的本机端口。如果两种方式都未配置，控制台和设备页面仍可打开，但会报告流量不可用。

## 云端行为与权限

Keel 使用参数数组、显式项目／可用区、捕获输出和有限超时执行以下命令：

```text
gcloud compute instances describe INSTANCE --project PROJECT --zone ZONE --format json
gcloud auth print-access-token
gcloud compute instances start INSTANCE --project PROJECT --zone ZONE --quiet
gcloud compute instances stop INSTANCE --project PROJECT --zone ZONE --quiet
```

每个 gcloud 子进程都使用 `stdin=subprocess.DEVNULL`，并在仅作用于该子进程的环境中设置 `CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API=false` 和 `CLOUDSDK_CORE_DISABLE_PROMPTS=true`。因此 CLI 和 HTTP 子进程都无法提示输入；Keel 不修改用户的 gcloud 配置或父进程环境。访问令牌仅在内存中用于 Monitoring 请求，不会打印或写入磁盘。云端错误经脱敏后才显示。

已登录身份只需具备所用操作对应的权限：

- `compute.instances.get`：读取状态和发现实例数字 ID。
- `monitoring.timeSeries.list`：读取指标。
- `compute.instances.start` 和 `compute.instances.stop`：执行对应操作。
- `compute.zoneOperations.get`：供同步 gcloud 命令等待操作完成。

读取操作只执行 `describe`、访问令牌获取和带 `view=FULL` 的 Monitoring `timeSeries` GET 请求，不启动云服务、不安装代理、不启用 API。指标使用真实数字实例 ID 和项目过滤，按 60 秒对齐；CPU 使用均值对齐，网络 INT64 `int64Value` DELTA 字节数按 `字节 × 8 ÷ 60 ÷ 1,000,000` 换算。不同网卡序列按时间戳求和，缺失分钟保持缺失，不补零。Monitoring 返回 `executionErrors` 或 `unreachable` 时会显示错误，而不是伪装成完整或空图表。

## 费用与边界

启动虚拟机可能产生计算费用。停止虚拟机后，持久磁盘、静态、保留或留存的公网 IP 等资源仍可能计费。临时外部 IPv4 地址可能在虚拟机停止并再次启动后改变。Keel 不显示虚构的账单总额、赠金、配额或实时余额，请通过固定的官方账单控制台链接查询。

Keel 不包含遥测、后台守护进程安装、部署自动化、自动启用 API 或远程访问功能。

## 离线测试

```bash
python3 -m unittest discover -s tests -v
```

测试只使用本地伪数据和 mock，不需要 `vps.local.json`、gcloud 登录、网络或真实虚拟机，覆盖指标分页／速率换算、网卡汇总、配置、子进程参数及写操作／Web 安全防护。

## 目录结构

- [`keel.py`](./keel.py)：命令行入口。
- [`keel/`](./keel/)：配置、gcloud、指标、受保护操作、本地 HTTP 服务及可选流量采集／查看模块。
- [`keel/static/`](./keel/static/)：无第三方依赖的中文网页控制台和设备流量视图。
- [`deploy/`](./deploy/)：不含机密的流量映射与 systemd 单元示例。
- [`tests/`](./tests/)：离线 `unittest` 回归测试。
- [`vps.example.json`](./vps.example.json)：配置模板。
