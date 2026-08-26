#!/usr/bin/env bash
# bootstrap-operator.sh — the ONE manual step on a brand-new operator host.
#
# Installs just enough to run ansible (git + ansible-core + python3); everything
# else is done by ansible/playbooks/00a-prepare-operator.yml.  Keep this script
# tiny — logic belongs in the playbook, not here.
#
#   # direct internet:
#   ./bootstrap-operator.sh
#   # behind an HTTP proxy (Apsara):
#   PROXY=http://10.x.x.x:3128 ./bootstrap-operator.sh
#
# Then:
#   git clone <repo> /root/alibaba-openshift
#   cd /root/alibaba-openshift/ansible
#   ansible-playbook -i inventory.yml playbooks/00a-prepare-operator.yml
set -euo pipefail

PROXY="${PROXY:-}"
if [ -n "$PROXY" ]; then
  export http_proxy="$PROXY" https_proxy="$PROXY"
  # dnf reads its own config, not the environment
  if ! grep -q '^proxy=' /etc/dnf/dnf.conf 2>/dev/null; then
    echo "proxy=$PROXY" >> /etc/dnf/dnf.conf
  else
    sed -i "s|^proxy=.*|proxy=$PROXY|" /etc/dnf/dnf.conf
  fi
  echo "==> proxy: $PROXY"
else
  echo "==> proxy: (direct)"
fi

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

PKG=""
for c in dnf yum apt-get zypper; do command -v "$c" >/dev/null 2>&1 && { PKG="$c"; break; }; done
[ -n "$PKG" ] || { echo "!! no supported package manager (dnf/yum/apt-get/zypper)"; exit 1; }
echo "==> package manager: $PKG"

case "$PKG" in
  dnf|yum)   $SUDO "$PKG" install -y git python3 python3-pip ansible-core || \
             $SUDO "$PKG" install -y git python3 python3-pip ansible ;;
  apt-get)   [ -n "$PROXY" ] && echo "Acquire::http::Proxy \"$PROXY\";" | $SUDO tee /etc/apt/apt.conf.d/99proxy >/dev/null
             $SUDO apt-get update -y && $SUDO apt-get install -y git python3 python3-pip ansible ;;
  zypper)    $SUDO zypper --non-interactive install git python3 python3-pip ansible ;;
esac

# Some minimal images ship ansible-core without the package; fall back to pip.
if ! command -v ansible-playbook >/dev/null 2>&1; then
  echo "==> ansible not in the repos — installing via pip"
  $SUDO python3 -m pip install --upgrade pip
  $SUDO python3 -m pip install ansible-core
fi

echo
echo "==> installed:"
for t in git python3 ansible-playbook; do
  printf '    %-16s %s\n' "$t" "$(command -v "$t" 2>/dev/null || echo MISSING)"
done
ansible-playbook --version 2>/dev/null | head -1 || true

cat <<'NEXT'

==> next:
    git clone <repo-url> /root/alibaba-openshift
    cd /root/alibaba-openshift/ansible
    ansible-playbook -i inventory.yml playbooks/00a-prepare-operator.yml
    # (apsara: add  -e operator_proxy=http://<squid>:3128  if group_vars isn't filled in yet)
NEXT
