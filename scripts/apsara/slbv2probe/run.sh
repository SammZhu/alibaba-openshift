#!/bin/bash
# Runs the NLB probe from the operator host.  Endpoint must be given: nothing in
# the deployment has needed NLB before, so all.yml does not carry it.
#
#   SLB_ENDPOINT=slb-vpc.cloud.dyz7.com ./run.sh > /home/claude/slbv2-probe.txt 2>&1
#
# Find the endpoint first with scripts/apsara/probe-endpoints.sh, which now
# covers nlb/alb.
set -euo pipefail
cd "$(dirname "$0")"

AV=${AV:-../../../ansible/group_vars/all.yml}
[ -r "$AV" ] || { echo "cannot read $AV (set AV=/path/to/all.yml)"; exit 2; }

get() {
  python3 - "$AV" "$1" <<'PY'
import re, sys
key = sys.argv[2]
for line in open(sys.argv[1], encoding='utf-8'):
    m = re.match(r'\s*%s:\s*(.*?)\s*$' % re.escape(key), line)
    if m:
        print(m.group(1).strip().strip('"').strip("'"))
        break
PY
}

export REGION=${REGION:-$(get region)}
export SLB_ENDPOINT=${SLB_ENDPOINT:-$(get ENDPOINT_SLB)}
export AK=${AK:-$(get capa_ak)}
export SK=${SK:-$(get capa_sk)}

: "${SLB_ENDPOINT:?set SLB_ENDPOINT (find it with scripts/apsara/probe-endpoints.sh)}"
: "${REGION:?could not read region from $AV}"
: "${AK:?could not read capa_ak from $AV}"

echo "region=$REGION  slb=$SLB_ENDPOINT  ak=${AK:0:6}…"
# Versions are pinned in go.mod to exactly what the CCM v2.14.0 binary embeds
# (read with `go version -m`).  Probing a different version answers a different
# question — that mistake cost a round on the SLB side.
export GOFLAGS=${GOFLAGS:--mod=mod}
export GOPROXY=${GOPROXY:-https://goproxy.cn,direct}
go mod tidy >/dev/null
echo "sdk:"; go list -m github.com/alibabacloud-go/slb-20140515/v4 github.com/alibabacloud-go/darabonba-openapi/v2 | sed 's/^/  /'
echo
go build -o slbv2probe . && ./slbv2probe
