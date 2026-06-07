#!/usr/bin/env bash
# Register this (internal) RHEL 8 box as the GitHub self-hosted runner that the
# bootimage bake job runs on (P3-IMG.1). Run THIS ON THE RHEL 8 box.
#
# The runner makes an OUTBOUND long-poll to GitHub — no inbound firewall hole.
# It only needs egress to github.com:443, the RHCOS source (prefer the internal
# mirror), and the Alibaba OSS/ECS internal endpoints.
#
# Usage:
#   register-bootimage-runner.sh --check                         # prereqs + connectivity only
#   RUNNER_TOKEN=<token> register-bootimage-runner.sh --register # download + configure + install
#
# Get RUNNER_TOKEN from:
#   GitHub repo -> Settings -> Actions -> Runners -> New self-hosted runner (Linux),
#   OR:  gh api -X POST repos/SammZhu/alibaba-openshift/actions/runners/registration-token -q .token
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/SammZhu/alibaba-openshift}"
LABELS="${LABELS:-rhel8,alibaba-vpc}"          # must satisfy the workflow's runs-on
RUNNER_VERSION="${RUNNER_VERSION:-2.319.1}"
RUNNER_DIR="${RUNNER_DIR:-$HOME/actions-runner}"
EPHEMERAL="${EPHEMERAL:-true}"                  # JIT: deregister after each job (recommended)

# Prereqs the bake actually uses. oc/aliyun optional under --check (warn only).
HARD=(git curl tar python3 qemu-img guestfish gunzip sha256sum ansible-playbook)
SOFT=(aliyun oc)

check() {
  local ok=1
  echo "== prerequisites =="
  for t in "${HARD[@]}"; do
    if command -v "$t" >/dev/null 2>&1; then printf "  ok   %s\n" "$t"
    else printf "  MISS %s   (RHEL8: see hints below)\n" "$t"; ok=0; fi
  done
  for t in "${SOFT[@]}"; do
    command -v "$t" >/dev/null 2>&1 && printf "  ok   %s\n" "$t" || printf "  warn %s (needed at bake time)\n" "$t"
  done
  echo "== outbound connectivity (zero inbound needed) =="
  if curl -fsS -o /dev/null --max-time 10 https://github.com; then echo "  ok   github.com:443 reachable"
  else echo "  MISS github.com:443 — set HTTPS_PROXY or use the cron-poller fallback"; ok=0; fi
  echo "== libguestfs backend =="
  echo "  LIBGUESTFS_BACKEND=${LIBGUESTFS_BACKEND:-(unset; bake exports =direct for no-nested-virt)}"
  if [ "$ok" = 1 ]; then echo "RESULT: ready to register"; else
    cat <<'HINT'
RESULT: missing prerequisites. On RHEL 8:
  sudo dnf install -y git curl tar python3 qemu-img libguestfs-tools libguestfs-tools-c
  # ansible:  python3 -m pip install --user ansible
  # aliyun:   https://help.aliyun.com/document_detail/121541.html  (or the project's setup)
  # oc:       from the cluster mirror / openshift-clients
HINT
    return 1
  fi
}

register() {
  : "${RUNNER_TOKEN:?set RUNNER_TOKEN (GitHub registration token) — see header}"
  check || { echo "fix prerequisites first"; exit 1; }
  mkdir -p "$RUNNER_DIR"; cd "$RUNNER_DIR"
  local tgz="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  if [ ! -x ./run.sh ]; then
    echo "== download runner ${RUNNER_VERSION} =="
    curl -fSLo "$tgz" "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${tgz}"
    tar xzf "$tgz"
  fi
  local args=(--url "$REPO_URL" --token "$RUNNER_TOKEN" --labels "$LABELS"
              --name "bootimage-$(hostname -s)" --unattended --replace)
  [ "$EPHEMERAL" = true ] && args+=(--ephemeral)
  echo "== configure (labels: self-hosted,$LABELS) =="
  ./config.sh "${args[@]}"
  if [ "$EPHEMERAL" = true ]; then
    echo "== ephemeral: run one job then deregister =="
    echo "Run now with:   (cd $RUNNER_DIR && ./run.sh)"
    echo "Or wrap in a systemd unit / loop to re-register per job."
  else
    echo "== install as a systemd service =="
    sudo ./svc.sh install && sudo ./svc.sh start && sudo ./svc.sh status
  fi
  echo "DONE. Verify under GitHub repo -> Settings -> Actions -> Runners."
}

case "${1:---check}" in
  --check) check ;;
  --register) register ;;
  *) echo "usage: $0 [--check|--register]"; exit 2 ;;
esac
