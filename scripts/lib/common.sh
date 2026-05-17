# Shared helpers for the test-cluster automation scripts.
# Sourced by every 0X-*.sh script — never executed directly.
#
# Platform: RHEL 8 / Alibaba Cloud Linux 3 (or any EL8-compatible).
# Uses GNU sed (-i), GNU base64 (-w0), GNU stat (-c%s) — these scripts
# will NOT run unmodified on macOS / BSD. Use the ansible/ playbooks
# for cross-platform automation.

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$LIB_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSPACE_ROOT="$(cd "$REPO_ROOT/.." && pwd)"
STATE_FILE="$SCRIPT_DIR/.state"
CONFIG_FILE="$SCRIPT_DIR/config.sh"

# ── Logging ──────────────────────────────────────────────────────────────────
log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required in PATH"; }

# ── Config loading ───────────────────────────────────────────────────────────
load_config() {
  [ -f "$CONFIG_FILE" ] || die "Missing $CONFIG_FILE — copy from config.sh.example and edit"
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  : "${CLUSTER_NAME:?CLUSTER_NAME is required}"
  : "${BASE_DOMAIN:?BASE_DOMAIN is required}"
  : "${REGION:?REGION is required}"
  : "${ZONE:?ZONE is required}"
  : "${OSS_BUCKET:?OSS_BUCKET is required}"
  : "${ALIBABA_CLOUD_PROFILE:?ALIBABA_CLOUD_PROFILE is required}"
  export ALIBABA_CLOUD_PROFILE
}

# ── State management ─────────────────────────────────────────────────────────
# State file is a flat KEY=VALUE store sourced by later scripts.
state_load() {
  [ -f "$STATE_FILE" ] || return 0
  # shellcheck disable=SC1090
  source "$STATE_FILE"
}

state_set() {
  local key="$1" value="$2"
  mkdir -p "$(dirname "$STATE_FILE")"
  # GNU sed -i (RHEL 8 / Alibaba Linux 3). Removes any prior assignment
  # of the same key, then appends the new value.
  [ -f "$STATE_FILE" ] && sed -i "/^${key}=/d" "$STATE_FILE"
  echo "${key}=${value}" >> "$STATE_FILE"
  ok "state: ${key}=${value}"
}

state_require() {
  for key in "$@"; do
    if ! grep -q "^${key}=" "$STATE_FILE" 2>/dev/null; then
      die "state: ${key} not set — run earlier script(s) first"
    fi
  done
  state_load
}

# ── Assisted Installer API helpers ───────────────────────────────────────────
ASSISTED_API="https://api.openshift.com/api/assisted-install/v2"
SSO_URL="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"

# Kept for backwards-compat; new code should call ai_token (auto-cached + auto-refresh).
ai_access_token() { _AI_TOKEN=""; _AI_TOKEN_EXPIRES_AT=0; ai_token; }

# Access token cache with auto-refresh. Assisted SSO tokens default to ~15 min
# TTL — install monitoring spans 45-60 min, so we MUST refresh during long polls.
_AI_TOKEN=""
_AI_TOKEN_EXPIRES_AT=0
ai_token() {
  local now; now="$(date +%s)"
  # Refresh 60 s before stated expiry to leave room for clock drift + the call.
  if [ -z "$_AI_TOKEN" ] || [ "$now" -ge "$_AI_TOKEN_EXPIRES_AT" ]; then
    [ -f "${OFFLINE_TOKEN_FILE:-}" ] || die "OFFLINE_TOKEN_FILE not configured"
    local offline resp expires
    offline="$(cat "$OFFLINE_TOKEN_FILE")"
    resp="$(curl -sf \
      --data-urlencode "grant_type=refresh_token" \
      --data-urlencode "client_id=cloud-services" \
      --data-urlencode "refresh_token=${offline}" \
      "$SSO_URL")" || die "Failed to refresh Assisted access token — offline token may be revoked"
    _AI_TOKEN="$(echo "$resp" | jq -r .access_token)"
    expires="$(echo "$resp" | jq -r '.expires_in // 900')"
    [ -n "$_AI_TOKEN" ] && [ "$_AI_TOKEN" != "null" ] \
      || die "Token refresh returned no access_token. Get a fresh offline token from https://console.redhat.com/openshift/token"
    _AI_TOKEN_EXPIRES_AT=$((now + expires - 60))
  fi
  echo "$_AI_TOKEN"
}

# Usage: ai_curl METHOD PATH [data-file-or-string]
ai_curl() {
  local method="$1" path="$2" data="${3:-}"
  local -a args=( -sf -X "$method"
    -H "Authorization: Bearer $(ai_token)"
    -H "Content-Type: application/json" )
  if [ -n "$data" ]; then
    if [ -f "$data" ]; then
      args+=( --data-binary "@${data}" )
    else
      args+=( --data-binary "$data" )
    fi
  fi
  curl "${args[@]}" "${ASSISTED_API}${path}"
}

# ── Aliyun helpers ───────────────────────────────────────────────────────────
aliyun_q() {
  # Shorthand: aliyun_q <service> <Action> <key1=val1> <key2=val2> ...
  local svc="$1" action="$2"; shift 2
  local -a flags=()
  for kv in "$@"; do
    flags+=( "--${kv%%=*}" "${kv#*=}" )
  done
  aliyun "$svc" "$action" "${flags[@]}"
}

# ── ECS Image Import RAM role ────────────────────────────────────────────────
# Alibaba Cloud requires AliyunECSImageImportDefaultRole to exist before
# ImportImage works. The web console auto-creates it on first use; CLI/SDK
# users must create it themselves (Forbidden.RAM otherwise).
ensure_ecs_image_import_role() {
  if aliyun ram GetRole --RoleName AliyunECSImageImportDefaultRole >/dev/null 2>&1; then
    return 0
  fi
  log "Creating AliyunECSImageImportDefaultRole (one-time)..."
  local policy='{"Statement":[{"Action":"sts:AssumeRole","Effect":"Allow","Principal":{"Service":["ecs.aliyuncs.com"]}}],"Version":"1"}'
  aliyun ram CreateRole \
    --RoleName AliyunECSImageImportDefaultRole \
    --AssumeRolePolicyDocument "$policy" >/dev/null \
    || die "Failed to create AliyunECSImageImportDefaultRole — check RAM permissions"
  aliyun ram AttachPolicyToRole \
    --PolicyType System \
    --PolicyName AliyunECSImageImportRolePolicy \
    --RoleName AliyunECSImageImportDefaultRole >/dev/null \
    || die "Failed to attach AliyunECSImageImportRolePolicy"
  ok "RAM role + policy created"
}

# ── Validation ───────────────────────────────────────────────────────────────
preflight() {
  for tool in aliyun curl jq base64; do need "$tool"; done
  load_config
  state_load
}

# Same as preflight, but also verifies the offline token can mint an access
# token. Use in scripts that hit the Assisted API (01, 04, 99).
preflight_with_assisted() {
  preflight
  log "Verifying Red Hat offline token..."
  ai_token >/dev/null  # exits via die() with an actionable error if invalid
  ok "Assisted API auth ready"
}
