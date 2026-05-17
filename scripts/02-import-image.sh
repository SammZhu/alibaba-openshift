#!/usr/bin/env bash
#
# 02-import-image.sh — Phase A.2 to A.4
#   Upload Discovery ISO to OSS, import as ECS custom image, wait Available.
#
# Outputs (saved to .state):
#   ECS_IMAGE_ID

set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
preflight
state_require ISO_PATH

[ -f "$ISO_PATH" ] || die "ISO not found at $ISO_PATH — re-run 01-prepare-iso.sh"

OSS_OBJECT="discovery-${CLUSTER_NAME}.iso"

# ── Idempotency: skip if already imported ────────────────────────────────────
if [ -n "${ECS_IMAGE_ID:-}" ]; then
  STATUS="$(aliyun ecs DescribeImages --RegionId "$REGION" --ImageId "$ECS_IMAGE_ID" \
    --query 'Images.Image[0].Status' --output text 2>/dev/null || echo "")"
  if [ "$STATUS" = "Available" ]; then
    ok "Image $ECS_IMAGE_ID already Available; skipping"
    exit 0
  elif [ "$STATUS" = "Creating" ] || [ "$STATUS" = "Importing" ]; then
    log "Existing import in progress ($STATUS); resuming poll"
  else
    warn "Stale ECS_IMAGE_ID; will re-import"
    ECS_IMAGE_ID=""
  fi
fi

# ── Ensure OSS bucket exists in the right region ─────────────────────────────
if ! aliyun oss ls "oss://${OSS_BUCKET}/" --region "$REGION" >/dev/null 2>&1; then
  log "Creating OSS bucket ${OSS_BUCKET} in ${REGION}..."
  aliyun oss mb "oss://${OSS_BUCKET}" --region "$REGION"
fi

# ── Upload ISO (skip if same size already there) ─────────────────────────────
REMOTE_SIZE="$(aliyun oss stat "oss://${OSS_BUCKET}/${OSS_OBJECT}" --region "$REGION" 2>/dev/null \
  | awk '/Content-Length/ {print $2}' || echo "")"
LOCAL_SIZE="$(stat -c%s "$ISO_PATH")"
if [ -n "$REMOTE_SIZE" ] && [ "$REMOTE_SIZE" = "$LOCAL_SIZE" ]; then
  ok "ISO already in OSS with matching size; skipping upload"
else
  log "Uploading ${ISO_PATH} -> oss://${OSS_BUCKET}/${OSS_OBJECT} ..."
  aliyun oss cp "$ISO_PATH" "oss://${OSS_BUCKET}/${OSS_OBJECT}" --region "$REGION" -f
fi

# ── Import as ECS custom image ───────────────────────────────────────────────
if [ -z "${ECS_IMAGE_ID:-}" ]; then
  # Ensure ECS can read from OSS during import (first-time setup).
  ensure_ecs_image_import_role

  log "Submitting ImportImage..."
  IMPORT_OUTPUT="$(aliyun ecs ImportImage \
    --RegionId "$REGION" \
    --ImageName "openshift-${CLUSTER_NAME}-iso" \
    --OSType Linux \
    --Platform Others_Linux \
    --Architecture x86_64 \
    --DiskDeviceMapping.1.Format ISO \
    --DiskDeviceMapping.1.OSSBucket "$OSS_BUCKET" \
    --DiskDeviceMapping.1.OSSObject "$OSS_OBJECT" 2>&1)" || {
    echo "$IMPORT_OUTPUT" >&2
    die "ImportImage failed. Common causes:
    - Forbidden.RAM           → role just created; retry in 60s (RAM propagation)
    - InvalidOSSObject.NotFound → check OSS_BUCKET / region match
    - InvalidImageName        → ImageName already exists; bump version or delete first"
  }
  ECS_IMAGE_ID="$(echo "$IMPORT_OUTPUT" | jq -r '.ImageId // empty')"
  [ -n "$ECS_IMAGE_ID" ] || die "ImportImage returned no ImageId. Raw response: $IMPORT_OUTPUT"
  state_set ECS_IMAGE_ID "$ECS_IMAGE_ID"
fi

# ── Wait Available ───────────────────────────────────────────────────────────
log "Waiting for image $ECS_IMAGE_ID to become Available (15-30 min)..."
SECONDS_WAITED=0
while true; do
  STATUS="$(aliyun ecs DescribeImages --RegionId "$REGION" --ImageId "$ECS_IMAGE_ID" \
    --query 'Images.Image[0].Status' --output text 2>/dev/null || echo "")"
  case "$STATUS" in
    Available) ok "Image Available after ${SECONDS_WAITED}s"; break;;
    Creating|Importing|Waiting) printf '\r  [%4ds] Status: %s ' "$SECONDS_WAITED" "$STATUS";;
    "") die "Could not query image status";;
    *) die "Image import failed with status: $STATUS";;
  esac
  sleep 30
  SECONDS_WAITED=$((SECONDS_WAITED + 30))
done

ok "Phase A.2-4 done. ECS_IMAGE_ID=$ECS_IMAGE_ID"
ok "Next: ./03-create-stack.sh"
