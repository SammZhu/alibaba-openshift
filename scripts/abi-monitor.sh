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

# tiny YAML scalar reader (top-level "key: value", strips quotes/comments)
yval() { sed -nE "s/^${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/p" "$2" 2>/dev/null | head -n1 | sed 's/[[:space:]]*$//'; }

JUMP_HOST_IP="${JUMP_HOST_IP:-$(yval jump_host_ip "$STATE")}"
MIRROR_PRIVATE_IP="${MIRROR_PRIVATE_IP:-$(yval mirror_private_ip "$STATE")}"
SSH_PRIV_KEY_FILE="${SSH_PRIV_KEY_FILE:-$(yval ssh_priv_key_file "$GV")}"
# group_vars value may use a Jinja env lookup; fall back to the documented default
[ -z "${SSH_PRIV_KEY_FILE:-}" ] || case "$SSH_PRIV_KEY_FILE" in *'{{'*) SSH_PRIV_KEY_FILE="$HOME/.ssh/openshift_ed25519";; esac
SSH_PRIV_KEY_FILE="${SSH_PRIV_KEY_FILE:-$HOME/.ssh/openshift_ed25519}"
M_INSTALL_DIR="${M_INSTALL_DIR:-/var/lib/quay-storage/agent-build/install}"

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
