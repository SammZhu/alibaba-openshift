#!/usr/bin/env bash
# Bake ONE RHCOS aliyun boot image: download -> sha verify -> gunzip -> rootless
# re-stamp (Phase 10) -> offline gate -> write+commit provenance (P3-IMG.1).
# The matrix workflow calls this once per not-yet-baked version.
#
#   bake-one.sh <rhcos_version> <ocp_minor> <url> <sha256>
set -euo pipefail

RHCOS="${1:?rhcos version}"; OCP="${2:?ocp minor}"; URL="${3:?url}"; SHA="${4:?sha256}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT     # zero standing storage

echo "[bake] $RHCOS (OCP $OCP)"
curl -fSL --retry 3 "$URL" -o "$WORK/rhcos.qcow2.gz"
echo "$SHA  $WORK/rhcos.qcow2.gz" | sha256sum -c -          # trust anchor
gunzip -f "$WORK/rhcos.qcow2.gz"

# Diff-guard baseline = the latest already-recorded version's karg keys.
PREV="$(ls -1 bootimage/provenance/*.yaml 2>/dev/null | grep -v example | sort | tail -1 || true)"
BASELINE=""
if [ -n "$PREV" ]; then
  python3 scripts/print_baseline.py "$PREV" > "$WORK/baseline.keys"
  BASELINE="$WORK/baseline.keys"
fi

ansible-playbook -i ansible/inventory.yml ansible/playbooks/10-prepare-worker-bootimage.yml \
  -e "rhcos_qcow=$WORK/rhcos.qcow2" -e "skip_upload_until_gate=true"

export BOOTIMAGE_EMIT_BASELINE="$WORK/kargs.baseline"
scripts/bootimage-gate.sh "$WORK/rhcos.qcow2" $BASELINE     # hard gate before any record

python3 scripts/write_provenance.py \
  --rhcos "$RHCOS" --ocp "$OCP" --url "$URL" --sha256 "$SHA" \
  --baseline "$WORK/kargs.baseline" --provenance-dir bootimage/provenance \
  --guestfish "$(guestfish --version | awk '{print $2}')" \
  --qemu-img "$(qemu-img --version | head -1 | awk '{print $3}')" \
  --commit
