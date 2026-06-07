#!/usr/bin/env bash
# Offline format gate for a re-stamped RHCOS aliyun qcow2 (P3-IMG.1).
#
# Runs on the bake host (self-hosted runner / RHEL) AFTER the guestfish re-stamp,
# BEFORE OSS upload + ImportImage. Catches a silently-broken image at bake time
# instead of at cluster-boot time. Boot-free; structural + karg assertions only.
#
#   bootimage-gate.sh <qcow2> [baseline-keys-file]
#
# Exit 0 = blessed-able; non-zero = a named failure (do NOT upload/import).
set -euo pipefail

QCOW="${1:?usage: bootimage-gate.sh <qcow2> [baseline-keys-file]}"
BASELINE="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"   # no nested virt on runners

echo "== [gate] qcow2 integrity =="
qemu-img info --output=json "$QCOW" | python3 -c '
import json,sys
i=json.load(sys.stdin)
assert i["format"]=="qcow2", "format=%s, want qcow2" % i["format"]
print("  format=qcow2 virtual-size=%s" % i["virtual-size"])'
qemu-img check "$QCOW"

echo "== [gate] partition layout =="
# RHCOS: p1 bios, p2 EFI vfat, p3 boot ext4, p4 root xfs.
FS=$(guestfish --ro -a "$QCOW" run : list-filesystems)
echo "$FS" | sed 's/^/  /'
echo "$FS" | grep -q ': ext4' || { echo "[gate] FAIL: no ext4 boot partition"; exit 1; }
echo "$FS" | grep -q ': xfs'  || { echo "[gate] FAIL: no xfs root partition (OSTree root?)"; exit 1; }

echo "== [gate] extract BLS entries + grub.cfg =="
BOOTDEV=$(echo "$FS" | awk -F: '/ext4/{gsub(/ /,"",$1); print $1; exit}')
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
guestfish --ro -a "$QCOW" run \
  : mount "$BOOTDEV" / \
  : copy-out /loader/entries "$WORK" \
  : copy-out /grub2/grub.cfg "$WORK"

echo "== [gate] karg assertions =="
ARGS=("$WORK")
[ -n "$BASELINE" ] && [ -f "$BASELINE" ] && ARGS+=(--baseline-keys "$BASELINE")
python3 "$HERE/verify_kargs.py" "${ARGS[@]}"

echo "== [gate] PASS — image is structurally sound and fully re-stamped to aliyun =="
