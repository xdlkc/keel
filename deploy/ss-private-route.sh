#!/bin/bash
set -euo pipefail
ipt=/usr/sbin/iptables
if [[ $# != 2 || ! $2 =~ ^[1-9][0-9]{0,4}$ || $2 -gt 65535 ]]; then
  echo "Usage: $0 MANAGER_USER VIEWER_PORT" >&2
  exit 2
fi
uid=$(id -u -- "$1")
[[ $uid != 0 ]] || { echo "Use a dedicated unprivileged manager account." >&2; exit 2; }
view_port=$2
/usr/sbin/ip route replace local 192.0.2.80/32 dev lo src 127.0.0.1 table local
ensure() {
  local table=$1 chain=$2
  shift 2
  "$ipt" -w -t "$table" -C "$chain" "$@" 2>/dev/null || "$ipt" -w -t "$table" -I "$chain" 1 "$@"
}
# Only the Rust proxy's connections to this reserved address reach the read-only dashboard.
ensure nat OUTPUT -m owner --uid-owner "$uid" -d 192.0.2.80/32 -p tcp --dport 80 -j DNAT --to-destination "127.0.0.1:$view_port"
if ! "$ipt" -w -S KEEL_SS_OUT >/dev/null 2>&1; then
  "$ipt" -w -N KEEL_SS_OUT
fi
for range in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
  ensure filter KEEL_SS_OUT -d "$range" -j REJECT
done
for proto in tcp udp; do
  ensure filter KEEL_SS_OUT -d 169.254.169.254/32 -p "$proto" --dport 53 -j RETURN
  ensure filter KEEL_SS_OUT -d 127.0.0.53/32 -p "$proto" --dport 53 -j RETURN
done
ensure filter KEEL_SS_OUT -d 127.0.0.1/32 -p tcp --dport "$view_port" -m conntrack --ctorigdst 192.0.2.80 --ctorigdstport 80 -j RETURN
ensure filter OUTPUT -m owner --uid-owner "$uid" -m conntrack --ctdir ORIGINAL -j KEEL_SS_OUT
