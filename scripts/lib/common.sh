# Shared helpers for the test-cluster automation scripts.
# Sourced by every 0X-*.sh script — never executed directly.

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
  # Replace existing line or append
  if [ -f "$STATE_FILE" ] && grep -q "^${key}=" "$STATE_FILE"; then
    # Use a tmp file for portability between gnu/bsd sed
    grep -v "^${key}=" "$STATE_FILE" > "$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
  fi
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

ai_access_token() {
  [ -f "${OFFLINE_TOKEN_FILE:-}" ] || die "OFFLINE_TOKEN_FILE not configured"
  local offline
  offline="$(cat "$OFFLINE_TOKEN_FILE")"
  curl -sf \
    --data-urlencode "grant_type=refresh_token" \
    --data-urlencode "client_id=cloud-services" \
    --data-urlencode "refresh_token=${offline}" \
    "$SSO_URL" | jq -r .access_token
}

# Cached access token for the duration of one script run.
_AI_TOKEN=""
ai_token() {
  if [ -z "$_AI_TOKEN" ]; then
    _AI_TOKEN="$(ai_access_token)"
    [ -n "$_AI_TOKEN" ] && [ "$_AI_TOKEN" != "null" ] \
      || die "Failed to get Assisted access token — check OFFLINE_TOKEN"
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

# ── Validation ───────────────────────────────────────────────────────────────
preflight() {
  for tool in aliyun curl jq base64; do need "$tool"; done
  load_config
  state_load
}
