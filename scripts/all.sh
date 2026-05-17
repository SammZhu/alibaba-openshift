#!/usr/bin/env bash
#
# all.sh — Run phases 01-04 in sequence. Phase 05 must run on the jump host.
#
# Usage:
#   ./all.sh                # run 01-04 sequentially, stop on error
#   ./all.sh --from 03      # resume from script with prefix 03..
#   ./all.sh --only 02      # run a single script
#   ./all.sh --to 02        # run 01..02 (useful for halting before stack creation)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ALL=(01-prepare-iso.sh 02-import-image.sh 03-create-stack.sh 04-install-cluster.sh)
FROM=""
ONLY=""
TO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --to)   TO="$2";   shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

for script in "${ALL[@]}"; do
  # Numeric prefix (e.g. "03" from "03-create-stack.sh")
  prefix="${script%%-*}"

  # --only: run exactly that script
  if [ -n "$ONLY" ] && [ "$prefix" != "$ONLY" ]; then continue; fi

  # --from: skip scripts with prefix numerically less than FROM
  if [ -n "$FROM" ] && [ "$((10#$prefix))" -lt "$((10#$FROM))" ]; then continue; fi

  # --to: skip scripts with prefix numerically greater than TO
  if [ -n "$TO" ] && [ "$((10#$prefix))" -gt "$((10#$TO))" ]; then continue; fi

  echo
  echo "════════════════════════════════════════════════════════════════════"
  echo "▶ $script"
  echo "════════════════════════════════════════════════════════════════════"
  "${SCRIPT_DIR}/${script}"
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo "✓ Local pipeline done. To finish, ssh to the jump host:"
echo "    ssh root@\$(awk -F= '/^JUMP_HOST_IP=/{print \$2}' ${SCRIPT_DIR}/.state)"
echo "    cd /root/openshift-alibaba/alibaba-openshift"
echo "    ./scripts/05-deploy-post-install.sh"
echo "════════════════════════════════════════════════════════════════════"
