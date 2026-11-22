#!/bin/bash
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
    printf 'Usage: sudo bash %s DEVICE_ID LABEL\n' "$0" >&2
    exit 2
fi
DEVICE_ID=$1
LABEL=$2

# shellcheck source=common.sh
source "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/common.sh"
require_platform
"${HOSTCTL[@]}" validate-client "$DEVICE_ID" "$LABEL"
require_sources
acquire_lock
detect_managed
check_path_shapes
(( MANAGED == 1 )) || fail "no managed install exists; run install.sh first."
"${HOSTCTL[@]}" verify-managed
systemctl is-active --quiet keel-outline.service && systemctl is-active --quiet nginx.service \
    || fail "Outline and nginx must be active before adding a client."
endpoint=$("${HOSTCTL[@]}" endpoint)
IFS=$'\t' read -r PUBLIC_IPV4 SS_PORT <<<"$endpoint"
[[ -n $PUBLIC_IPV4 && -n $SS_PORT ]] || fail "managed endpoint state is invalid."
ORIGINAL_KEY=$("${HOSTCTL[@]}" first-key)

WORK=$(make_workdir keel-outline-client)
PUBLISHED=0
cleanup() {
    local rc=$?
    trap - EXIT
    if (( rc != 0 && PUBLISHED == 1 )); then
        if "${HOSTCTL[@]}" rollback "$WORK" && systemctl restart keel-outline.service && "${HOSTCTL[@]}" wait-key "$ORIGINAL_KEY"; then
            printf 'Server and profile restored; pending inventory retained for a stable retry.\n' >&2
        else
            printf 'Restore incomplete. Private recovery files retained at %s\n' "$WORK" >&2
            exit "$rc"
        fi
    fi
    if [[ ${WORK:-} == /var/tmp/keel-outline-client.* && -d $WORK ]]; then
        rm -rf -- "$WORK"
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 "$OUTLINE_DIR/enroll.py" \
    --inventory /etc/keel-outline/inventory.json \
    --host "$PUBLIC_IPV4" --port "$SS_PORT" \
    --device-id "$DEVICE_ID" --label "$LABEL" >/dev/null
python3 "$OUTLINE_DIR/generate.py" \
    --inventory /etc/keel-outline/inventory.json \
    --output "$WORK/generated" >/dev/null
if cmp -s /etc/keel-outline/server.yaml "$WORK/generated/outline.yml" \
    && cmp -s /var/lib/keel-sub/profile.yaml "$WORK/generated/clash.yaml"; then
    "${HOSTCTL[@]}" wait-key "$DEVICE_ID"
    printf 'Client already present and active; no files or credentials changed.\n'
    exit 0
fi

"${HOSTCTL[@]}" snapshot "$WORK"
PUBLISHED=1
"${HOSTCTL[@]}" publish-server "$WORK/generated/outline.yml"
systemctl try-restart keel-outline.service
"${HOSTCTL[@]}" wait-key "$DEVICE_ID"
"${HOSTCTL[@]}" publish-profile "$WORK/generated/clash.yaml"
systemctl is-active --quiet keel-outline.service
systemctl is-active --quiet nginx.service
PUBLISHED=0
printf 'Client active. The shared subscription URL is unchanged; delivery remains in /etc/keel-outline/delivery.json.\n'
