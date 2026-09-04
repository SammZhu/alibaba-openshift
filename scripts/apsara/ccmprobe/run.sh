#!/bin/bash
# Runs the CCM control experiment from the operator host, reading what the
# deployment already knows (group_vars/all.yml) instead of asking again.
#
#   ./run.sh > /home/claude/ccm-probe4.txt 2>&1
#
# Credentials are the ones the CAPA controller already uses, so a PASS here is a
# PASS for a configuration the CCM can actually be handed.
set -euo pipefail
cd "$(dirname "$0")"

AV=${AV:-../../../ansible/group_vars/all.yml}
[ -r "$AV" ] || { echo "cannot read $AV (set AV=/path/to/all.yml)"; exit 2; }

# One extractor, in python — the quoting in a sed pipeline for YAML scalars is
# exactly the kind of thing that fails silently and returns an empty string.
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
export ORG_ID=${ORG_ID:-$(get ORG_ID)}
export RG_ID=${RG_ID:-$(get RG_ID)}

# SLB_ENDPOINT is the one value that may genuinely be absent — nothing has
# needed it before now.  dyz7: slb-vpc.cloud.dyz7.com
: "${SLB_ENDPOINT:?set SLB_ENDPOINT explicitly (dyz7: slb-vpc.cloud.dyz7.com)}"
: "${REGION:?could not read region from $AV}"
: "${AK:?could not read capa_ak from $AV}"

echo "region=$REGION  slb=$SLB_ENDPOINT  org=${ORG_ID:0:8}…  ak=${AK:0:6}…"
export GOFLAGS=${GOFLAGS:--mod=mod}
export GOPROXY=${GOPROXY:-https://goproxy.cn,direct}
go mod tidy >/dev/null
go build -o ccmprobe . && ./ccmprobe
