#!/usr/bin/env bash
#
# 04-install-cluster.sh — Phase C
#   Wait for hosts to register, set host roles, upload custom manifests,
#   trigger install, wait for install-complete, download kubeconfig.
#
# Outputs (saved to .state):
#   KUBECONFIG_PATH
#   KUBEADMIN_PASSWORD_PATH

set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
preflight_with_assisted
state_require CLUSTER_ID INFRA_ENV_ID API_SLB_IP

# Manifests to upload, format: <folder>|<file-name>|<local-source-path>
MANIFESTS=(
  "manifests|00-ovn-mtu.yaml|${REPO_ROOT}/custom_manifests/00-ovn-mtu.yaml"
  "manifests|01-alibaba-ccm.yaml|${REPO_ROOT}/custom_manifests/01-alibaba-ccm.yaml"
  "manifests|alibaba-ccm-config.yaml|${DYNAMIC_MANIFEST_FILE:-${OUTPUT_DIR}/alibaba-ccm-config.yaml}"
  "openshift|03-machineconfig-providerid.yaml|${REPO_ROOT}/custom_manifests/03-machineconfig-providerid.yaml"
)

# ── Step 1: Wait for all hosts to register in Assisted ───────────────────────
# Compact 3-node: just CONTROL_PLANE_COUNT (typically 3).
# Standard topology: + COMPUTE_COUNT workers.
EXPECTED_HOSTS=$((CONTROL_PLANE_COUNT + COMPUTE_COUNT))
log "Waiting for $EXPECTED_HOSTS hosts to register with Assisted..."
SECONDS_WAITED=0
while true; do
  HOSTS_JSON="$(ai_curl GET "/infra-envs/$INFRA_ENV_ID/hosts")"
  HOST_COUNT="$(echo "$HOSTS_JSON" | jq 'length')"
  KNOWN_COUNT="$(echo "$HOSTS_JSON" | jq '[.[] | select(.status=="known" or .status=="known-unbound" or .status=="insufficient")] | length')"
  printf '\r  [%4ds] hosts: %d total, %d discovered ' "$SECONDS_WAITED" "$HOST_COUNT" "$KNOWN_COUNT"
  [ "$KNOWN_COUNT" -ge "$EXPECTED_HOSTS" ] && { echo; ok "All $EXPECTED_HOSTS hosts discovered"; break; }
  sleep 20
  SECONDS_WAITED=$((SECONDS_WAITED + 20))
  [ "$SECONDS_WAITED" -gt 1800 ] && die "Hosts did not register within 30 min — check ECS console.bootlog / Assisted UI"
done

# ── Step 2: Assign roles (first CONTROL_PLANE_COUNT = master, rest = worker) ─
log "Assigning roles: $CONTROL_PLANE_COUNT master + $COMPUTE_COUNT worker"
i=0
echo "$HOSTS_JSON" | jq -r '.[].id' | while read -r HOST_ID; do
  if [ "$i" -lt "$CONTROL_PLANE_COUNT" ]; then
    ROLE=master
  else
    ROLE=worker
  fi
  log "  $HOST_ID -> $ROLE"
  ai_curl PATCH "/infra-envs/$INFRA_ENV_ID/hosts/$HOST_ID" \
    "{\"host_role\":\"$ROLE\"}" >/dev/null
  i=$((i+1))
done

# ── Step 3: Upload custom manifests ──────────────────────────────────────────
log "Uploading custom manifests..."
for entry in "${MANIFESTS[@]}"; do
  IFS='|' read -r folder fname path <<< "$entry"
  [ -f "$path" ] || die "Manifest source not found: $path"
  log "  $folder/$fname"
  CONTENT_B64="$(base64 -w0 < "$path")"
  PAYLOAD="$(jq -n --arg f "$folder" --arg n "$fname" --arg c "$CONTENT_B64" \
    '{folder:$f, file_name:$n, content:$c}')"
  ai_curl POST "/clusters/$CLUSTER_ID/manifests" "$PAYLOAD" >/dev/null
done
ok "Uploaded ${#MANIFESTS[@]} manifests"

# ── Step 4: Wait cluster ready-to-install ────────────────────────────────────
log "Waiting for cluster ready-to-install validation..."
SECONDS_WAITED=0
while true; do
  STATUS="$(ai_curl GET "/clusters/$CLUSTER_ID" | jq -r .status)"
  printf '\r  [%4ds] cluster status: %-30s' "$SECONDS_WAITED" "$STATUS"
  case "$STATUS" in
    ready) echo; ok "Cluster ready to install"; break;;
    insufficient|pending-for-input) ;;
    *) ;;
  esac
  sleep 15
  SECONDS_WAITED=$((SECONDS_WAITED + 15))
  [ "$SECONDS_WAITED" -gt 600 ] && die "Cluster never became ready — check Assisted UI for validation errors"
done

# ── Step 5: Trigger install ──────────────────────────────────────────────────
log "Triggering install..."
ai_curl POST "/clusters/$CLUSTER_ID/actions/install" >/dev/null

# ── Step 6: Poll until installed ─────────────────────────────────────────────
log "Installing (45-60 min). Status updates every minute..."
log "  (Assisted access token auto-refreshes every ~15 min via common.sh)"
SECONDS_WAITED=0
LAST_STATUS=""
while true; do
  CLUSTER_JSON="$(ai_curl GET "/clusters/$CLUSTER_ID")"
  STATUS="$(echo "$CLUSTER_JSON" | jq -r .status)"
  STATUS_INFO="$(echo "$CLUSTER_JSON" | jq -r '.status_info // ""')"
  if [ "$STATUS" != "$LAST_STATUS" ]; then
    echo
    log "  [$((SECONDS_WAITED/60)) min] $STATUS — $STATUS_INFO"
    LAST_STATUS="$STATUS"
  else
    printf '\r  [%3d min] %s ' "$((SECONDS_WAITED/60))" "$STATUS"
  fi
  case "$STATUS" in
    installed)
      echo; ok "Cluster installed!"; break;;
    error|cancelled)
      echo
      warn "Install failed: $STATUS_INFO"
      warn "Full diagnosis:  curl -sH 'Authorization: Bearer \$(./lib/common.sh ai_token)' '${ASSISTED_API}/clusters/${CLUSTER_ID}' | jq"
      die "Install failed";;
    pending-for-input|installing-pending-user-action)
      echo
      die "Cluster waiting on user input — see Assisted UI:
    https://console.redhat.com/openshift/assisted-installer/clusters/${CLUSTER_ID}";;
  esac
  sleep 60
  SECONDS_WAITED=$((SECONDS_WAITED + 60))
done

# ── Step 7: Download credentials ─────────────────────────────────────────────
mkdir -p "${OUTPUT_DIR}/auth"
KUBECONFIG_PATH="${OUTPUT_DIR}/auth/kubeconfig"
KUBEADMIN_PATH="${OUTPUT_DIR}/auth/kubeadmin-password"

log "Downloading kubeconfig..."
curl -sf "${ASSISTED_API}/clusters/${CLUSTER_ID}/downloads/credentials?file_name=kubeconfig" \
  -H "Authorization: Bearer $(ai_token)" -o "$KUBECONFIG_PATH"
chmod 600 "$KUBECONFIG_PATH"
state_set KUBECONFIG_PATH "$KUBECONFIG_PATH"

log "Downloading kubeadmin password..."
curl -sf "${ASSISTED_API}/clusters/${CLUSTER_ID}/downloads/credentials?file_name=kubeadmin-password" \
  -H "Authorization: Bearer $(ai_token)" -o "$KUBEADMIN_PATH"
chmod 600 "$KUBEADMIN_PATH"
state_set KUBEADMIN_PASSWORD_PATH "$KUBEADMIN_PATH"

# ── Step 8: scp kubeconfig to jump host if configured ────────────────────────
if [ -n "${JUMP_HOST_IP:-}" ] && [ "$JUMP_HOST_IP" != "null" ]; then
  log "Copying kubeconfig to jump host $JUMP_HOST_IP..."
  # Wait for jump host SSH ready (cloud-init may still be running)
  for i in $(seq 1 30); do
    ssh -i "$SSH_PRIVATE_KEY_FILE" -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        root@"$JUMP_HOST_IP" "ls /var/log/userdata.done 2>/dev/null" >/dev/null 2>&1 && break
    sleep 10
  done
  scp -i "$SSH_PRIVATE_KEY_FILE" -o StrictHostKeyChecking=no \
      "$KUBECONFIG_PATH" "root@$JUMP_HOST_IP:/root/kubeconfig"
  ok "Kubeconfig copied to jump host"
  echo
  echo "──────────────────────────────────────────────────────────"
  echo "  ssh root@$JUMP_HOST_IP"
  echo "  export KUBECONFIG=/root/kubeconfig"
  echo "  oc get nodes"
  echo "──────────────────────────────────────────────────────────"
fi

ok "Phase C done. Kubeconfig: $KUBECONFIG_PATH"
ok "Next: ./05-deploy-post-install.sh (must run on jump host)"
