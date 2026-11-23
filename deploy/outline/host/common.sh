HOST_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
OUTLINE_DIR=$(CDPATH= cd -- "$HOST_DIR/.." && pwd -P)
HOSTCTL=(python3 "$HOST_DIR/hostctl.py")
LOCK_FILE=/run/keel-outline.lock
MANAGED=0

fail() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

require_platform() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run this entry point with sudo as root."
    [[ -r /etc/os-release ]] || fail "Debian 13 is required."
    . /etc/os-release
    [[ ${ID:-} == debian && ${VERSION_ID:-} == 13 ]] || fail "Debian 13 is required."
    [[ $(dpkg --print-architecture) == amd64 ]] || fail "the amd64 architecture is required."
    command -v systemctl >/dev/null 2>&1 || fail "systemd is required."
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    export DEBIAN_FRONTEND=noninteractive
    umask 077
}

acquire_lock() {
    [[ ! -L $LOCK_FILE ]] || fail "the serialization lock path is unsafe."
    if [[ -e $LOCK_FILE && ! -f $LOCK_FILE ]]; then
        fail "the serialization lock path is not a regular file."
    fi
    exec 9>>"$LOCK_FILE"
    chmod 0600 "$LOCK_FILE"
    flock -w 60 9 || fail "another Keel host operation is still running."
}

detect_managed() {
    [[ ! -L /etc/keel-outline/state.json ]] || fail "managed state cannot be a symlink."
    if [[ -f /etc/keel-outline/state.json ]]; then
        MANAGED=1
    elif [[ -e /etc/keel-outline/state.json ]]; then
        fail "managed state is not a regular file."
    else
        MANAGED=0
    fi
}

preflight_unmanaged() {
    local ss_port=$1
    local package status path unit directory
    (( MANAGED == 0 )) || return 0
    local packages=(
        nginx nginx-common nginx-core nginx-full grafana prometheus
        prometheus-node-exporter certbot outline-ss-server
    )
    for package in "${packages[@]}"; do
        status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$package" 2>/dev/null || true)
        [[ $status != ii* ]] || fail "an existing unrelated package must be removed or the host replaced."
    done
    local paths=(
        /etc/keel-outline /var/lib/keel-sub /var/lib/keel-dashboards
        /opt/keel-outline /opt/keel-certbot /var/www/keel-acme
        /usr/local/bin/outline-ss-server /etc/letsencrypt
        /etc/nginx /etc/grafana /etc/prometheus /var/lib/grafana /var/lib/prometheus
        /etc/apt/keyrings/keel-grafana.asc /etc/apt/sources.list.d/keel-grafana.list
        /etc/nginx/sites-available/keel-outline /etc/nginx/sites-enabled/keel-outline
        /etc/systemd/system/keel-outline.service
        /etc/systemd/system/keel-cert-renew.service /etc/systemd/system/keel-cert-renew.timer
    )
    for path in "${paths[@]}"; do
        [[ ! -e $path && ! -L $path ]] || fail "an existing path conflicts with a fresh managed install: $path"
    done
    local units=(
        keel-outline.service keel-cert-renew.service keel-cert-renew.timer
        nginx.service grafana-server.service prometheus.service prometheus-node-exporter.service
        outline-ss-server.service ssmanager.service shadowsocks-libev.service certbot.service certbot.timer
    )
    for unit in "${units[@]}"; do
        for directory in /etc/systemd/system /run/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
            path=$directory/$unit
            [[ ! -e $path && ! -L $path ]] || fail "an existing service name conflicts with this install: $unit"
        done
    done
    "${HOSTCTL[@]}" check-ports "$ss_port"
}

check_path_shapes() {
    local path
    local directories=(
        /etc/keel-outline /var/lib/keel-sub /var/lib/keel-dashboards
        /opt/keel-outline /opt/keel-certbot /var/www/keel-acme /etc/letsencrypt
        /etc/nginx /etc/nginx/sites-available /etc/nginx/sites-enabled
        /etc/grafana /etc/grafana/provisioning /etc/prometheus
        /var/lib/grafana /var/lib/prometheus /etc/apt/keyrings /etc/apt/sources.list.d
        /etc/systemd/system/grafana-server.service.d
    )
    for path in "${directories[@]}"; do
        [[ ! -L $path ]] || fail "a managed directory cannot be a symlink: $path"
        if [[ -e $path && ! -d $path ]]; then
            fail "a managed directory path is not a directory: $path"
        fi
    done
    local files=(
        /etc/keel-outline/state.json /etc/keel-outline/inventory.json
        /etc/keel-outline/access.json /etc/keel-outline/server.yaml
        /etc/keel-outline/delivery.json /etc/keel-outline/grafana.env
        /etc/keel-outline/grafana-secret.key
        /etc/keel-outline/packages-configured
        /var/lib/keel-sub/profile.yaml /usr/local/bin/outline-ss-server
        /var/log/keel-outline-install.log /etc/default/prometheus
        /etc/default/prometheus-node-exporter /etc/prometheus/prometheus.yml
        /etc/apt/keyrings/keel-grafana.asc /etc/apt/sources.list.d/keel-grafana.list
        /etc/nginx/sites-available/keel-outline
        /etc/systemd/system/keel-outline.service
        /etc/systemd/system/keel-cert-renew.service /etc/systemd/system/keel-cert-renew.timer
    )
    for path in "${files[@]}"; do
        [[ ! -L $path ]] || fail "a managed file cannot be a symlink: $path"
        if [[ -e $path && ! -f $path ]]; then
            fail "a managed file path is not a regular file: $path"
        fi
    done
}

require_sources() {
    local path
    local paths=(
        "$OUTLINE_DIR/enroll.py" "$OUTLINE_DIR/generate.py"
        "$OUTLINE_DIR/prometheus.example.yml" "$OUTLINE_DIR/grafana-datasource.yml"
        "$OUTLINE_DIR/grafana-dashboard.json" "$OUTLINE_DIR/tcpinfo/build.sha256"
        "$HOST_DIR/outline.service"
        "$HOST_DIR/grafana.conf" "$HOST_DIR/grafana-dashboards.yaml"
        "$HOST_DIR/prometheus.default" "$HOST_DIR/prometheus-node-exporter.default"
        "$HOST_DIR/cert-renew.service" "$HOST_DIR/cert-renew.timer"
    )
    for path in "${paths[@]}"; do
        [[ -f $path && ! -L $path ]] || fail "a required local host artifact is missing: $path"
    done
}

make_workdir() {
    local prefix=$1
    mktemp -d "/var/tmp/${prefix}.XXXXXXXX"
}
