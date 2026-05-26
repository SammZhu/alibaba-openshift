#!/usr/bin/env bash
#
# 03-create-stack.sh — Phase B
#   Create ROS stack, wait CREATE_COMPLETE, capture outputs.
#
# Outputs (saved to .state):
#   ROS_STACK_ID
#   API_SLB_IP
#   VPC_ID
#   PRIVATE_VSWITCH
#   WORKER_SG
#   NODE_RAM_ROLE
#   JUMP_HOST_IP        (if EnableJumpHost=true)
#   DYNAMIC_MANIFEST    (written to OUTPUT_DIR/alibaba-ccm-config.yaml)

set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
preflight
state_require ECS_IMAGE_ID

TEMPLATE_FILE="${REPO_ROOT}/ros-templates/create-cluster-LEGACY.yaml"
[ -f "$TEMPLATE_FILE" ] || die "Template not found: $TEMPLATE_FILE"
SSH_PUB_KEY="$(<"$SSH_PUBLIC_KEY_FILE")"

STACK_NAME="openshift-${CLUSTER_NAME}"

# ── Idempotency: re-use existing stack (by ID in state, or by name lookup) ──
if [ -z "${ROS_STACK_ID:-}" ]; then
  # State lost but stack may still exist — find by name to avoid AlreadyExists error.
  FOUND_ID="$(aliyun ros ListStacks --RegionId "$REGION" \
    --StackName.1 "$STACK_NAME" \
    --query 'Stacks[0].StackId' --output text 2>/dev/null || echo "")"
  if [ -n "$FOUND_ID" ] && [ "$FOUND_ID" != "None" ]; then
    warn "Stack '${STACK_NAME}' already exists ($FOUND_ID), adopting"
    state_set ROS_STACK_ID "$FOUND_ID"
    state_load
  fi
fi

STACK_ID=""
if [ -n "${ROS_STACK_ID:-}" ]; then
  CURRENT_STATUS="$(aliyun ros GetStack --StackId "$ROS_STACK_ID" --query 'Status' --output text 2>/dev/null || echo "MISSING")"
  case "$CURRENT_STATUS" in
    CREATE_COMPLETE) ok "Stack $ROS_STACK_ID already complete; refreshing outputs"; STACK_ID="$ROS_STACK_ID";;
    CREATE_IN_PROGRESS) log "Stack $ROS_STACK_ID still creating; will resume polling"; STACK_ID="$ROS_STACK_ID";;
    CREATE_FAILED|ROLLBACK_FAILED|ROLLBACK_COMPLETE)
      die "Stack in state $CURRENT_STATUS — delete it first:
    aliyun ros DeleteStack --StackId $ROS_STACK_ID --RegionId $REGION
    rm scripts/.state  # then rerun" ;;
    DELETE_IN_PROGRESS|DELETE_FAILED)
      die "Stack is being / failed deleting ($CURRENT_STATUS); wait or clean up before rerun" ;;
    MISSING) warn "Stack $ROS_STACK_ID gone; creating new"; STACK_ID=""; ROS_STACK_ID="";;
    *) warn "Unexpected stack state $CURRENT_STATUS; will create new"; STACK_ID=""; ROS_STACK_ID="";;
  esac
fi

# ── Build Parameters payload ─────────────────────────────────────────────────
build_params() {
  local jq_args=(
    --arg cluster   "$CLUSTER_NAME"
    --arg domain    "$BASE_DOMAIN"
    --arg region    "$REGION"
    --arg zone1     "$ZONE"
    --arg zone2     "$ZONE2"
    --arg vpc       "$VPC_CIDR"
    --arg priv1     "$PRIVATE_SUBNET_CIDR"
    --arg priv2     "$PRIVATE_SUBNET_CIDR_2"
    --arg pub       "$PUBLIC_SUBNET_CIDR"
    --argjson cpcount "$CONTROL_PLANE_COUNT"
    --arg cptype    "$CONTROL_PLANE_INSTANCE_TYPE"
    --argjson cocount "$COMPUTE_COUNT"
    --arg cotype    "$COMPUTE_INSTANCE_TYPE"
    --arg disktype  "$SYSTEM_DISK_CATEGORY"
    --argjson disksize "$SYSTEM_DISK_SIZE"
    --arg method    "$INSTALLATION_METHOD"
    --arg image     "$ECS_IMAGE_ID"
    --arg rip       "$RENDEZVOUS_IP"
    --argjson jump  "${ENABLE_JUMP_HOST:-false}"
    --arg jumpt     "${JUMP_HOST_INSTANCE_TYPE:-ecs.t6-c1m1.large}"
    --arg sshkey    "$SSH_PUB_KEY"
  )
  jq -n "${jq_args[@]}" '[
    {ParameterKey: "ClusterName",                ParameterValue: $cluster},
    {ParameterKey: "BaseDomain",                 ParameterValue: $domain},
    {ParameterKey: "Region",                     ParameterValue: $region},
    {ParameterKey: "ZoneId",                     ParameterValue: $zone1},
    {ParameterKey: "ZoneId2",                    ParameterValue: $zone2},
    {ParameterKey: "VpcCidr",                    ParameterValue: $vpc},
    {ParameterKey: "PrivateSubnetCidr",          ParameterValue: $priv1},
    {ParameterKey: "PrivateSubnetCidr2",         ParameterValue: $priv2},
    {ParameterKey: "PublicSubnetCidr",           ParameterValue: $pub},
    {ParameterKey: "ControlPlaneCount",          ParameterValue: $cpcount|tostring},
    {ParameterKey: "ControlPlaneInstanceType",   ParameterValue: $cptype},
    {ParameterKey: "ComputeCount",               ParameterValue: $cocount|tostring},
    {ParameterKey: "ComputeInstanceType",        ParameterValue: $cotype},
    {ParameterKey: "SystemDiskCategory",         ParameterValue: $disktype},
    {ParameterKey: "SystemDiskSize",             ParameterValue: $disksize|tostring},
    {ParameterKey: "InstallationMethod",         ParameterValue: $method},
    {ParameterKey: "ImageId",                    ParameterValue: $image},
    {ParameterKey: "RendezvousIp",               ParameterValue: $rip},
    {ParameterKey: "EnableJumpHost",             ParameterValue: $jump|tostring},
    {ParameterKey: "JumpHostInstanceType",       ParameterValue: $jumpt},
    {ParameterKey: "SshPublicKey",               ParameterValue: $sshkey}
  ]'
}

if [ -z "${STACK_ID:-}" ]; then
  log "Creating ROS stack..."
  PARAMS_JSON="$(build_params)"
  TEMPLATE_BODY="$(cat "$TEMPLATE_FILE")"

  STACK_ID="$(aliyun ros CreateStack \
    --RegionId "$REGION" \
    --StackName "$STACK_NAME" \
    --DisableRollback true \
    --TimeoutInMinutes 60 \
    --TemplateBody "$TEMPLATE_BODY" \
    --Parameters "$PARAMS_JSON" \
    --query 'StackId' --output text)"
  [ -n "$STACK_ID" ] && [ "$STACK_ID" != "None" ] || die "CreateStack returned no StackId"
  state_set ROS_STACK_ID "$STACK_ID"
fi

# ── Wait for completion ──────────────────────────────────────────────────────
log "Waiting for stack $STACK_ID to reach CREATE_COMPLETE..."
SECONDS_WAITED=0
while true; do
  STATUS="$(aliyun ros GetStack --StackId "$STACK_ID" --query 'Status' --output text 2>/dev/null || echo "ERROR")"
  printf '\r  [%4ds] %s ' "$SECONDS_WAITED" "$STATUS"
  case "$STATUS" in
    CREATE_COMPLETE) echo; ok "Stack ready"; break;;
    CREATE_IN_PROGRESS|UPDATE_IN_PROGRESS) ;;
    *) echo; die "Unexpected stack status: $STATUS — check ROS console for resource-level errors";;
  esac
  sleep 30
  SECONDS_WAITED=$((SECONDS_WAITED + 30))
done

# ── Capture outputs ──────────────────────────────────────────────────────────
log "Reading stack outputs..."
OUTPUTS_JSON="$(aliyun ros GetStack --StackId "$STACK_ID" --query 'Outputs')"

get_output() { echo "$OUTPUTS_JSON" | jq -r ".[] | select(.OutputKey==\"$1\") | .OutputValue"; }

VPC_ID="$(get_output VpcId)"
PRIVATE_VSWITCH="$(get_output VSwitchId)"
API_SLB_IP="$(get_output ApiSLBIp)"
WORKER_SG="$(get_output WorkerSecurityGroup)"
NODE_RAM_ROLE="$(get_output NodeRamRoleName)"
JUMP_HOST_IP="$(get_output JumpHostPublicIp)"
DYNAMIC_MANIFEST="$(get_output DynamicCustomManifest)"
INSTALL_CONFIG="$(get_output InstallConfig)"

[ -n "$API_SLB_IP" ] || die "ApiSLBIp output missing"
state_set VPC_ID         "$VPC_ID"
state_set PRIVATE_VSWITCH "$PRIVATE_VSWITCH"
state_set API_SLB_IP     "$API_SLB_IP"
state_set WORKER_SG      "$WORKER_SG"
state_set NODE_RAM_ROLE  "$NODE_RAM_ROLE"
[ -n "$JUMP_HOST_IP" ] && [ "$JUMP_HOST_IP" != "null" ] && state_set JUMP_HOST_IP "$JUMP_HOST_IP"

# ── Save dynamic manifest and install-config locally ─────────────────────────
mkdir -p "$OUTPUT_DIR"
echo "$DYNAMIC_MANIFEST" > "${OUTPUT_DIR}/alibaba-ccm-config.yaml"
echo "$INSTALL_CONFIG"   > "${OUTPUT_DIR}/install-config.yaml"
state_set DYNAMIC_MANIFEST_FILE "${OUTPUT_DIR}/alibaba-ccm-config.yaml"

ok "Phase B done. API SLB: $API_SLB_IP"
[ -n "${JUMP_HOST_IP:-}" ] && [ "$JUMP_HOST_IP" != "null" ] && ok "Jump host: ssh root@$JUMP_HOST_IP"
ok "Next: ./04-install-cluster.sh"
