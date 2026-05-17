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
preflight_with_assisted

FORCE=false
SKIP_APP_CLEANUP=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    --skip-app-cleanup) SKIP_APP_CLEANUP=true ;;
    *) die "Unknown arg: $arg" ;;
  esac
done

confirm() {
  $FORCE && return 0
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[yY]$ ]]
}

cat <<EOF

Teardown will:
  0. (auto, via jump host SSH) delete LoadBalancer Services + PVCs to release
     CCM-managed SLBs and CSI-managed disks. Skip with --skip-app-cleanup.
  1. Delete Assisted Installer cluster (Red Hat side only — frees no Aliyun \$)
  2. Delete ROS stack (frees VPC, ECS, SLBs, NAT, EIP, PrivateZone, jump host)
  3. Verify no orphan resources remain (read-only scan by cluster tag)
  4. Clear scripts/.state

EOF
confirm "Continue?" || die "Aborted"

state_load

# ── 0. App-layer cleanup via jump host ───────────────────────────────────────
if ! $SKIP_APP_CLEANUP && [ -n "${JUMP_HOST_IP:-}" ]; then
  log "Cleaning up application-layer Aliyun resources via jump host..."
  if ssh -i "$SSH_PRIVATE_KEY_FILE" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
       root@"$JUMP_HOST_IP" 'test -f /root/kubeconfig' 2>/dev/null; then
    ssh -i "$SSH_PRIVATE_KEY_FILE" -o StrictHostKeyChecking=no \
        root@"$JUMP_HOST_IP" 'bash -s' <<'CLEANUP'
set -e
export KUBECONFIG=/root/kubeconfig
echo "Deleting LoadBalancer Services..."
oc get svc -A --field-selector spec.type=LoadBalancer -o json \
  | jq -r '.items[] | "\(.metadata.namespace) \(.metadata.name)"' \
  | while read -r ns name; do
      [ -n "$name" ] && oc delete svc -n "$ns" "$name" --wait=false || true
    done
echo "Deleting all PVCs..."
oc delete pvc --all -A --wait=false 2>/dev/null || true
echo "Waiting 3 min for CCM/CSI to actually release the Aliyun resources..."
sleep 180
CLEANUP
    ok "App-layer cleanup done"
  else
    warn "Could not reach jump host or kubeconfig missing; skipping app cleanup."
    warn "If LoadBalancer Services or PVCs still exist, the corresponding"
    warn "SLBs/disks will survive the ROS stack delete as orphans."
    confirm "Continue anyway?" || die "Aborted"
  fi
elif $SKIP_APP_CLEANUP; then
  warn "App-layer cleanup skipped per --skip-app-cleanup"
else
  warn "JUMP_HOST_IP not set — cannot auto-cleanup app layer."
  warn "If you have other access to the cluster, run there first:"
  warn "  oc delete svc -A --field-selector spec.type=LoadBalancer"
  warn "  oc delete pvc -A --all"
  confirm "Continue without app cleanup?" || die "Aborted"
fi

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

scan() {
  local svc="$1" action="$2" tagflag="${3:---Tag.1.Key}" tagvalflag="${4:---Tag.1.Value}"
  aliyun "$svc" "$action" --RegionId "$REGION" \
    "$tagflag" "$TAG_KEY" "$tagvalflag" owned 2>/dev/null || echo '{}'
}

ECS="$(scan ecs DescribeInstances)"
DISKS="$(scan ecs DescribeDisks)"
SECGRPS="$(scan ecs DescribeSecurityGroups)"
SLBS="$(scan slb DescribeLoadBalancers --Tag.1.TagKey --Tag.1.TagValue)"
EIPS="$(scan vpc DescribeEipAddresses)"
NATS="$(scan vpc DescribeNatGateways)"
VSWITCHES="$(scan vpc DescribeVSwitches)"
VPCS="$(scan vpc DescribeVpcs)"
ZONES="$(scan pvtz DescribeZones)"

count_json() { echo "$1" | jq -r '[(.. | objects | select(has("InstanceId") or has("DiskId") or has("LoadBalancerId") or has("AllocationId") or has("SecurityGroupId") or has("NatGatewayId") or has("VSwitchId") or has("VpcId") or has("ZoneId"))) ] | length' 2>/dev/null || echo 0; }

ORPHANS=0
for entry in "ECS:$ECS" "DISKS:$DISKS" "SECGRPS:$SECGRPS" "SLBS:$SLBS" "EIPS:$EIPS" "NATS:$NATS" "VSWITCHES:$VSWITCHES" "VPCS:$VPCS" "ZONES:$ZONES"; do
  label="${entry%%:*}"
  body="${entry#*:}"
  n="$(count_json "$body")"
  if [ "$n" -gt 0 ]; then
    warn "$n orphan $label resource(s) still exist"
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
