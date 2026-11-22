# Keel

English | [简体中文](./README.zh-CN.md)

Keel is a Chinese-first local console for one configured Google Compute Engine VM. It uses only Python's standard library and local HTML/CSS/JavaScript. The main console is not a multi-instance manager, billing dashboard, deployment tool, agent installer, or background service. An optional, separate ssmanager collector adds per-device traffic views without creating GCP resources.

## Prerequisites

- Python 3.9 or newer; no pip packages are required.
- Google Cloud CLI (`gcloud`) installed and authenticated with `gcloud auth login`.
- An existing Compute Engine VM, its project ID, zone, and instance name.
- Network access to Cloud Monitoring. The Monitoring API and required permissions must already be configured; Keel never enables APIs.

## Setup

Copy the example next to `keel.py`, then edit the three required values:

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

`vps.local.json`, common credential files, and Python caches are git-ignored. Keel does not create service-account keys or store access tokens. Configuration must be valid UTF-8 JSON. Project, zone, and instance identifiers are validated before any subprocess runs; option-like names are rejected, while standard and legacy domain-scoped project IDs are supported.

## CLI

Run commands from the repository root:

```bash
python3 keel.py status
python3 keel.py metrics
python3 keel.py serve
python3 keel.py serve --port 8765
python3 keel.py start --confirm your-vm-instance-name
python3 keel.py stop --confirm your-vm-instance-name
```

- `status` shows state, zone, machine type, boot disk size, and public IP.
- `metrics` prints the last hour of per-minute CPU percentage and outgoing network in Mbit/s.
- `start` and `stop` require an exact `--confirm` instance name. Without it, or with a different name, Keel does not invoke the operation.
- Success means the synchronous gcloud command exited successfully, not that a desired state was observed. Run `status` to verify the actual state.

## Local web console

```bash
python3 keel.py serve
```

Open `http://127.0.0.1:8765`. The default port is `8765`; `--port` changes it. The server binds only to `127.0.0.1`, never all interfaces. Keep it in the foreground and press Ctrl+C to exit.

The Chinese UI provides refresh, real start/stop confirmation dialogs, one-hour SVG trend charts, empty and missing-data states, and official links to the project console, Monitoring, and the fixed billing console. Start is enabled only for `TERMINATED`; stop is enabled only for `RUNNING`. Each dialog displays and pins the project, zone, and instance shown when it opens. The server reloads configuration and rejects any target mismatch before invoking gcloud, even when another project or zone has the same instance name. Actions require a per-process session CSRF token and a same-origin request. A rejected token is cleared and requires a new explicit confirmation; Keel never replays the action automatically. Invalid `Host` headers and cross-origin requests are rejected to reduce CSRF and DNS-rebinding risk.

After each home refresh, a compact device section independently requests the same-origin `/api/traffic?limit=1` endpoint. Cloud or session errors do not suppress this request, and traffic errors do not alter the cloud charts. A failed traffic request clears the previous summary values and shows a short unavailable status. The full read-only view is at `/devices`.

## Optional per-device traffic

Keel can poll an existing shadowsocks-rust `ssmanager` Unix datagram socket and expose a separate read-only device page. This uses the existing traffic node and a private SQLite file; it creates no GCP VMs, service accounts, metrics, or other resources and enables no APIs. It does not install ssmanager or replace its configuration.

### Accounting boundaries

- The manager's `stat` counters are sampled per server port. RX and TX are summed: Keel reports only combined bytes and bytes per second, never separate directions.
- Assign each physical device its own server port and dedicated Shadowsocks (SS) credential. A shared port or credential yields only a shared total and cannot identify individual devices. `shared: true` keeps such legacy entries explicitly labelled.
- `totalBytes` is the monitored total accumulated since Keel first observed the port, not the manager's earlier history or a GCP bill. There is no historical backfill. Missing samples remain missing; Keel does not invent rates for them.
- Run exactly one continuous collector (one watcher) for a manager and its database. Do not run duplicate collectors against the same manager mapping or SQLite file.
- First observation, collection interruption, a missing port, or a manager/counter reset makes the current rate unavailable until new continuous samples establish a baseline. Counter decreases never become negative usage. The last monitored total can remain visible while the current rate is unavailable; stale samples (30 seconds by default) also have no current rate.
- The collector creates a mode `0600` database under a mode `0700` state directory. `--history` bounds retained samples per port (default `360`, allowed range `10`–`100000`). The static mapping contains labels, not credentials, client IP addresses, or client hostnames.

### Deploy on the existing traffic node

The examples use these paths and names; change them consistently for your host:

- Code: `/opt/keel`; mapping: `/etc/keel/traffic.json`.
- Database: `/var/lib/keel-traffic/traffic.db`; dedicated client socket directory: `/run/keel-traffic`.
- Existing manager: `ssmanager.service`, running as user and group `ssmanager`, with its mode `0700` Unix socket at `/run/ssmanager/manager.sock`.
- Read-only web listener: `127.0.0.1:8766`.

Use the existing manager's unprivileged account, not `root`. Provision the manager socket separately and do not loosen its `0700` access. Both Keel units deliberately use that same account so the collector can reach the socket and both can access the private database. `RuntimeDirectory=keel-traffic` and `RuntimeDirectoryMode=0700` create the collector's dedicated client directory.

From this checkout, provision the example as follows:

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

Set real per-device ports and labels in the mapping; it must not contain SS passwords or other secrets. If the manager account, unit, socket, or paths differ, update both unit files and the mapping together. The collector is restricted to `AF_UNIX` and its state/runtime paths. The viewer is restricted to `AF_INET` only, cannot access either manager or client Unix sockets, and opens the database with SQLite `mode=ro` and `PRAGMA query_only=ON`; systemd also mounts its database path read-only. The viewer serves only `http://127.0.0.1:8766/devices` and contains no instance-management or gcloud routes.

### Connect the main console

Choose one of two local connection methods. In both cases the main console remains on `127.0.0.1` and exposes only the read-only `/api/traffic` proxy, not the manager socket.

**Option A: an existing local HTTP proxy.** Add `trafficProxyPort` to `vps.local.json`:

```json
{
  "project": "your-gcp-project-id",
  "zone": "us-central1-a",
  "instance": "your-vm-instance-name",
  "trafficProxyPort": 17890
}
```

The value is optional, but when present it must be a JSON integer from `1` to `65535`. It identifies an existing HTTP proxy on local port `17890` in this example; Keel does not start or configure that proxy. Keel sends an absolute-form request for the fixed upstream `http://192.0.2.80/api/traffic` (with the requested `limit` query) through `127.0.0.1:17890`, with `Host: 192.0.2.80`. Configure the existing proxy and node routing so this address reaches only the read-only listener.

For private phone access, open `http://192.0.2.80/devices`. In the phone's existing proxy client, add a routing rule for destination `192.0.2.80` that uses that phone's dedicated device SS credential, not a shared or another device's credential. On the traffic node, map the Rust service's dedicated UID to the read-only listener. Keep this route limited to the device view; do not expose the manager or main console.

On an IPv4 Debian node, the [private route example](./deploy/ss-private-route.sh) can install the route. Run `bash deploy/ss-private-route.sh ssmanager 8766` as root with the dedicated non-root Rust service account and viewer port. It uses iptables, permits Google Cloud DNS and restricts access to private destinations for that account. Apply it before the Rust service starts after each reboot. The home device summary refreshes every 10 seconds independently of on-demand cloud metrics.

**Option B: a safe loopback SSH forward.** If the read-only viewer listens on the node's `127.0.0.1:8766`, keep this SSH command running on the same machine as the main console:

```bash
ssh -N -T -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18766:127.0.0.1:8766 your-ssh-user@your-vps.example
```

Then start Keel in another terminal:

```bash
python3 keel.py serve --traffic-forward-port 18766
```

The flag forwards `/api/traffic` directly to `127.0.0.1:18766` and takes precedence over `trafficProxyPort`. This uses your existing SSH access, adds no public listener, and does not forward the manager Unix socket. Choose an unused local port different from the main `--port`. If neither connection method is configured, the console and device page remain available but report traffic as unavailable.

## Cloud behavior and privileges

Keel uses these commands with argument arrays, explicit project/zone, captured output, and bounded timeouts:

```text
gcloud compute instances describe INSTANCE --project PROJECT --zone ZONE --format json
gcloud auth print-access-token
gcloud compute instances start INSTANCE --project PROJECT --zone ZONE --quiet
gcloud compute instances stop INSTANCE --project PROJECT --zone ZONE --quiet
```

Every gcloud child receives `stdin=subprocess.DEVNULL` and a per-child environment with `CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API=false` and `CLOUDSDK_CORE_DISABLE_PROMPTS=true`. CLI and HTTP children therefore cannot prompt; Keel does not change the user's gcloud configuration or parent environment. The access token is used only in memory for Monitoring requests and is never printed or persisted. Cloud errors are sanitized before display.

The authenticated identity needs only the operations you use:

- `compute.instances.get` for status and instance ID discovery.
- `monitoring.timeSeries.list` for metrics.
- `compute.instances.start` and `compute.instances.stop` for the corresponding actions.
- `compute.zoneOperations.get` for synchronous gcloud operation waits.

Read operations run only `describe`, access-token retrieval, and Monitoring `timeSeries` GET requests with `view=FULL`. They do not start cloud services, install agents, or enable APIs. Metrics use the actual numeric instance ID and project filter, a 60-second alignment period, CPU mean alignment, and INT64 `int64Value` DELTA byte counts converted with `bytes × 8 ÷ 60 ÷ 1,000,000`. Distinct NIC series are summed by timestamp; missing minutes remain missing rather than becoming zero. Monitoring `executionErrors` or `unreachable` responses are shown as errors rather than complete or empty charts.

## Costs and scope

Starting a VM can incur compute charges. Stopping a VM can still leave billable resources, including persistent disks and static, reserved, or retained public IP addresses. An ephemeral external IPv4 address can change after the VM is stopped and started again. Keel shows no fabricated billing totals, credits, quotas, or live balance; use the fixed link to the official billing console.

Keel has no telemetry, daemon installer, deployment automation, automatic API enablement, or remote-access feature.

## Offline tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use local fakes and mocks only. They do not need `vps.local.json`, gcloud authentication, network access, or a real VM, and cover metric pagination/rate conversion, NIC aggregation, configuration, subprocess arguments, and write-action/web security guards.

## Layout

- [`keel.py`](./keel.py): CLI entry point.
- [`keel/`](./keel/): configuration, gcloud, metrics, guarded actions, local HTTP server, and optional traffic collector/viewer modules.
- [`keel/static/`](./keel/static/): dependency-free Chinese web console and device traffic view.
- [`deploy/`](./deploy/): secret-free traffic mapping and systemd unit examples.
- [`tests/`](./tests/): offline `unittest` regression tests.
- [`vps.example.json`](./vps.example.json): configuration template.
