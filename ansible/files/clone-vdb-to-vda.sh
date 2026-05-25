#!/bin/bash
# Clone /dev/vdb → /dev/vda before reboot.
#
# Triggered by clone-vdb-to-vda.service's ExecStop during systemd shutdown.
# Runs ONCE per discovery-agent lifetime — the service exists only in the
# live RHCOS rootfs (RAM), so after the post-install reboot the new RHCOS
# on /dev/vda doesn't carry this hook and won't re-clone.
#
# Why this exists: Alibaba ECS has no virtual CD-ROM.  The Discovery ISO
# becomes the system disk's content (/dev/vda + vda1 auto-mounted at
# /run/media/iso), so coreos-installer can't install over /dev/vda
# (busy partitions).  We install to /dev/vdb instead and clone here.
set -euo pipefail

# Guard 1: no vdb attached → noop
[ -b /dev/vdb ] || { echo "no /dev/vdb, skipping"; exit 0; }

# Guard 2: vdb has no partition table (still empty/blank install target)
sfdisk -d /dev/vdb >/dev/null 2>&1 || { echo "vdb has no partition table, skipping"; exit 0; }

# Find last used sector across all vdb partitions (start+size-1 max).
# sfdisk -d emits one line per partition like:
#   /dev/vdb1 : start=        2048, size=     2095104, type=...
LAST_SECTOR=$(sfdisk -d /dev/vdb \
  | awk -F'[=,]' '/start=/{end=$3+$5-1; if(end>m)m=end} END{print m+0}')

[ "${LAST_SECTOR:-0}" -gt 0 ] || { echo "could not determine end sector"; exit 0; }

# Round byte count up to bs alignment so the last partition's tail bytes
# don't get truncated.
BS=$((512 * 256))         # 128 KB block size — fast on ESSD
BYTE_COUNT=$(( (LAST_SECTOR + 1) * 512 ))
COUNT=$(( (BYTE_COUNT + BS - 1) / BS ))
MB=$(( COUNT * BS / 1024 / 1024 ))

echo "Cloning first ${MB} MB of /dev/vdb → /dev/vda (last partition end sector ${LAST_SECTOR})"
sync

# Hard cap: 5 min is ~10x the expected ~25 sec for 5 GB on ESSD.  If dd
# isn't done by then, the underlying IO is wedged — fail fast so systemd
# doesn't block reboot for the full ExecStop timeout.  Without --foreground
# `timeout` won't actually deliver SIGTERM in time on busy hosts; pair
# with --kill-after to guarantee SIGKILL 30 s after that.
#
# dd's status=progress prints throughput once per second to the journal +
# serial console, so live progress is visible even though we don't poll
# from this script.
timeout --foreground --kill-after=30s 5m \
  dd if=/dev/vdb of=/dev/vda bs="$BS" count="$COUNT" \
     conv=fsync,notrunc status=progress
RC=$?
sync

if [ "$RC" -ne 0 ]; then
  echo "dd failed or timed out (rc=$RC) — reboot will proceed but vda likely incomplete"
  echo "Investigate via aliyun ECS console or boot the next attempt fresh"
  exit "$RC"
fi

# vda may be larger than vdb (or equal).  If larger, GPT backup header
# inherited from vdb points at the wrong sector for vda's actual end.
# sgdisk -e relocates the backup to vda's real end-of-disk.
sgdisk -e /dev/vda 2>/dev/null || true

echo "Clone done — next boot will load RHCOS from /dev/vda"
