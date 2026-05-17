#!/usr/bin/env bash
#
# all.sh — Run phases 01-04 in sequence. Phase 05 must run on the jump host.
#
# Usage:
#   ./all.sh                # run 01-04 sequentially, stop on error
#   ./all.sh --from 03      # resume from script 03 (state from previous runs)
#   ./all.sh --only 02      # run a single script

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ALL=(01-prepare-iso.sh 02-import-image.sh 03-create-stack.sh 04-install-cluster.sh)
FROM=""
ONLY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 2 ;;
  esac
done

for script in "${ALL[@]}"; do
  if [ -n "$ONLY" ] && [[ "$script" != ${ONLY}* ]]; then continue; fi
  if [ -n "$FROM" ] && [[ "$script" < ${FROM}* ]]; then continue; fi
  echo
  echo "════════════════════════════════════════════════════════════════════"
  echo "▶ $script"
  echo "════════════════════════════════════════════════════════════════════"
  "${SCRIPT_DIR}/${script}"
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo "✓ Local pipeline done (01-04). To finish:"
echo "    ssh root@\$(awk -F= '/^JUMP_HOST_IP=/{print \$2}' ${SCRIPT_DIR}/.state)"
echo "    cd /root/openshift-alibaba/alibaba-openshift"
echo "    ./scripts/05-deploy-post-install.sh"
echo "════════════════════════════════════════════════════════════════════"
