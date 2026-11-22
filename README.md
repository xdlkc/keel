# Keel

English | [简体中文](./README.zh-CN.md)

Keel is a Chinese-first local console for one existing Google Compute Engine VM, plus an offline generator for Outline Shadowsocks, Prometheus, and Grafana. The local console and offline generator use the Python standard library. Optional host installation and client verification are separate operator tools.

## Client and account setup

For a new client or a new Google account, start at [AGENTS.md](./AGENTS.md) and follow the [operator runbook](./docs/agent-onboarding.md). It covers credit verification, an isolated Google Cloud login, a dedicated Singapore VM, HTTPS subscription delivery and a client traffic check. On a fresh Debian 13 x86-64 host, the explicit installer configures Outline, Prometheus and authenticated Grafana. Reuse a free device slot or add a new one on an installer-managed host. Ordinary clients only need the shared subscription and their assigned slot.

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

The dashboard has native sections for device/key detail, host overview, pressure/disk, and network/TCP:

- An All-enabled, multi-select `device` variable lists observed `access_key` values. Configured keys are not a physical-device inventory or a device count.
- Client-side rates in bytes/second use `rate(...[$__rate_interval])`; estimated selected-range volume in bytes uses `increase(...[$__range])`.
- Only `shadowsocks_data_bytes` directions `c>p` and `c<p`, and protocols `tcp` and `udp`, are grouped by `access_key`, `dir`, and `proto`. Server-side directions are excluded to avoid double counting. TCP byte counters update when a connection closes, so an ongoing transfer can appear later as a spike.
- For selected non-empty authenticated keys, `increase(shadowsocks_tunnel_time_seconds{access_key=~"$device",access_key!=""}[$__range])` estimates tunnel time in seconds, and `shadowsocks_tcp_connections_closed` counts closed TCP connections by `access_key` and `status` over the selected range. The native tunnel counter sums active key/source-IP durations, so concurrent sources can accumulate more than wall-clock time; it is not physical device uptime, online status, or a last-seen indicator.
- Host panels ignore `device`. They cover host uptime, visible vCPUs and RAM, 1/5/15-minute load divided by visible vCPUs, CPU modes including iowait and steal, memory available, CPU/memory/IO PSI, OOM kills, and root filesystem space and inodes.
- Disk panels show separate read/write throughput, IOPS, mean latency in milliseconds, and busy time without summing devices. Generic filters exclude loop, RAM, optical, device-mapper, RAID, zram, NBD/RBD, and common partition devices. A mean read or write latency series is omitted when its window has zero corresponding operations rather than reporting a fake zero.
- Network panels retain per-interface rates and add non-loopback errors/drops, TCP retransmits, and available TCP inuse/time-wait socket gauges.
- No scrape job or instance value is hard-coded, and absent series receive no zero fallback; missing optional collector data stays “No data.” The dashboard does not invent source-IP inventories, OS or hardware models, or physical-device presence.

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
- [`deploy/outline/generate.py`](./deploy/outline/generate.py): offline inventory generator.
- [`deploy/outline/`](./deploy/outline/): inventory, Prometheus, Grafana datasource, and dashboard artifacts.
- [`tests/`](./tests/): offline regression tests.

The previous `keel/traffic.py`, `keel/traffic_server.py`, and `deploy/keel-traffic-*.service` files remain solely for manual legacy rollback; they are not linked or advertised by the current UI.
