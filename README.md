# Keel

English | [简体中文](./README.zh-CN.md)

Keel is a Chinese-first local console for one configured Google Compute Engine VM. It uses only Python's standard library and local HTML/CSS/JavaScript. It is not a multi-instance manager, billing dashboard, deployment tool, agent installer, or background service.

## Prerequisites

- Python 3.9 or newer; no pip packages are required.
- Google Cloud CLI (`gcloud`) installed and authenticated with `gcloud auth login`.
- An existing Compute Engine VM, its project ID, zone, and instance name.
- Network access to Cloud Monitoring. The Monitoring API and required permissions must already be configured; Keel never enables APIs.

## Setup

Copy the example next to `keel.py`, then edit the three values:

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
- [`keel/`](./keel/): configuration, gcloud, metrics, guarded actions, and local HTTP server modules.
- [`keel/static/`](./keel/static/): dependency-free Chinese web console.
- [`tests/`](./tests/): offline `unittest` regression tests.
- [`vps.example.json`](./vps.example.json): configuration template.
