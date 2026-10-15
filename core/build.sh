#!/bin/bash
# Build from the exact upstream commit; do not modify the supplied checkout.
set -Eeuo pipefail
umask 077
upstream=${1:?pinned upstream checkout required}
output=${2:?absolute binary output required}
architecture=${3:-amd64}
[[ "$output" == /* && "$architecture" =~ ^(amd64|arm64)$ ]]
commit=d2758a023cd7f4174a5a5fa4ff66e487d4342ba0
[[ "$(git -C "$upstream" rev-parse HEAD)" == "$commit" ]]
[[ "$(go env GOVERSION)" == go1.26.9 ]]
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
build_root=$(mktemp -d -t kenxu-flow-build.XXXXXXXX)
# Kept on error for diagnostics; contains public upstream source only.
# The reviewed artifact was built from Git's CRLF export. Pin that input too;
# Go build IDs include source bytes even where CRLF does not change semantics.
git -C "$upstream" -c core.autocrlf=true archive "$commit" | tar -xf - -C "$build_root"
cd "$build_root"
sed -i 's/\r$//' app/dispatcher/default.go app/dispatcher/stats.go common/mux/server.go features/stats/stats.go main/run.go proxy/proxy.go proxy/vless/inbound/inbound.go
git apply --check "$source_root/xray-26.3.27.patch"
git apply "$source_root/xray-26.3.27.patch"
mkdir app/kenxuflow
cp "$source_root"/kenxuflow/*.go app/kenxuflow/
export GOTOOLCHAIN=local GOMAXPROCS=${GOMAXPROCS:-2}
go test -p 2 ./app/kenxuflow ./app/dispatcher ./common/mux
if [[ ${KENXU_RACE_CHECK:-0} == 1 ]];then CGO_ENABLED=1 go test -race -p 2 ./app/kenxuflow ./app/dispatcher ./common/mux;fi
CGO_ENABLED=0 GOOS=linux GOARCH="$architecture" go build -p 2 -trimpath -buildvcs=false -o "$output" ./main
sha256sum "$output"
printf 'Pinned upstream: %s; Go 1.26.9; linux/%s; MPL-2.0; source: %s\n' "$commit" "$architecture" "$build_root"
