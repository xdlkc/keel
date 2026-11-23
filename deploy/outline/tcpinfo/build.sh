#!/usr/bin/env bash
# Build the Linux server on the operator's machine, including macOS.
set -euo pipefail
if (( $# != 1 )); then echo "Usage: bash build.sh OUTPUT_BINARY" >&2; exit 2; fi
destination=$(cd -- "$(dirname -- "$1")" && pwd -P)/$(basename -- "$1")
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_dir=$(mktemp -d)
trap 'rm -rf -- "$source_dir"' EXIT
git clone --quiet --depth 1 --branch v1.9.2 https://github.com/OutlineFoundation/tunnel-server.git "$source_dir"
cd "$source_dir"
test "$(git rev-parse HEAD)" = be0f6d744f43514c476f91d40ac0e0fdf5f39753
git apply "$here/hooks.patch"
cp -R "$here/overlay/." .
export GOTOOLCHAIN=go1.27.4 GOOS=linux GOARCH=amd64 GOAMD64=v1 CGO_ENABLED=0 GOWORK=off GOFLAGS=
go build -mod=readonly -trimpath -buildvcs=false -ldflags="-s -w -buildid= -X main.version=1.9.2-keel-tcp1" -o "$source_dir/outline-ss-server" ./cmd/outline-ss-server
python3 - "$source_dir/outline-ss-server" "$here/build.sha256" <<'PY'
import hashlib, sys
from pathlib import Path
if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != Path(sys.argv[2]).read_text().strip():
    raise SystemExit("Built server checksum differs from the reviewed Linux build.")
PY
install -m 0755 "$source_dir/outline-ss-server" "$destination"
