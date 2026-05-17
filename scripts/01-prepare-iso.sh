#!/usr/bin/env bash
#
# 01-prepare-iso.sh — Phase A.1
#   Create cluster + infra-env via Assisted Installer REST API, download
#   Discovery ISO to OUTPUT_DIR. No Alibaba Cloud calls in this phase.
#
# Outputs (saved to .state):
#   CLUSTER_ID
#   INFRA_ENV_ID
#   ISO_PATH

set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
preflight

[ -f "$PULL_SECRET_FILE" ]    || die "PULL_SECRET_FILE not found: $PULL_SECRET_FILE"
[ -f "$SSH_PUBLIC_KEY_FILE" ] || die "SSH_PUBLIC_KEY_FILE not found: $SSH_PUBLIC_KEY_FILE"
[ -f "$OFFLINE_TOKEN_FILE" ]  || die "OFFLINE_TOKEN_FILE not found. Get from https://console.redhat.com/openshift/token"

mkdir -p "$OUTPUT_DIR"
ISO_PATH="${OUTPUT_DIR}/discovery-${CLUSTER_NAME}.iso"

# ── Prevent accidental duplicate clusters in Assisted ────────────────────────
if grep -q "^CLUSTER_ID=" "$STATE_FILE" 2>/dev/null; then
  state_load
  log "Existing CLUSTER_ID found in state: $CLUSTER_ID"
  log "Verifying it still exists in Assisted..."
  if ai_curl GET "/clusters/$CLUSTER_ID" >/dev/null 2>&1; then
    ok "Cluster $CLUSTER_ID is alive; skipping creation"
    # Refresh the ISO if missing
    if [ -f "$ISO_PATH" ]; then
      ok "ISO already present: $ISO_PATH"
      exit 0
    fi
    INFRA_ENV_ID="${INFRA_ENV_ID:?missing in state}"
    log "Re-downloading ISO for existing infra-env $INFRA_ENV_ID"
  else
    warn "Stale CLUSTER_ID in state — will create a new cluster"
    : > "$STATE_FILE"   # truncate
  fi
fi

# ── Build payloads ───────────────────────────────────────────────────────────
PULL_SECRET_JSON="$(jq -c -R -s . "$PULL_SECRET_FILE")"
SSH_PUB_KEY="$(<"$SSH_PUBLIC_KEY_FILE")"

CLUSTER_PAYLOAD="$(mktemp)"
trap 'rm -f "$CLUSTER_PAYLOAD" "$INFRA_PAYLOAD" 2>/dev/null' EXIT

if [ -z "${CLUSTER_ID:-}" ]; then
  cat > "$CLUSTER_PAYLOAD" <<EOF
{
  "name": "${CLUSTER_NAME}",
  "openshift_version": "${OPENSHIFT_VERSION}",
  "cpu_architecture": "x86_64",
  "base_dns_domain": "${BASE_DOMAIN}",
  "high_availability_mode": "Full",
  "control_plane_count": ${CONTROL_PLANE_COUNT},
  "pull_secret": ${PULL_SECRET_JSON},
  "ssh_public_key": "${SSH_PUB_KEY}",
  "platform": {
    "type": "external",
    "external": {
      "platform_name": "AlibabaCloud",
      "cloud_controller_manager": "External"
    }
  }
}
EOF

  log "Creating Assisted cluster '${CLUSTER_NAME}'..."
  CLUSTER_ID="$(ai_curl POST /clusters "$CLUSTER_PAYLOAD" | jq -r .id)"
  [ -n "$CLUSTER_ID" ] && [ "$CLUSTER_ID" != "null" ] || die "Cluster creation failed"
  state_set CLUSTER_ID "$CLUSTER_ID"
fi

# ── InfraEnv (generates the ISO) ─────────────────────────────────────────────
if [ -z "${INFRA_ENV_ID:-}" ]; then
  INFRA_PAYLOAD="$(mktemp)"
  cat > "$INFRA_PAYLOAD" <<EOF
{
  "name": "${CLUSTER_NAME}-infra",
  "cluster_id": "${CLUSTER_ID}",
  "image_type": "minimal-iso",
  "pull_secret": ${PULL_SECRET_JSON},
  "ssh_authorized_key": "${SSH_PUB_KEY}",
  "openshift_version": "${OPENSHIFT_VERSION}",
  "cpu_architecture": "x86_64"
}
EOF

  log "Creating infra-env (generates ISO)..."
  INFRA_ENV_ID="$(ai_curl POST /infra-envs "$INFRA_PAYLOAD" | jq -r .id)"
  [ -n "$INFRA_ENV_ID" ] && [ "$INFRA_ENV_ID" != "null" ] || die "InfraEnv creation failed"
  state_set INFRA_ENV_ID "$INFRA_ENV_ID"
fi

# ── Wait for ISO ready ───────────────────────────────────────────────────────
log "Polling for ISO ready URL..."
DOWNLOAD_URL=""
for i in $(seq 1 30); do
  DOWNLOAD_URL="$(ai_curl GET "/infra-envs/$INFRA_ENV_ID/downloads/image-url" \
    | jq -r .url 2>/dev/null || echo "")"
  [ -n "$DOWNLOAD_URL" ] && [ "$DOWNLOAD_URL" != "null" ] && break
  sleep 5
  printf "."
done
echo
[ -n "$DOWNLOAD_URL" ] && [ "$DOWNLOAD_URL" != "null" ] \
  || die "ISO URL did not become available within 150s"

# ── Download ─────────────────────────────────────────────────────────────────
log "Downloading ISO to $ISO_PATH..."
curl -fsLo "$ISO_PATH" "$DOWNLOAD_URL"
ls -lh "$ISO_PATH"
state_set ISO_PATH "$ISO_PATH"

ok "Phase A.1 done. ISO ready at $ISO_PATH"
ok "Next: ./02-import-image.sh"
