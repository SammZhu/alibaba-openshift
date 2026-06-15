#!/usr/bin/env bash
# abi-eni-spike.sh — answer the ONE open question that gates ABI 06a/06b
# (plan §4/§7): on Alibaba Cloud, can a pre-created ENI carry a known MAC
# BEFORE the instance exists, and can it be attached as the instance's PRIMARY
# NIC?  This decides the static-NMState MAC-harvest mechanism:
#
#   Option C : pre-create the PRIMARY ENI, harvest MAC, attach at RunInstances.
#   Option B1: create instance from a placeholder image, harvest the auto
#              primary-ENI MAC, then ReplaceSystemDisk to the agent image.
#   Option B2: pre-create a SECONDARY ENI (MAC known immediately), attach as
#              secondary, drive the node network off it via NMState.
#
# Near-zero cost: uses `RunInstances --DryRun true` (no instance is created) and
# one throwaway ENI that is deleted at the end.  Run on the operator host
# (RHEL8) with the aliyun CLI configured — same convention as the other scripts.
#
# Usage:
#   scripts/abi-eni-spike.sh
# Requires (env or args; falls back to scripts/.state and config.sh):
#   SPIKE_VSWITCH_ID   private VSwitch id (e.g. the masters' PrivateVSwitch2Id)
#   SPIKE_SG_ID        a security group id in the same VPC
#   SPIKE_IMAGE_ID     any bootable ECS image id in the region (for DryRun)
#   SPIKE_INSTANCE_TYPE  (default: ecs.g7.xlarge)
#   SPIKE_TEST_IP      a free IP in the VSwitch subnet (default: <subnet>.250)

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
# shellcheck source=lib/common.sh
source "$LIB_DIR/common.sh"   # helpers only (log/ok/die/need/state_load)

# Soft config: this spike only needs REGION + a working aliyun profile, so do
# NOT hard-require the full scripts/config.sh (ansible users drive everything
# from group_vars and have no config.sh).  Use it if present, else env/defaults.
[ -f "$CONFIG_FILE" ] && { . "$CONFIG_FILE"; ok "loaded $CONFIG_FILE"; } || \
  warn "no scripts/config.sh — using env/defaults (REGION, optional ZONE2/ALIBABA_CLOUD_PROFILE)"
REGION="${REGION:-cn-wulanchabu}"
[ -n "${ALIBABA_CLOUD_PROFILE:-}" ] && export ALIBABA_CLOUD_PROFILE
state_load 2>/dev/null || true

need aliyun
need jq

INSTANCE_TYPE="${SPIKE_INSTANCE_TYPE:-ecs.g7.xlarge}"

# Resolve network resources: env > .state (cluster/mirror stack outputs).
VSWITCH_ID="${SPIKE_VSWITCH_ID:-${PRIVATE_VSWITCH_2_ID:-${PRIVATE_VSWITCH_ID:-}}}"
SG_ID="${SPIKE_SG_ID:-${CONTROL_PLANE_SG_ID:-${JUMP_HOST_SG_ID:-}}}"
IMAGE_ID="${SPIKE_IMAGE_ID:-${ECS_IMAGE_ID:-}}"
VSW_CIDR=""

# ── Auto-discover anything not supplied (so the spike is a one-liner) ─────────
if [ -z "$VSWITCH_ID" ]; then
  log "No SPIKE_VSWITCH_ID — discovering a VSwitch in $REGION${ZONE2:+ (prefer zone $ZONE2)} ..."
  vsw_json="$(aliyun vpc DescribeVSwitches --RegionId "$REGION" --PageSize 50)"
  # prefer one in ZONE2 (where masters live); else take the first available.
  read -r VSWITCH_ID VSW_CIDR _vpc < <(echo "$vsw_json" | jq -r --arg z "${ZONE2:-}" '
    (.VSwitches.VSwitch | map(select($z=="" or .ZoneId==$z)) | .[0])
    // (.VSwitches.VSwitch[0])
    | "\(.VSwitchId) \(.CidrBlock) \(.VpcId)"')
  [ -n "$VSWITCH_ID" ] && [ "$VSWITCH_ID" != null ] \
    || die "No VSwitch found in $REGION. Create one (or a mirror-stack) first, or set SPIKE_VSWITCH_ID."
  SG_VPC="$_vpc"
  ok "VSwitch: $VSWITCH_ID ($VSW_CIDR, vpc $SG_VPC)"
else
  read -r VSW_CIDR SG_VPC < <(aliyun vpc DescribeVSwitches --RegionId "$REGION" --VSwitchId "$VSWITCH_ID" \
    | jq -r '.VSwitches.VSwitch[0] | "\(.CidrBlock) \(.VpcId)"')
fi

if [ -z "$SG_ID" ]; then
  log "No SPIKE_SG_ID — discovering a security group in vpc ${SG_VPC:-<any>} ..."
  SG_ID="$(aliyun ecs DescribeSecurityGroups --RegionId "$REGION" ${SG_VPC:+--VpcId "$SG_VPC"} --PageSize 50 \
    | jq -r '.SecurityGroups.SecurityGroup[0].SecurityGroupId // empty')"
  [ -n "$SG_ID" ] || die "No security group in vpc $SG_VPC. Set SPIKE_SG_ID."
  ok "SecurityGroup: $SG_ID"
fi

if [ -z "$IMAGE_ID" ]; then
  log "No SPIKE_IMAGE_ID — discovering a public Linux x86_64 image ..."
  IMAGE_ID="$(aliyun ecs DescribeImages --RegionId "$REGION" --ImageOwnerAlias system \
    --OSType linux --Architecture x86_64 --PageSize 10 \
    | jq -r '.Images.Image[0].ImageId // empty')"
  [ -n "$IMAGE_ID" ] || die "No system image found. Set SPIKE_IMAGE_ID."
  ok "Image: $IMAGE_ID"
fi

# Test IP: a high host inside the CHOSEN VSwitch CIDR (avoids the
# subnet-mismatch footgun); fall back to PRIVATE_SUBNET_CIDR_2 / 10.0.32.0/20.
if [ -z "${SPIKE_TEST_IP:-}" ]; then
  base="$(echo "${VSW_CIDR:-${PRIVATE_SUBNET_CIDR_2:-10.0.32.0/20}}" | cut -d/ -f1 | cut -d. -f1-3)"
  SPIKE_TEST_IP="${base}.250"
fi
TEST_IP="$SPIKE_TEST_IP"

log "Region=$REGION VSwitch=$VSWITCH_ID SG=$SG_ID Image=$IMAGE_ID TestIP=$TEST_IP"

# ── 1. Pre-create a standalone ENI and check the MAC is known immediately ─────
log "Creating a throwaway ENI with fixed primary IP $TEST_IP ..."
ENI_ID="$(aliyun ecs CreateNetworkInterface \
  --RegionId "$REGION" \
  --VSwitchId "$VSWITCH_ID" \
  --SecurityGroupId "$SG_ID" \
  --PrimaryIpAddress "$TEST_IP" \
  --NetworkInterfaceName abi-eni-spike \
  | jq -r '.NetworkInterfaceId')"
[ -n "$ENI_ID" ] && [ "$ENI_ID" != null ] || die "CreateNetworkInterface failed."
ok "ENI created: $ENI_ID"

cleanup() {
  warn "Cleaning up ENI $ENI_ID ..."
  aliyun ecs DeleteNetworkInterface --RegionId "$REGION" --NetworkInterfaceId "$ENI_ID" >/dev/null 2>&1 \
    && ok "ENI deleted." || warn "ENI delete failed — delete $ENI_ID by hand."
}
trap cleanup EXIT

# MAC may take a moment to populate; poll briefly.
MAC=""
for _ in 1 2 3 4 5 6; do
  MAC="$(aliyun ecs DescribeNetworkInterfaces --RegionId "$REGION" \
          --NetworkInterfaceId.1 "$ENI_ID" \
          | jq -r '.NetworkInterfaceSets.NetworkInterfaceSet[0].MacAddress // empty')"
  [ -n "$MAC" ] && break
  sleep 3
done

if [ -n "$MAC" ]; then
  ok "B2 viable: secondary ENI MAC is known pre-instance -> $MAC"
  B2_MAC=yes
else
  warn "ENI MAC not reported — B2 (pre-known MAC) NOT viable on this account/region."
  B2_MAC=no
fi

# ── helper: run a DryRun RunInstances and classify the result ────────────────
# Alibaba returns Code="DryRunOperation" when the (otherwise valid) request
# passes the dry run; any other Code means the request shape was rejected.
dryrun_verdict() {
  local label="$1"; shift
  local out code
  out="$(aliyun ecs RunInstances --RegionId "$REGION" --DryRun true \
          --ImageId "$IMAGE_ID" --InstanceType "$INSTANCE_TYPE" \
          --Amount 1 "$@" 2>&1 || true)"
  code="$(echo "$out" | jq -r '.Code // empty' 2>/dev/null || true)"
  if [ "$code" = "DryRunOperation" ] || echo "$out" | grep -qi 'DryRunOperation'; then
    ok "$label: ACCEPTED by DryRun (supported)"
    return 0
  fi
  warn "$label: REJECTED -> ${code:-$(echo "$out" | head -c 300)}"
  return 1
}

# ── 2. Option C: attach the pre-created ENI as the PRIMARY NIC ────────────────
log "Testing Option C (pre-created ENI as PRIMARY NIC) ..."
if dryrun_verdict "Option C (primary attach)" \
      --NetworkInterface.1.NetworkInterfaceId "$ENI_ID"; then
  OPT_C=yes
else
  OPT_C=no
fi

# ── 3. Option B2: attach the pre-created ENI as a SECONDARY NIC ───────────────
log "Testing Option B2 (pre-created ENI as SECONDARY NIC) ..."
if dryrun_verdict "Option B2 (secondary attach)" \
      --VSwitchId "$VSWITCH_ID" --SecurityGroupId "$SG_ID" \
      --NetworkInterface.1.NetworkInterfaceId "$ENI_ID" \
      --NetworkInterface.1.InstanceType Secondary; then
  OPT_B2=yes
else
  OPT_B2=no
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
echo
echo "================ ABI ENI spike verdict ================"
echo "  secondary-ENI MAC known pre-instance : $B2_MAC"
echo "  Option C  (pre-created PRIMARY ENI)   : $OPT_C"
echo "  Option B2 (pre-created SECONDARY ENI) : $OPT_B2"
echo "-------------------------------------------------------"
if [ "$OPT_C" = yes ]; then
  echo "  => Use Option C: 06a pre-creates ENIs (fixed IP, harvest MAC);"
  echo "     06b attaches them as the primary NIC at RunInstances."
elif [ "$OPT_B2" = yes ] && [ "$B2_MAC" = yes ]; then
  echo "  => Use Option B2: 06a pre-creates SECONDARY ENIs (MAC known now);"
  echo "     06b attaches as secondary; NMState drives the node off it."
  echo "     (Routing note: pin default route to the secondary NIC; ignore"
  echo "      / down the auto primary ENI.)"
else
  echo "  => Fall back to Option B1 (reimage): 06a boots instances from a"
  echo "     placeholder image (fixed primary IP), harvest auto primary-ENI"
  echo "     MAC, build the agent ISO, then ReplaceSystemDisk to the agent"
  echo "     image and start."
fi
echo "======================================================="
