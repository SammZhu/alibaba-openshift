#!/usr/bin/env bash
#
# 99-teardown.sh — Phase G
#   Clean up everything created by 01-04 in reverse order.
#
# IMPORTANT: Run app-layer cleanup FIRST (PVCs, LoadBalancer Services) on the
# jump host BEFORE this script — otherwise CCM/CSI-created SLBs and disks
# become orphans and keep billing.
#
# Usage: ./99-teardown.sh [--force]

set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
preflight

FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

confirm() {
  $FORCE && return 0
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[yY]$ ]]
}

cat <<EOF

Teardown will:
  1. Delete Assisted Installer cluster (releases the Red Hat record only)
  2. Delete ROS stack (releases all Aliyun resources tagged owned)
  3. Verify no orphan SLB / disks / EIPs remain (read-only)
  4. Clear .state

Make sure you have already run on the cluster:
  oc delete svc -A --field-selector spec.type=LoadBalancer
  oc delete pvc -A --all
  (wait ~3 min for CCM/CSI to actually delete the Aliyun resources)

EOF
confirm "Continue?" || die "Aborted"

state_load

# ── 1. Delete Assisted cluster ───────────────────────────────────────────────
if [ -n "${CLUSTER_ID:-}" ]; then
  log "Deleting Assisted cluster $CLUSTER_ID..."
  ai_curl DELETE "/clusters/$CLUSTER_ID" >/dev/null 2>&1 || warn "Assisted DELETE returned non-zero (maybe already gone)"
  ok "Assisted cluster deleted"
fi

# ── 2. Delete ROS stack ──────────────────────────────────────────────────────
if [ -n "${ROS_STACK_ID:-}" ]; then
  log "Deleting ROS stack $ROS_STACK_ID..."
  aliyun ros DeleteStack --StackId "$ROS_STACK_ID" --RegionId "$REGION" >/dev/null

  log "Waiting for stack delete..."
  while true; do
    OUT="$(aliyun ros GetStack --StackId "$ROS_STACK_ID" --RegionId "$REGION" 2>&1 || true)"
    if echo "$OUT" | grep -q "StackNotFound\|does not exist\|EntityNotExists"; then
      echo
      ok "Stack deleted"
      break
    fi
    STATUS="$(echo "$OUT" | jq -r '.Status // empty' 2>/dev/null || echo "")"
    printf '\r  %s ' "${STATUS:-MISSING}"
    case "$STATUS" in
      DELETE_FAILED) echo; die "Stack delete failed — check ROS console for blocking resources";;
    esac
    sleep 20
  done
fi

# ── 3. Orphan check ──────────────────────────────────────────────────────────
log "Scanning for orphan resources tagged with this cluster..."
TAG_KEY="kubernetes.io/cluster/${CLUSTER_NAME}"

count() { local n; n="$(echo "$1" | jq 'length' 2>/dev/null || echo 0)"; echo "$n"; }

ECS="$(aliyun ecs DescribeInstances --RegionId "$REGION" \
  --Tag.1.Key "$TAG_KEY" --Tag.1.Value owned \
  --query 'Instances.Instance[].[InstanceName,Status]' 2>/dev/null || echo '[]')"
DISKS="$(aliyun ecs DescribeDisks --RegionId "$REGION" \
  --Tag.1.Key "$TAG_KEY" --Tag.1.Value owned \
  --query 'Disks.Disk[].[DiskName,Status]' 2>/dev/null || echo '[]')"
SLBS="$(aliyun slb DescribeLoadBalancers --RegionId "$REGION" \
  --Tag.1.TagKey "$TAG_KEY" --Tag.1.TagValue owned \
  --query 'LoadBalancers.LoadBalancer[].[LoadBalancerName,LoadBalancerStatus]' 2>/dev/null || echo '[]')"
EIPS="$(aliyun vpc DescribeEipAddresses --RegionId "$REGION" \
  --Tag.1.Key "$TAG_KEY" --Tag.1.Value owned \
  --query 'EipAddresses.EipAddress[].[Name,Status]' 2>/dev/null || echo '[]')"

ORPHANS=0
for label in ECS DISKS SLBS EIPS; do
  n="$(count "${!label}")"
  if [ "$n" -gt 0 ]; then
    warn "$n orphan $label resources still exist:"
    echo "${!label}" | jq -r '.[] | "  - " + (.|tostring)'
    ORPHANS=$((ORPHANS+n))
  else
    ok "$label: 0 orphans"
  fi
done

if [ "$ORPHANS" -gt 0 ]; then
  warn "Total $ORPHANS orphan resources — they continue billing until manually deleted."
  warn "Most likely cause: PVCs / LoadBalancer Services were not deleted before teardown."
fi

# ── 4. Clear state ───────────────────────────────────────────────────────────
log "Clearing $STATE_FILE..."
rm -f "$STATE_FILE"

ok "Teardown complete. Check the Aliyun billing dashboard in 1h to verify."
