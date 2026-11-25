# Keel

English | [简体中文](./README.zh-CN.md)

Keel is a Chinese-first local console for one existing Google Compute Engine VM, plus an offline generator for Outline Shadowsocks, Prometheus, and Grafana. The local console and offline generator use the Python standard library. Optional host installation and client verification are separate operator tools.

## Client and account setup

For a new client or a new Google account, start at [AGENTS.md](./AGENTS.md) and follow the [operator runbook](./docs/agent-onboarding.md). It covers credit verification, an isolated Google Cloud login, a dedicated Singapore VM, HTTPS subscription delivery and a client traffic check. On a fresh Debian 13 x86-64 host, the explicit installer configures Outline, Prometheus and authenticated Grafana. Reuse a free device slot or add a new one on an installer-managed host. Ordinary clients only need the shared subscription and their assigned slot.

## Dual-node 443 TLS architecture

The live production deployment has moved from a single bare-Shadowsocks high port (`18443`) and 6-day IP certificate to a dual-node `443` TLS (Trojan + WebSocket) architecture backed by 90-day Let's Encrypt ECDSA domain certificates. Full topology, host paths, and operations procedures live in [docs/tls-443-architecture.md](./docs/tls-443-architecture.md), with sanitized templates in [`deploy/tls-gateway/`](./deploy/tls-gateway/).

- **Primary Singapore node (`sg`)**: `nginx` serves ACME HTTP-01 on port `80` and TLS on port `443`, proxying `/keel/mobile-ws` to a loopback-only `sing-box` gateway (`127.0.0.1:19080`, hardened with `IPAddressDeny=any` and `IPAddressAllow=localhost`). Four Trojan users (`computer-1`, `phone-1`, `computer-2`, `phone-2`) are routed by `auth_user` to loopback `outline-ss-server` outbounds so per-device Grafana and TCP leg metrics remain intact. A dedicated subscription hostname serves only the exact 256-bit random token path (`access_log off`, `Cache-Control: private, no-store`, `X-Robots-Tag: noindex, nofollow`), while `/` on the primary hostname serves Grafana.
- **Standby US Oregon node (`us`)**: a lightweight Always Free `e2-micro` host on the `STANDARD` network tier (200 GiB/month free egress) exposing only ports `80`/`443` (and SSH) with no bare proxy ports. `nginx` proxies `/keel/mobile-ws` to loopback `sing-box` (`direct` outbound with `ip_is_private` rejection to block RFC1918 and `169.254.169.254` metadata) and mirrors the same Clash subscription file, with TCP BBR enabled and `journald` capped at `100M`.
- **Unified dual-node Clash subscription**: both hosts mirror `/var/lib/keel-sub/profile.yaml`, served via loopback `keel-sub-server` (`127.0.0.1:19090`, `www-data`) behind nginx's exact token path. It exposes only two logical nodes (`新加坡 01` and `美国 01`) with `🚀 节点选择`, Singapore-first `🛟 故障转移 (新加坡优先)`, `♻️ 自动测速`, split-routing rules, and live `subscription-userinfo` + `profile-update-interval: 12` headers for the Clash traffic bar.
- **Monthly egress metering and auto circuit breaker**: both hosts run `/usr/local/bin/keel-quota` every 60 seconds (`keel-quota.timer`) against default-interface `/proc/net/dev` outbound bytes (persisted in `/var/lib/keel-quota/`, configured via mode `0600` `/etc/keel-quota/config.json`) and exchange monthly totals over HTTPS `/metrics-sync/<256-bit-token>.json`. Exceeding `200 GiB/month` combined—or `190 GiB/month` locally on the US standby host—stops only the proxy services while keeping SSH, nginx, Grafana, and certificate renewal running, then resets and restarts proxies automatically on the 1st of each month at `00:00 UTC`.
- **Simplified IP rotation and safety guardrail**: rotating an ephemeral public IP only requires updating DNS `A` records—90-day domain certificates and client subscription URLs remain valid. Do **not** run the legacy bare-IP / high-port installer or `generate.py` overwrite path in `deploy/outline/` directly against upgraded `443` TLS hosts.

## Requirements

- Python 3.9 or newer; no pip packages.
- An existing VM and authenticated `gcloud` for status, metrics, start, and stop.
- On the proxy host: `outline-ss-server` v1.9 or newer, Prometheus, `node_exporter`, and Grafana. The runbook includes a fresh-host installer.
- One available public port for Outline TCP and UDP. Keep Prometheus, node_exporter, and Outline metrics on loopback.

## Generate Outline and the shared subscription

Create a private inventory from the non-secret template, then replace every placeholder with a unique, cryptographically strong secret:

```bash
cp deploy/outline/inventory.example.json deploy/outline/inventory.local.json
chmod 600 deploy/outline/inventory.local.json
python3 deploy/outline/generate.py \
  --inventory deploy/outline/inventory.local.json \
  --output deploy/outline/generated
```

The inventory is one JSON object:

- `host`: the server's IPv4, IPv6, or DNS name, without a scheme or path.
- `port`: one integer from `1` to `65535`, shared by every device for both TCP and UDP.
- `devices`: an ordered list of explicit slots. Each has a unique printable `label`, a stable `id`, and a unique `secret`. IDs are 1–64 lowercase ASCII letters, digits, or interior hyphens, and are used as Outline `access_key` values. Secrets use `chacha20-ietf-poly1305`.

The inventory must be a non-symlink local file with no group/other permissions. The generator rejects duplicate IDs, labels, or secrets and never prints secret values. It creates a mode `0700` output directory and atomically replaces two deterministic mode `0600` files:

- `outline.yml`: the v1.9+ `services` → `listeners` / `keys` configuration, with TCP and UDP on the same port.
- `clash.yaml`: one `select` group named `Keel Outline`, one named SS node per device, and `profile.store-selected: true`. There is no automatic group. A direct rule for the configured server prevents TUN loops; all other traffic uses the selected node.

Both files are JSON-encoded YAML, which the relevant YAML parsers accept. Rerunning with identical input produces identical bytes. Never commit or log the private inventory, generated files, or subscription URL. Use a dedicated, owner-only output directory. A second writer is rejected while a generation transaction is active.

### Use one URL on all devices

Publish the same `clash.yaml` at one HTTPS URL through static hosting that the host owner already controls. The fresh-host installer can serve this file through nginx; the local console runs independently. Import the same subscription in every Clash/Mihomo client, select that device's own named node in `Keel Outline` once, and retain the client's local profile/cache so `store-selected` can restore it.

**Security boundary:** every subscription contains every device's secret. Any device or person with the URL can read all credentials; this is not per-device isolation or revocation. Treat the URL as a bearer secret, use an unguessable path and existing distribution controls, and rotate all exposed keys when the subscription leaks.

## Connect the host services

These are entry points for the host owner, not commands Keel runs:

```bash
outline-ss-server -config /etc/keel-outline/outline.yml -metrics 127.0.0.1:9091
node_exporter --web.listen-address=127.0.0.1:9100
prometheus --config.file=/etc/prometheus/keel.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --web.listen-address=127.0.0.1:9090
```

Install the generated `outline.yml` as `/etc/keel-outline/outline.yml` with owner-only access, and copy [`prometheus.example.yml`](./deploy/outline/prometheus.example.yml) to `/etc/prometheus/keel.yml`. The scrape example uses `127.0.0.1:9091` for Outline and `127.0.0.1:9100` for node_exporter without inventing metric labels. Permit the chosen Outline public port for both TCP and UDP in the host firewall.

For Grafana on the same host, install [`grafana-datasource.yml`](./deploy/outline/grafana-datasource.yml) in its datasource provisioning directory. It creates a Prometheus datasource with UID `keel-prom` at `http://127.0.0.1:9090`. Import [`grafana-dashboard.json`](./deploy/outline/grafana-dashboard.json); its UID is `keel-outline`, so its normal path is `/d/keel-outline/keel-outline`. Use Grafana's existing login and an existing HTTPS reverse proxy for remote access; Keel adds no authentication or public metrics listener.

The host installer provisions [Node Exporter Full, revision 45](https://grafana.com/grafana/dashboards/1860-node-exporter-full/) alongside Keel. For manual setup, import [the same revision](https://grafana.com/api/dashboards/1860/revisions/45/download) and select Keel Prometheus. The dashboard link opens the template with the current time range.

The device, TCP leg, host, disk, and network panels use these measurements.

- The device picker and Outline interval-detail panels include keys with a positive selected-interval increase in either historical Outline client traffic or `keel_tcp_download_bytes_total`. A first long-lived download can therefore be selected before Outline closes the connection. An active series keeps its idle periods in the rate chart, but missing samples can still hide a device.
- All panels follow Grafana's native selected time range; rolling rates use `$__rate_interval`. Byte rates use bytes per second, RTT uses seconds, and volume shows the estimated byte increase during the selected interval.
- Only `shadowsocks_data_bytes` directions `c>p` and `c<p`, and protocols `tcp` and `udp`, are grouped by `access_key`, `dir`, and `proto`. Server-side directions are excluded to avoid double counting. These Outline TCP byte counters update when a connection closes, so an ongoing transfer can appear later as a spike.
- The native **TCP 链路诊断** row filters by `access_key`. RTT and download series add only the `leg` label (`source` or `device`), never a source-IP label. Live mean RTT divides summed `keel_tcp_rtt_seconds_sum` by positive summed `keel_tcp_rtt_samples`; both gauges describe currently open sockets.
- The download panel uses `rate(keel_tcp_download_bytes_total)` to compare source-leg payload received from the origin with device-leg encrypted TCP bytes acknowledged by the client. These are deliberately different byte semantics.
- The retransmission panel uses `rate(keel_tcp_device_retrans_bytes_total)` for encrypted TCP bytes retransmitted from the VPS toward the client. The backlog panel sums the `keel_tcp_device_notsent_bytes` gauge for currently unsent bytes in the same direction. Missing samples remain gaps rather than zeroes.
- Tunnel time estimates active key/source-IP seconds. Concurrent connections from distinct source IPs can accumulate more than wall-clock time. TCP closes are grouped by key and status.
- Host panels ignore `device`. They cover host uptime, visible vCPUs and RAM, 1/5/15-minute load divided by visible vCPUs, CPU modes including iowait and steal, memory available, CPU/memory/IO PSI, OOM kills, and root filesystem space and inodes.
- Disk panels show separate read/write throughput, IOPS, mean latency in milliseconds, and busy time without summing devices. Generic filters exclude loop, RAM, optical, device-mapper, RAID, zram, NBD/RBD, and common partition devices. A mean read or write latency series is omitted when its window has zero corresponding operations rather than reporting a fake zero.
- Network panels retain per-interface rates and add non-loopback errors/drops, TCP retransmits, and available TCP inuse/time-wait socket gauges.
- Protocol-health panels add TCP retransmits as a percentage of sent segments with a positive-denominator guard, SYN retransmits and timeouts, listen overflows/drops, UDP datagrams and receive/send buffer errors, and Outline UDP NAT entry additions/removals. These are not per-device series.
- No scrape job or instance value is hard-coded, and absent series receive no zero fallback; missing optional collector data stays “No data.” The dashboard does not invent source-IP inventories, OS or hardware models, or physical-device presence.

The TCP diagnostics also keep a stalled device visible while its send queue has a backlog or retransmitted bytes increase. RTT is averaged over live connections at each scrape. Short connections may finish between scrapes, and acknowledgements arriving after socket close are outside the final sample. The installer uses the [reviewed Outline build](./deploy/outline/tcpinfo/build.sh). Follow the [operator runbook](./docs/agent-onboarding.md) when updating a manually managed host.

## Local VM console

Copy and edit [`vps.example.json`](./vps.example.json):

```bash
cp vps.example.json vps.local.json
python3 keel.py serve
```

Open `http://127.0.0.1:8765`; change the port with `--port`. The server binds only to loopback. The Chinese home page contains instance status, project, zone, machine type, boot disk, public IP, update time, refresh, and the existing guarded start/stop confirmations. Rust traffic views and Cloud Monitoring charts are not part of the current UI.

`dashboardUrl` is optional. It must be an ASCII HTTPS URL without user-info credentials; HTTP is accepted only for `localhost` or an IP loopback address. Do not put API tokens or passwords in the URL. When configured, the home page shows one Grafana link. `/devices` ignores all request parameters and redirects with `Cache-Control: no-store` to that fixed configured URL, or to `/` when unset, so bookmarks cannot become open redirects.

Example remote and local-only values are:

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

Start is enabled only for `TERMINATED`; stop only for `RUNNING`. Both require the exact instance name, a same-origin request, and a per-process CSRF token. The server reloads the target configuration before invoking gcloud and never automatically replays a rejected action.

## CLI and existing APIs

```bash
python3 keel.py status
python3 keel.py metrics
python3 keel.py serve --port 8765
python3 keel.py start --confirm your-vm-instance-name
python3 keel.py stop --confirm your-vm-instance-name
```

`status` reports the current VM details. `metrics` prints the last hour of Cloud Monitoring CPU percentages and outgoing network rates; it requires the existing Monitoring API, permission, and network access. `/api/status` and `/api/metrics` remain available even though the home page no longer draws cloud charts. A successful start/stop message means the synchronous gcloud command exited, not that the desired state was observed; run `status` to confirm.

Existing `trafficProxyPort` configuration and the legacy traffic CLI/API remain parse-compatible for rollback. They are not the current Outline observability path.

## Costs and boundaries

A running VM, outbound network traffic, persistent disks, and static or retained public IPs can incur charges. Stopping a VM does not necessarily stop disk or IP charges, and an ephemeral external IPv4 address can change after restart. Outline, Prometheus, Grafana, and node_exporter also consume host CPU, memory, disk, and network; retained Prometheus samples increase storage use. Keel shows no invented bill or quota—use the provider's billing console.

## Offline verification

```bash
python3 -m unittest discover -s tests -v
```

Tests use local fakes, temporary files, and loopback HTTP servers only. They do not require credentials, gcloud, cloud resources, external network access, Outline, Prometheus, or Grafana.

## Layout

- [`keel.py`](./keel.py): VM CLI entry point.
- [`keel/`](./keel/): configuration, guarded gcloud actions, metrics, and local HTTP server.
- [`keel/static/`](./keel/static/): streamlined Chinese console.
- [`docs/tls-443-architecture.md`](./docs/tls-443-architecture.md): dual-node 443 TLS architecture, DNS topology, unified Clash subscription, monthly quota/circuit-breaker, and operations guide.
- [`docs/agent-onboarding.md`](./docs/agent-onboarding.md): Google Cloud and single-host Outline operator onboarding runbook.
- [`deploy/tls-gateway/`](./deploy/tls-gateway/): sanitized nginx, sing-box, systemd, sysctl, journald, quota/sub-server, and unified Clash templates for the dual-node 443 TLS stack.
- [`deploy/outline/generate.py`](./deploy/outline/generate.py): offline Outline inventory generator (legacy bare-Shadowsocks layout).
- [`deploy/outline/`](./deploy/outline/): inventory, Prometheus, Grafana datasource, dashboard, and legacy host installer artifacts.
- [`tests/`](./tests/): offline regression tests.

The previous `keel/traffic.py`, `keel/traffic_server.py`, and `deploy/keel-traffic-*.service` files remain solely for manual legacy rollback; they are not linked or advertised by the current UI.
