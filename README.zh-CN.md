# Keel

[English](./README.md) | 简体中文

Keel 是一个中文优先的本地控制台，只管理一台已配置的 Google Compute Engine 虚拟机。它仅使用 Python 标准库和本地 HTML/CSS/JavaScript，不是多实例管理器、账单面板、部署工具、代理安装器或后台服务。

## 前置条件

- Python 3.9 或更高版本，无需安装 pip 依赖。
- 已安装 Google Cloud CLI（`gcloud`），并已通过 `gcloud auth login` 登录。
- 一台现有的 Compute Engine 虚拟机，以及其项目 ID、可用区和实例名。
- 可访问 Cloud Monitoring 的网络。Monitoring API 和所需权限必须预先配置，Keel 不会启用 API。

## 配置

在 `keel.py` 旁边复制示例文件，然后填写三个值：

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
- [`keel/`](./keel/)：配置、gcloud、指标、受保护操作和本地 HTTP 服务模块。
- [`keel/static/`](./keel/static/)：无第三方依赖的中文网页控制台。
- [`tests/`](./tests/)：离线 `unittest` 回归测试。
- [`vps.example.json`](./vps.example.json)：配置模板。
