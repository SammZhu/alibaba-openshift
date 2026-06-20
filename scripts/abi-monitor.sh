#!/usr/bin/env bash
# abi-monitor.sh — LIVE Agent-based install progress monitor.
#
# Why this exists: 07-install-cluster-agent.yml runs `openshift-install agent
# wait-for ...` on the mirror ECS under ansible async/poll, which BUFFERS the
# command's stdout — so during the 40-90 min install the operator sees nothing
# and can't tell whether it's bootstrapping, pivoting, or stuck.  This script
# streams the very same `agent wait-for` output LIVE (no ansible buffering) by
# sshing straight to the mirror ECS, where the agent assets (install_dir with
# .openshift_install_state.json) were built by 01 (iso_agent.yml).  Run it in a
# SECOND terminal on the operator host (RHEL8) right after you kick off 06
# (cluster-stack) / site-agent.yml.  It is read-only — it only watches.
#
# Usage:
#   scripts/abi-monitor.sh                  # bootstrap, then install-complete
#   scripts/abi-monitor.sh install-complete # jump straight to install-complete
#   scripts/abi-monitor.sh bootstrap-complete
#
# Connection facts come from ansible/state.yml (jump_host_ip, mirror_private_ip)
# and group_vars/all.yml (ssh_priv_key_file), matching the playbooks.  Override
# any of them via env: JUMP_HOST_IP, MIRROR_PRIVATE_IP, SSH_PRIV_KEY_FILE,
# M_INSTALL_DIR.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANSIBLE_DIR="$(cd "$HERE/../ansible" && pwd)"
STATE="$ANSIBLE_DIR/state.yml"
GV="$ANSIBLE_DIR/group_vars/all.yml"

# tiny YAML scalar reader: grab everything after "key:", strip trailing
# inline comment and surrounding quotes (keeps inner quotes intact).
yval() {
  sed -nE "s/^${1}:[[:space:]]*(.*)/\1/p" "$2" 2>/dev/null | head -n1 \
    | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]*$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

# Resolve a value that may be a Jinja env lookup like
#   {{ lookup('env', 'HOME') }}/.ssh/openshift_ed25519
resolve_jinja_home() {
  printf '%s' "$1" | sed -E "s#\{\{[[:space:]]*lookup\([^)]*'HOME'[^)]*\)[[:space:]]*\}\}#${HOME}#g"
}

JUMP_HOST_IP="${JUMP_HOST_IP:-$(yval jump_host_ip "$STATE")}"
MIRROR_PRIVATE_IP="${MIRROR_PRIVATE_IP:-$(yval mirror_private_ip "$STATE")}"
SSH_PRIV_KEY_FILE="${SSH_PRIV_KEY_FILE:-$(yval ssh_priv_key_file "$GV")}"
SSH_PRIV_KEY_FILE="$(resolve_jinja_home "$SSH_PRIV_KEY_FILE")"
SSH_PRIV_KEY_FILE="${SSH_PRIV_KEY_FILE/#\~/$HOME}"
M_INSTALL_DIR="${M_INSTALL_DIR:-/var/lib/quay-storage/agent-build/install}"

# If the resolved key doesn't exist, try to auto-discover one under ~/.ssh so
# the operator isn't forced to hand-set SSH_PRIV_KEY_FILE.
if [ -n "$SSH_PRIV_KEY_FILE" ] && [ ! -f "$SSH_PRIV_KEY_FILE" ]; then
  echo "WARN: ssh key '$SSH_PRIV_KEY_FILE' not found — searching ~/.ssh ..."
  for c in "$HOME"/.ssh/openshift* "$HOME"/.ssh/*.pem "$HOME"/.ssh/id_*; do
    case "$c" in *.pub) continue;; esac
    [ -f "$c" ] || continue
    SSH_PRIV_KEY_FILE="$c"; echo "WARN: using '$c' (override with SSH_PRIV_KEY_FILE=...)"; break
  done
fi
if [ ! -f "${SSH_PRIV_KEY_FILE:-/nonexistent}" ]; then
  echo "ERROR: no usable ssh private key. Set SSH_PRIV_KEY_FILE=/path/to/key and re-run."
  echo "       (check: grep ssh_priv_key_file ansible/group_vars/all.yml ; ls -la ~/.ssh)"
  exit 1
fi

MILESTONE="${1:-both}"

for v in JUMP_HOST_IP MIRROR_PRIVATE_IP SSH_PRIV_KEY_FILE; do
  if [ -z "${!v:-}" ]; then echo "ERROR: $v unresolved (set it via env or run after 03/04)"; exit 1; fi
done

SSH_COMMON=(-i "$SSH_PRIV_KEY_FILE" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
            -o ServerAliveInterval=30 -o ServerAliveCountMax=10 -o TCPKeepAlive=yes)
PROXY="ssh -i $SSH_PRIV_KEY_FILE -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p root@${JUMP_HOST_IP}"

echo "==> ABI monitor  mirror=${MIRROR_PRIVATE_IP}  via jump=${JUMP_HOST_IP}  dir=${M_INSTALL_DIR}"

mssh() { ssh "${SSH_COMMON[@]}" -o "ProxyCommand=${PROXY}" "root@${MIRROR_PRIVATE_IP}" "$@"; }

# sanity: assets present?
if ! mssh "test -f ${M_INSTALL_DIR}/.openshift_install_state.json"; then
  echo "ERROR: ${M_INSTALL_DIR}/.openshift_install_state.json not found on mirror ECS — has 01 (iso_agent) run?"
  exit 1
fi

watch_milestone() {
  local m="$1"
  echo "================ wait-for ${m} ($(date '+%H:%M:%S')) ================"
  # -tt: force a pty so openshift-install streams its progress lines live
  ssh -tt "${SSH_COMMON[@]}" -o "ProxyCommand=${PROXY}" "root@${MIRROR_PRIVATE_IP}" \
    "openshift-install agent wait-for ${m} --dir ${M_INSTALL_DIR} --log-level=info" \
  | sed -u "s/^/[${m}] /" || return $?
}

case "$MILESTONE" in
  both)              watch_milestone bootstrap-complete; watch_milestone install-complete ;;
  bootstrap-complete) watch_milestone bootstrap-complete ;;
  install-complete)  watch_milestone install-complete ;;
  *) echo "usage: $0 [both|bootstrap-complete|install-complete]"; exit 2 ;;
esac

echo "==> done ($(date '+%H:%M:%S'))"
