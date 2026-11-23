#!/bin/bash
set -Eeuo pipefail

if [[ $# -ne 4 ]]; then
    printf 'Usage: sudo bash %s PUBLIC_IPV4 SS_PORT DEVICE_ID LABEL\n' "$0" >&2
    exit 2
fi
PUBLIC_IPV4=$1
SS_PORT=$2
DEVICE_ID=$3
LABEL=$4

# shellcheck source=common.sh
source "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"
require_platform
"${HOSTCTL[@]}" validate-install "$PUBLIC_IPV4" "$SS_PORT" "$DEVICE_ID" "$LABEL"
require_sources
acquire_lock
detect_managed
check_path_shapes
preflight_unmanaged "$SS_PORT"
if (( MANAGED == 0 )); then
    install -d -m 0700 -o root -g root /etc/keel-outline
fi
"${HOSTCTL[@]}" begin "$PUBLIC_IPV4" "$SS_PORT" "$DEVICE_ID" "$LABEL"
"${HOSTCTL[@]}" access

WORK=$(make_workdir keel-outline-install)
cleanup() {
    local rc=$?
    trap - EXIT
    if [[ ${WORK:-} == /var/tmp/keel-outline-install.* && -d $WORK ]]; then
        rm -rf -- "$WORK"
    fi
    if (( rc != 0 )); then
        printf 'Installation stopped; rerun the same command to resume without rotating credentials.\n' >&2
    fi
    exit "$rc"
}
trap cleanup EXIT

python3 "$OUTLINE_DIR/enroll.py" \
    --inventory /etc/keel-outline/inventory.json \
    --host "$PUBLIC_IPV4" --port "$SS_PORT" \
    --device-id "$DEVICE_ID" --label "$LABEL" >/dev/null
python3 "$OUTLINE_DIR/generate.py" \
    --inventory /etc/keel-outline/inventory.json \
    --output "$WORK/generated" >/dev/null

LOG=/var/log/keel-outline-install.log
touch "$LOG"
chmod 0600 "$LOG"
run_logged() {
    (umask 022; "$@") >>"$LOG" 2>&1
}
CURL=(curl --fail --silent --show-error --location --max-time 120 --proto '=https' --proto-redir '=https')

run_logged apt-get update
run_logged apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg python3 python3-venv tar util-linux
"${CURL[@]}" https://apt.grafana.com/gpg.key -o "$WORK/grafana.asc"
expected_fingerprint=B53AE77BADB630A683046005963FA27710458545
gpg --show-keys --with-colons "$WORK/grafana.asc" 2>/dev/null \
    | awk -F: '$1 == "fpr" {print $10}' \
    | grep -Fqx "$expected_fingerprint"
install -d -m 0755 /etc/apt/keyrings
install -m 0644 "$WORK/grafana.asc" /etc/apt/keyrings/keel-grafana.asc
printf 'deb [signed-by=/etc/apt/keyrings/keel-grafana.asc] https://apt.grafana.com stable main\n' \
    >"$WORK/keel-grafana.list"
install -m 0644 "$WORK/keel-grafana.list" /etc/apt/sources.list.d/keel-grafana.list
if [[ ! -f /etc/keel-outline/packages-configured ]]; then
    run_logged systemctl mask nginx.service grafana-server.service prometheus.service \
        prometheus-node-exporter.service
fi
run_logged apt-get update
run_logged apt-get install -y --no-install-recommends \
    nginx prometheus prometheus-node-exporter grafana

install -d -m 0700 -o root -g root /etc/keel-outline /opt/keel-outline
install -d -m 0750 -o root -g www-data /var/lib/keel-sub
install -d -m 0755 -o root -g root /var/lib/keel-dashboards /var/www/keel-acme
install -d -m 0755 -o root -g root \
    /etc/systemd/system/grafana-server.service.d

if [[ ! -x /opt/keel-certbot/bin/certbot ]]; then
    run_logged python3 -m venv /opt/keel-certbot
fi
if ! /opt/keel-certbot/bin/python - <<'PY'
import importlib.metadata
import sys
version = tuple(int(part) for part in importlib.metadata.version("certbot").split(".")[:2])
sys.exit(0 if version >= (5, 4) and version < (6, 0) else 1)
PY
then
    run_logged /opt/keel-certbot/bin/pip --disable-pip-version-check install 'certbot>=5.4,<6'
fi
/opt/keel-certbot/bin/python - <<'PY'
import importlib.metadata
version = tuple(int(part) for part in importlib.metadata.version("certbot").split(".")[:2])
if not (5, 4) <= version < (6, 0):
    raise SystemExit(1)
PY

outline_version=''
if [[ -x /usr/local/bin/outline-ss-server ]]; then
    outline_version=$(/usr/local/bin/outline-ss-server -version 2>&1 || true)
fi
if [[ $outline_version != *1.9.2* ]]; then
    release_url=https://github.com/OutlineFoundation/tunnel-server/releases/download/v1.9.2
    archive=outline-ss-server_1.9.2_linux_x86_64.tar.gz
    "${CURL[@]}" "$release_url/$archive" -o "$WORK/outline.tar.gz"
    printf '%s  outline.tar.gz\n' 6166cb676334e9f8a3296621d6a29b0e0cce36e4b0a72600b3309b07a21f5b8c >"$WORK/archive.sha256"
    (cd "$WORK" && run_logged sha256sum -c archive.sha256)
    tar -xzf "$WORK/outline.tar.gz" -C "$WORK" outline-ss-server
    install -m 0755 "$WORK/outline-ss-server" /usr/local/bin/outline-ss-server
fi

install -m 0644 "$HOST_DIR/outline.service" /etc/systemd/system/keel-outline.service
install -m 0644 "$HOST_DIR/cert-renew.service" /etc/systemd/system/keel-cert-renew.service
install -m 0644 "$HOST_DIR/cert-renew.timer" /etc/systemd/system/keel-cert-renew.timer
install -m 0644 "$HOST_DIR/grafana.conf" /etc/systemd/system/grafana-server.service.d/keel.conf
install -m 0644 "$HOST_DIR/prometheus.default" /etc/default/prometheus
install -m 0644 "$HOST_DIR/prometheus-node-exporter.default" /etc/default/prometheus-node-exporter
install -m 0644 "$OUTLINE_DIR/prometheus.example.yml" /etc/prometheus/prometheus.yml
install -m 0644 "$OUTLINE_DIR/grafana-datasource.yml" /etc/grafana/provisioning/datasources/keel.yaml
install -m 0644 "$HOST_DIR/grafana-dashboards.yaml" /etc/grafana/provisioning/dashboards/keel.yaml
# Grafana community dashboard 1860, revision 45 (upstream UID rYdddlPWk).
"${CURL[@]}" https://grafana.com/api/dashboards/1860/revisions/45/download -o "$WORK/node-exporter-full.json"
printf '%s  node-exporter-full.json\n' 184c6b7409f306da75525d7772f71945b10cea23ad16b5d78c4698ea0ea51986 >"$WORK/dashboard.sha256"
(cd "$WORK" && run_logged sha256sum -c dashboard.sha256)
install -m 0644 "$WORK/node-exporter-full.json" /var/lib/keel-dashboards/node-exporter-full.json
install -m 0644 "$OUTLINE_DIR/grafana-dashboard.json" /var/lib/keel-dashboards/keel-outline.json
"${HOSTCTL[@]}" grafana-env "$PUBLIC_IPV4"
"${HOSTCTL[@]}" publish-install "$WORK/generated"
if [[ -e /etc/nginx/sites-enabled/default || -L /etc/nginx/sites-enabled/default ]]; then
    [[ -L /etc/nginx/sites-enabled/default ]] || fail "the nginx default site is not the package symlink."
    [[ $(readlink /etc/nginx/sites-enabled/default) == /etc/nginx/sites-available/default ]] \
        || fail "the nginx default symlink has an unexpected target."
    unlink /etc/nginx/sites-enabled/default
fi
certificate_ready=0
if [[ -s /etc/letsencrypt/live/keel-outline/fullchain.pem && -s /etc/letsencrypt/live/keel-outline/privkey.pem ]]; then
    certificate_ready=1
fi
if (( certificate_ready == 1 )); then
    "${HOSTCTL[@]}" nginx "$PUBLIC_IPV4" https
else
    "${HOSTCTL[@]}" nginx "$PUBLIC_IPV4" http
fi
if [[ -e /etc/nginx/sites-enabled/keel-outline || -L /etc/nginx/sites-enabled/keel-outline ]]; then
    [[ -L /etc/nginx/sites-enabled/keel-outline ]] \
        && [[ $(readlink /etc/nginx/sites-enabled/keel-outline) == /etc/nginx/sites-available/keel-outline ]] \
        || fail "the managed nginx site symlink is unsafe."
else
    ln -s /etc/nginx/sites-available/keel-outline /etc/nginx/sites-enabled/keel-outline
fi

run_logged promtool check config /etc/prometheus/prometheus.yml
run_logged nginx -t
touch /etc/keel-outline/packages-configured
run_logged systemctl unmask nginx.service grafana-server.service prometheus.service prometheus-node-exporter.service
run_logged systemctl daemon-reload
run_logged systemctl enable --now keel-outline.service prometheus.service prometheus-node-exporter.service
run_logged systemctl try-restart keel-outline.service prometheus.service prometheus-node-exporter.service
"${HOSTCTL[@]}" wait-key "$DEVICE_ID"
run_logged systemctl enable --now grafana-server.service
run_logged systemctl try-restart grafana-server.service
grafana_ready=0
for _ in $(seq 1 60); do
    if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:3000/api/health >/dev/null 2>&1; then
        grafana_ready=1
        break
    fi
    sleep 1
done
(( grafana_ready == 1 )) || fail "Grafana did not become ready on loopback."
"${HOSTCTL[@]}" ensure-viewer

run_logged systemctl enable --now nginx.service
run_logged systemctl restart nginx.service
install -d -m 0755 /var/www/keel-acme/.well-known /var/www/keel-acme/.well-known/acme-challenge
(umask 022; run_logged /opt/keel-certbot/bin/certbot certonly \
    --non-interactive --agree-tos --register-unsafely-without-email \
    --webroot -w /var/www/keel-acme --preferred-profile shortlived \
    --ip-address "$PUBLIC_IPV4" --cert-name keel-outline --keep-until-expiring)
"${HOSTCTL[@]}" nginx "$PUBLIC_IPV4" https
run_logged nginx -t
run_logged systemctl reload nginx.service
run_logged systemctl enable --now keel-cert-renew.timer

for unit in keel-outline prometheus prometheus-node-exporter grafana-server nginx keel-cert-renew.timer; do
    systemctl is-active --quiet "$unit"
done
systemctl is-enabled --quiet keel-cert-renew.timer
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:9090/-/ready >/dev/null
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:9100/metrics >/dev/null
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:3000/api/health >/dev/null
curl --fail --silent --show-error --max-time 10 --noproxy '*' \
    --resolve "$PUBLIC_IPV4:443:127.0.0.1" "https://$PUBLIC_IPV4/api/health" >/dev/null
"${HOSTCTL[@]}" delivery "$PUBLIC_IPV4"
printf 'Install complete. Private delivery: /etc/keel-outline/delivery.json\n'
