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

# Diff-guard baseline = the newest recorded version OF THE SAME RHEL GENERATION.
# It used to be `ls | sort | tail -1`, which is lexicographic and therefore wrong:
# upstream changed the RHCOS naming scheme at 4.19, so the directory sorts
#   10.2.20260715-0  <  418.94.202608142238-0  <  9.6.20260818-0
# and `tail -1` always lands on a 9.x entry. Every 10.x bake — i.e. 4.22 and
# everything after it — would be diffed against a RHEL 9 baseline, which is the
# cross-generation comparison the same-generation rule exists to prevent (two
# RHEL bases may legitimately carry different karg sets, so it can fail a good
# image). The 4.22 bake did exactly that and passed only because the keys matched.
PREV="$(python3 scripts/pick_baseline.py bootimage/provenance "$RHCOS")"
BASELINE=""
if [ -n "$PREV" ]; then
  python3 scripts/print_baseline.py "bootimage/provenance/$PREV.yaml" > "$WORK/baseline.keys"
  BASELINE="$WORK/baseline.keys"
  echo "[bake] diff guard baseline: $PREV (same RHEL generation)"
else
  echo "[bake] no same-generation provenance entry yet — absolute checks only"
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
