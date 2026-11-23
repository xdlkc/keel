# 从干净的客户端或 Google 账号接入 Keel

入口是仓库根目录的 [AGENTS.md](../AGENTS.md)。以下命令供有该私有仓库读取权限的 agent 在 Linux 或 macOS 的 Bash 中运行。操作者电脑需要 Git、Python 3.9+、GitHub CLI；管理云资源时还需 Google Cloud CLI。用有权读取私有仓库的身份执行 `gh auth status`（首次可用 `gh auth login`），再执行 `gh repo clone xdlkc/keel` 和 `cd keel`。服务器无需登录 GitHub。每个新 Google 账号使用独立的本地目录与 CLI 登录会话。

## 1. 先判断接入哪台服务器

| 手头的条件 | 路径 |
| --- | --- |
| 新手机或电脑，继续使用原服务器 | 进入第 4 节。只需已有订阅及一个未分配的槽位，无需登录 Google 或创建项目。 |
| 新 Google 账号，要独立部署服务器 | 完成第 2、3 节，再完成客户端导入和第 5 节验收。新账号使用自己的账单、订阅密钥与 Grafana 账号。 |
| 新客户端还要管理实例启停 | 代理连接验收后，按第 6 节配置本地 Keel。实例管理使用另行授权的 Google 身份。 |

登记目标账号、项目、区域、实例名、设备 ID（例如 `phone-2`）、显示名（例如「手机 2」）和已有槽位分配。真实订阅、密钥、验证码和密码只放用户的密码管理器或私有交付文件。订阅包含全部槽位密钥，仅分发给同一信任范围内的设备。持有人可选任一槽位，两台设备选同一槽位会合并统计。

## 2. 新 Google 账号与费用边界

1. 在目标账号的 [Google Developer Program 权益页](https://developers.google.com/program/my-benefits) 领取对应档位的月度 Cloud 赠金，选择该账号自己的 Cloud Billing 账号。在 [Cloud Billing](https://console.cloud.google.com/billing) 核对赠金状态、余额、币种、适用服务和有效期，同时核对项目关联的结算账号。会员档位以及赠金额度以该账号实际显示为准。[官方权益说明](https://developers.google.com/profile/help/benefits)
2. 如果用户要求「一分钱也不额外扣」，在创建资源前确认该账号处于 Google 明示不会自动扣款的有效试用状态，或具备覆盖本次运行的等效硬性保护。已升级的按量付费账号即使有赠金或预算告警，也可能因流量超额而扣款。无法验证保护时可以完成本地准备，云端创建留待条件满足。不要替用户升级结算、购买承诺用量、补写身份或付款资料。[免费试用](https://cloud.google.com/free/docs/free-cloud-features) · [预算行为](https://cloud.google.com/billing/docs/how-to/budgets)
3. 确认用户授权的项目与地域。新加坡单机的参考起点为 `asia-southeast1-c`、`e2-medium`、10 GiB `pd-balanced`、一个保留的区域公网 IPv4，让设备订阅和 IP 证书在实例停止再启动后继续有效。估算时包含实例、系统盘、IPv4 和按目的地计价的公网出站流量；赠金余额与实际费用以账单为准。停止实例后磁盘和保留的 IPv4 仍可能计费。仅在永久放弃该地址和所有依赖它的链接时释放地址。

在仓库根目录打开 Bash。替换输入值并使用独立目录保存登录状态。用户按 Google 的交互提示完成身份验证。

```bash
set -euo pipefail
umask 077
PROJECT='your-globally-unique-project-id'
BILLING='your-billing-account-id'
REGION=asia-southeast1
ZONE=asia-southeast1-c
VM=keel-sg
PORT=18443
DEVICE_ID=computer-1
LABEL='电脑 1'
STATE="$HOME/.local/share/keel/$PROJECT"
mkdir -p "$STATE/gcloud"
chmod 700 "$STATE" "$STATE/gcloud"
export CLOUDSDK_CONFIG="$STATE/gcloud"
gcloud auth login
gcloud auth list --filter=status:ACTIVE --format='value(account)'
```

先在 Cloud Console 核对项目归属。需要新项目时执行下方前三行；复用已正确绑定的项目时从第四行开始。如果项目已存在于其他身份下，选另一个全局唯一 ID。组织策略可能要求指定组织或文件夹。

```bash
gcloud projects create "$PROJECT"
gcloud billing projects link "$PROJECT" --billing-account="$BILLING"
gcloud billing projects describe "$PROJECT"
gcloud config set project "$PROJECT"
test "$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')" = "billingAccounts/$BILLING"
gcloud services enable compute.googleapis.com iap.googleapis.com oslogin.googleapis.com --project="$PROJECT"
```

## 3. 新账号的独立服务器

以下创建块适用于新项目中的空命名空间。中途重试时先用对应的 `describe` 查看已经创建的网络、子网、防火墙与实例，核对区域、网段、机器类型、标签、来源范围、端口和磁盘，只补缺项；遇到名称冲突应换一组名称或确认资源归属。删资源重跑会丢失磁盘或订阅。

```bash
NET=keel-net
SUBNET=keel-sg-subnet
TAG=keel-outline
ADDRESS=keel-sg-ip
python3 - "$PORT" <<'PYPORT'
import sys
p=int(sys.argv[1])
if not 1024 <= p <= 65535 or p in {3000,9090,9091,9100}:
    raise SystemExit("Select an unprivileged port outside the management/metrics ports.")
PYPORT
gcloud compute addresses create "$ADDRESS" --region="$REGION" --network-tier=PREMIUM --project="$PROJECT"
RESERVED_IP=$(gcloud compute addresses describe "$ADDRESS" --region="$REGION" --project="$PROJECT" --format='value(address)')
gcloud compute networks create "$NET" --subnet-mode=custom --project="$PROJECT"
gcloud compute networks subnets create "$SUBNET" --network="$NET" --region="$REGION" --range=10.73.0.0/24 --project="$PROJECT"
gcloud compute firewall-rules create keel-iap-ssh --network="$NET" --target-tags="$TAG" --source-ranges=35.235.240.0/20 --allow=tcp:22 --project="$PROJECT"
gcloud compute firewall-rules create keel-client-ingress --network="$NET" --target-tags="$TAG" --source-ranges=0.0.0.0/0 --allow="tcp:80,tcp:443,tcp:$PORT,udp:$PORT" --project="$PROJECT"
gcloud compute instances create "$VM" --project="$PROJECT" --zone="$ZONE" \
  --machine-type=e2-medium --address="$RESERVED_IP" --network="$NET" --subnet="$SUBNET" --tags="$TAG" \
  --image-family=debian-13 --image-project=debian-cloud --boot-disk-type=pd-balanced \
  --boot-disk-size=10GB --no-service-account --no-scopes \
  --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE
IP=$(gcloud compute instances describe "$VM" --project="$PROJECT" --zone="$ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
```

首次 SSH 使用 IAP 与 OS Login。操作者需要目标实例的 IAP 隧道访问、实例查询和 OS Admin Login（用于 sudo）权限，由有权的管理员按最小范围授权。服务端代理端口、ACME HTTP-01 和 HTTPS 对客户端开放；采集器与 Grafana 后端只监听回环地址。[IAP 文档](https://cloud.google.com/iap/docs/using-tcp-forwarding)

<a id="iap-upload"></a>

把已检出的提交打包经 IAP 送到新 VM。下方使用固定的安全参数和标记名；改用其他设备显示名时，第 4 节的命令会处理 shell 引号。

```bash
ssh_vm() {
  gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap \
    --ssh-key-file="$STATE/iap_key" --ssh-key-expire-after=30m \
    --ssh-flag=-T --command="$1"
}
release=$(git rev-parse --verify HEAD)
remote_dir="keel-releases/$release"
ssh_vm 'sudo -n true'
git archive HEAD deploy/outline | ssh_vm "set -eu; umask 077; mkdir -p '$remote_dir'; tar -xf - -C '$remote_dir'"
quote_args() { python3 -c 'import shlex,sys; print(" ".join(map(shlex.quote,sys.argv[1:])))' "$@"; }
# 以下三行仅用于首次安装。构建机需要 Git、Python 3 和支持自动选择工具链的 Go 1.21+。
bash deploy/outline/tcpinfo/build.sh "$STATE/outline-ss-server"
ssh_vm "umask 077; cat > '$remote_dir/deploy/outline/host/outline-ss-server-linux-amd64'" < "$STATE/outline-ss-server"
ssh_vm "sudo bash $(quote_args "$remote_dir/deploy/outline/host/install.sh" "$IP" "$PORT" "$DEVICE_ID" "$LABEL")"
```

安装目标为干净的 Debian 13 x86-64 主机。安装程序使用正式的公网 IP 和受信任的免费证书；公网 80 必须能完成证书验证。变更地址后要重新核对 DNS／IP、证书、订阅中的主机地址和本地大盘入口。证书申请失败时先排查 IP 与防火墙，再按安装程序的恢复路径重试，保留原始密钥。已有用户／旧版代理的机器按第 4 节处理。

## 4. 接入客户端

**使用现有槽位。** 从用户的密码管理器或之前保存的私有交付文件取同一条 HTTPS 订阅，确认该槽位尚未分配。已有服务器的这一步不需要 SSH。Clash Verge 在「订阅」中导入该地址并激活；在「代理 → Keel Outline」选本机显示名一次。手机使用支持 Clash/Mihomo 配置及本地选择缓存的客户端。保留用户原有订阅。原生 Outline Client 使用单个访问密钥，不能直接导入这个多节点 Clash 订阅。

**从一台新的操作电脑管理已有服务器。** 若只导入已分配的订阅，跳过本段。向用户取得目标项目、可用区、实例名、公网地址和预期代理端口，独立设置 `PROJECT`、`ZONE`、`VM`、`PORT`、`STATE` 和 `CLOUDSDK_CONFIG`（如第 2 节），用获授权的账号登录。从 `gcloud compute instances describe` 读回 `IP` 并与交付地址核对。执行[第 3 节的 SSH 函数和代码上传](#iap-upload)，跳过该段最后三行的构建、二进制上传和首次安装命令。为已有服务器增加客户端不需要创建项目、地址或实例。若只想验收已有客户端，可直接设置 `IP`、`PORT`、`DEVICE_ID`、`LABEL` 并将第 5 节的 `--delivery` 指向用户原有的私有交付文件。

**需要新槽位。** 对本仓库安装程序管理的服务器，通过已授权的同一 SSH 会话和已上传的发行目录执行下方命令。每台设备用唯一 ID 与显示名。现有设备刷新同一订阅即可看到新名称。对以前手工部署的服务器，优先使用已有空槽位；若所有槽位都在用，应先核对当前清单与运行配置，尤其是兼容监听端口，再决定迁移。直接覆盖旧配置会断开仍使用旧端口的设备。

```bash
DEVICE_ID=phone-2
LABEL='手机 2'
ssh_vm "sudo bash $(quote_args "$remote_dir/deploy/outline/host/add-client.sh" "$DEVICE_ID" "$LABEL")"
```

**新安装的私有交付。** 只取用户侧资料到受限目录，避免在聊天、终端输出和 Git 中披露。新客户端本身仅需订阅地址；Grafana 的只读登录按需交给设备所有者。

```bash
delivery_tmp=$(mktemp "$STATE/delivery.XXXXXX")
ssh_vm 'sudo -n cat /etc/keel-outline/delivery.json' > "$delivery_tmp"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert all(d.get(k) for k in ("subscriptionUrl","dashboardUrl","username","password"))' "$delivery_tmp"
mv "$delivery_tmp" "$STATE/delivery.json"
```

## 5. 完成验收再交付

1. 验证 SSH 目标、实例公网 IP 和证书，并在真实客户端导入订阅、选择该设备。通过客户端发起一个会结束的请求。桌面使用该客户端实际的混合代理端口，例如客户端实际使用 7890 时，执行 `curl --noproxy '' --proxy http://127.0.0.1:7890 --max-time 15 -fsS -o /dev/null -w '%{http_code}\n' https://www.google.com/generate_204`。成功应为 204。另一台设备的节点不应被改变。
2. 只接入订阅的设备完成真实代理请求即可。等两个抓取周期后，由有只读资料的操作者在自己的电脑上运行以下验证，客户端无需取得后台密码。它会核验证书、目标地址、匿名访问限制、Viewer 角色、采集状态以及该 ID 的下行计数。退出码 2 表示交付资料可用，但仍需产生流量并等到采样。旧槽位已有历史计数时，另在 Grafana 选择该 ID、短时间窗口，对照刚才的测试确认其计数增长。UDP 在需要时用支持 UDP 的客户端完成一次 DNS 查询，再检查对应的 UDP 流量序列。
3. 在同一个客户端重启并刷新订阅，确认选中的名字保留。从手机／电脑打开交付文件中的大盘 URL，按设备筛选，机器资源图仍显示全机数据。交付给用户仓库提交、设备显示名、私有文件位置、可点击的大盘入口和实测结果，注明任何无法从当前环境验证的环节。

```bash
python3 deploy/outline/verify_client.py --delivery "$STATE/delivery.json" \
  --host "$IP" --port "$PORT" --device-id "$DEVICE_ID" --label "$LABEL"
```

## 6. 配置可以管理实例的工作站

为这个账号单独克隆一份 Keel，避免覆盖另一台实例的 `vps.local.json`。在第 2 节的同一 `CLOUDSDK_CONFIG` 环境中运行以下示例。用于普通代理访问的手机无需这些权限。

```bash
python3 - "$PROJECT" "$ZONE" "$VM" "$IP" <<'PY'
import json, os, sys
from pathlib import Path
project, zone, vm, ip = sys.argv[1:]
os.umask(0o077)
with Path("vps.local.json").open("x") as out:
    json.dump(dict(project=project, zone=zone, instance=vm,
                   dashboardUrl="https://" + ip + "/d/keel-outline/keel-outline"), out, indent=2)
PY
python3 keel.py status
python3 keel.py serve
```

打开 `http://127.0.0.1:8765` 核对实例与 Grafana 入口。遇到登录、账单或网络问题时保留原始订阅和资源，先核对当前账号、项目和服务器地址；切换账号不要求清除其他账号的登录状态。

## 7. 给已有 Outline 主机更新 TCP 采样

适用于 Debian 13 amd64、现有服务名为 `keel-outline.service` 的主机。先用 `systemctl show keel-outline.service -p ExecStart` 核对二进制路径，并从 Grafana 的大盘 API 核对 `meta.provisionedExternalId`。旧部署的大盘文件可能叫 `outline.json`，安装器创建的文件叫 `keel-outline.json`。使用第 3 节的 SSH 函数，只上传新二进制与大盘。所有设备继续使用原订阅和密钥。

1. 在操作电脑运行 `bash deploy/outline/tcpinfo/build.sh "$STATE/outline-ss-server"`。创建独立的远端暂存目录，上传该产物与 `deploy/outline/grafana-dashboard.json`。用 `sha256sum` 比对服务端产物与仓库中的 `deploy/outline/tcpinfo/build.sha256`。
2. 保存原二进制与实际使用的大盘文件到 root 专用备份目录。记录原版本、哈希及本地指标中的 `shadowsocks_keys` 数量。准备恢复命令，部署期间用定时器执行自动回滚，验收成功后取消定时器。如果管理连接也经过这台代理，让更新过程在服务器上独立运行；重新连接后先核对实际服务与文件哈希。
3. 在现有二进制所在目录暂存新版，验证哈希，再原子重命名到原路径并运行 `sudo systemctl restart keel-outline.service`。这会短暂断开已有 TCP 连接。核对服务恢复、密钥数一致，并确认 `shadowsocks_build_info` 报告 `1.9.2-keel-tcp1`。按同样方式原子替换现有大盘文件，让 Grafana 自动加载。
4. 用一台已配置的客户端发起有流量上限的 TCP 下载，在下载进行中核实该设备密钥出现 `device` 和 `source` 两段 RTT、下载速率，以及设备段重传与发送积压。检查原订阅和各监听端口。失败时，将备份的原二进制和大盘原子恢复到各自的原路径，重启同一服务并复核版本与密钥数。
