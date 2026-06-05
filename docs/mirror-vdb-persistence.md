# P3-FIX.9 — Converge all mirror state onto vdb; snapshot vdb only

**Status:** design (pending review) · **Owner:** P3-FIX.9 · **Date:** 2026-06-05

## 1. Problem

The mirror snapshot fast-path (P3-COST.6 / #40) is supposed to let a torn-down
mirror be restored in ~2-3 min instead of re-running the ~50-min oc-mirror
import. In practice it **cannot restore a working Quay**, because Quay's
persistent state is split across two disks and only one of them is captured by
the snapshot.

### 1.1 Incident (2026-06-05)

A `site.yml` run reached Phase 04 on a snapshot-restored mirror and failed. The
log said "password drift", but the real situation was:

- `/var/lib/quay-storage/mirror-ready` marker present (vdb) ✓
- `/var/lib/quay-storage/datastorage` = **21 GB of image blobs** present (vdb) ✓
- **Quay not running**: `podman ps -a` empty, `podman images` empty, nothing on
  `:8443` (connection refused), no `quay-*` systemd units.

The 04 auth sentinel ran `podman login`, got **connection refused** (not 401),
and `failed_when: false` + `rc != 0` misreported it as a password drift. (That
sentinel + a Jinja-injection bug in its failure message were fixed separately;
this doc is about the underlying disk-layout cause.)

### 1.2 Root cause — state distribution

| Quay state | Path | Disk | In vdb snapshot? |
|---|---|---|---|
| Image blobs (`datastorage`) | `/var/lib/quay-storage/datastorage` | **vdb** | ✓ |
| Quay config + TLS | `/var/lib/quay-storage/quay-config` | **vdb** | ✓ |
| `mirror-ready` marker | `/var/lib/quay-storage/mirror-ready` | **vdb** | ✓ |
| **Postgres catalog DB** (podman volume `pg-storage`) | `/var/lib/containers/storage/volumes/pg-storage` | **vda** | ✗ |
| **Quay / Postgres / Redis container images** | `/var/lib/containers/storage` | **vda** | ✗ |
| systemd units (`quay-pod/app/postgres/redis`) | `/etc/systemd/system` | **vda** | ✗ |
| `mirror-registry` binary | `/opt/mirror-registry` | **vda** | ✗ |

The blobs live on vdb (we explicitly pass `--quayStorage
/var/lib/quay-storage/datastorage`), but **everything else** — most critically
the **Postgres catalog DB** that records which repos/tags/manifests exist —
lives in podman's default graphroot on **vda**.

The current fast-path tried to also preserve vda by baking a custom image from
a vda **system snapshot** (`MirrorOverrideImageId`). But Aliyun `CreateImage`
runs **sysprep** on the system disk, which strips the container runtime
(graphroot, volumes, units). So on restore: vdb's blobs survive, but the catalog
DB, the app images, and the units are gone → Quay can't start, and even if you
re-install it, it's an **empty registry pointing at 21 GB of orphaned blobs**.

The ROS template already documents this limitation
(`ros-templates/mirror-stack.yaml`, `MirrorOverrideImageId` description):

> "single-disk restore leaves Postgres metadata on vda out of sync with blobs
> on vdb."

This design removes the split entirely.

## 2. Goal

Make the **vdb data disk the single source of truth** for all Quay persistent
state. Then:

- **vda** is a plain stock RHEL system disk — no CreateImage, no sysprep, no
  system snapshot.
- **Only vdb is snapshotted** (one snapshot instead of two → ~half the snapshot
  storage cost and none of the sysprep fragility).
- On restore, the stock vda boots, cloud-init mounts the vdb snapshot,
  re-points podman's graphroot at vdb, and brings Quay up against the existing
  catalog + blobs — **no re-import**.

## 3. Design

### 3.1 Relocate podman graphroot onto vdb

Quay (via `mirror-registry`) runs as **rootful** podman containers. Move
podman's persistent storage (graphroot) from the vda default
(`/var/lib/containers/storage`) onto vdb:

```ini
# /etc/containers/storage.conf
[storage]
driver    = "overlay"
graphroot = "/var/lib/quay-storage/containers/storage"   # vdb (persistent)
runroot   = "/run/containers/storage"                     # tmpfs/vda (ephemeral, OK)
```

`graphroot` holds **both** the container images **and** named volumes — so
`pg-storage` (the catalog DB) moves to vdb with this one change. `runroot` is
runtime-only (locks, pid files) and is correctly left on the ephemeral root.

After this, the complete Quay state on vdb is:

```
/var/lib/quay-storage/
├── datastorage/            # image blobs (already here)
├── quay-config/            # config + TLS (already here)
├── containers/storage/     # NEW: app images + pg-storage volume (catalog DB)
├── mirror-ready            # marker
└── swapfile
```

### 3.2 Boot-time Quay bring-up

On a restored ECS the stock vda has no quay systemd units. Two options to start
Quay against the vdb-resident state:

- **Option A — re-run `mirror-registry install` on boot.** Idempotent against
  existing storage; recreates units and starts the pod. Reuses the existing
  `pg-storage` volume + blobs → **no re-import**. Needs `/opt/mirror-registry`
  present (stash it on vdb, see 3.3). ~2-3 min.
- **Option B (preferred) — pre-generated units on vdb.** At first install,
  `podman generate systemd --files --name quay-pod` (and members) into
  `/var/lib/quay-storage/systemd/`. On boot, cloud-init copies them to
  `/etc/systemd/system`, `daemon-reload`, `enable --now quay-pod`. No
  mirror-registry binary needed at restore; fastest and least magic.

**DECIDED: Option B** (user, 2026-06-05). It removes the dependency on the
installer binary and on `mirror-registry install` being idempotent against a
live DB (an unverified assumption). Option A is kept only as a documented
fallback.

Concrete mechanics of B: `mirror-registry install` already generates
reboot-safe systemd units (`quay-pod`, `quay-app`, `quay-postgres`,
`quay-redis`) in `/etc/systemd/system`. We do **not** regenerate them — we
**stash those units onto vdb** at install time
(`/var/lib/quay-storage/systemd/`). On restore, cloud-init writes
`storage.conf` (graphroot→vdb) first, then copies the stashed units back,
`daemon-reload`, and `enable --now quay-pod`. Because the graphroot (images +
`pg-storage` volume) is on the restored vdb and storage.conf points there, the
units start Quay against the existing catalog + blobs.

### 3.3 vda = stock system disk

- Drop `MirrorOverrideImageId` / the `CreateImage`-from-system-snapshot path in
  Phase 03. The mirror ECS always launches from the **stock RHEL image**.
- vdb is attached either blank (fresh) or `SnapshotId` (restore) — unchanged
  mechanism (`MirrorDataDiskSnapshotId`), this part already works.
- Stash anything from vda that restore needs **onto vdb** at install time:
  `/opt/mirror-registry` (if Option A), the generated unit files (Option B),
  and `/etc/containers/storage.conf` content (cloud-init rewrites it anyway).

## 4. Changes per file

| File | Change |
|---|---|
| `ros-templates/mirror-stack.yaml` | cloud-init: after mounting vdb, write `/etc/containers/storage.conf` (graphroot→vdb) **before** any podman use; on restore (existing XFS) run the Option-B unit install + `enable --now`; ensure ordering (vdb mount → storage.conf → quay). Remove `MirrorOverrideImageId` usage. |
| `playbooks/03-create-mirror-stack.yml` | Delete the `CreateImage` system-snapshot branch + `MirrorOverrideImageId` plumbing. Restore = stock image + `MirrorDataDiskSnapshotId` only. Keep the defensive `DescribeSnapshots` (data snapshot only). |
| `playbooks/04-prepare-mirror.yml` | Install with graphroot already on vdb. After successful import, `podman generate systemd` → stash units on vdb (Option B). (Health-gate fix from 2026-06-05 stays.) |
| `playbooks/05-verify-mirror.yml` | Snapshot **vdb only**. Drop the system-disk snapshot entirely. `mirror_snapshot_system_id` retires. |
| `playbooks/99-teardown.yml` | **No change required.** Already defensive: the snapshot delete loop guards on `item \| length > 0` so an empty `mirror_snapshot_system_id` is skipped; the `{cluster}-mirror-system` stale-sweep and the `mirror-system-from-snap` image delete simply no-op when those artifacts don't exist — and usefully *clean up* old-layout artifacts during migration. Left as-is. |
| `state.yml` | `mirror_snapshot_system_id` removed; only `mirror_snapshot_data_id` remains. |

## 5. Restore flow (target)

1. Phase 03 launches mirror ECS from **stock RHEL image**; vdb created from
   `MirrorDataDiskSnapshotId`.
2. cloud-init: mount vdb at `/var/lib/quay-storage` (existing XFS → no mkfs);
   write `storage.conf` (graphroot→vdb); install + `enable --now` the stashed
   quay units.
3. Quay starts against the existing `pg-storage` (catalog) + `datastorage`
   (blobs) → serving on `:8443` with the full mirrored catalog. **No import.**
4. Phase 04 health gate (marker ∧ `:8443` healthy) passes → skips heavy work.
5. 05 re-snapshots vdb (now self-contained).

## 6. Caveats to validate during implementation

1. **SELinux.** graphroot on a non-default path needs `container_file_t`
   (`semanage fcontext -a -t container_file_t '/var/lib/quay-storage/containers/storage(/.*)?'`
   + `restorecon -R`, or set in storage.conf + relabel). Without it containers
   fail to start with permission denials. The vdb XFS must also be mounted with
   a context that survives the snapshot (labels are stored in xattrs → captured
   by the snapshot, but verify after restore).
2. **Mount ordering.** vdb must be mounted and storage.conf written **before**
   podman/quay starts. Use fstab `x-systemd.before=` or sequence strictly in
   cloud-init `runcmd` (which is ordered) rather than a separate unit.
3. **`mirror-registry install` idempotency** (only if Option A): confirm it
   attaches the existing `pg-storage` volume rather than re-initialising the DB
   (which would lose the catalog). Option B sidesteps this.
4. **runroot on tmpfs**: fine, but ensure `/run/containers/storage` is recreated
   each boot (it is, being tmpfs).
5. **First-seed migration** (§7).

## 7. Migration (one-time)

Existing snapshots (`mirror_snapshot_system_id` + `mirror_snapshot_data_id`) are
from the old split layout and are **not** restorable under the new design. To
seed the new layout once:

1. Apply the code changes.
2. Full fresh mirror bring-up (01→05): graphroot now lands on vdb from the
   start; 05 takes the first **self-contained** vdb snapshot.
3. From then on, teardown→restore is fast-path and actually works.

Clear the stale `mirror_snapshot_system_id` from `state.yml` as part of the
cutover.

## 8. Cost / benefit

- **Snapshot storage:** one disk instead of two (no ~40 GB system snapshot) →
  roughly halves the standing snapshot cost.
- **Restore time:** ~2-3 min (boot + container start) vs ~50 min (re-import).
- **Robustness:** eliminates the entire sysprep/CreateImage failure class; the
  marker can no longer lie (Quay state and marker are on the same disk).
- **Simplicity:** removes the `CreateImage` + `MirrorOverrideImageId` machinery
  from Phase 03.

## 9. Open questions

- Option A vs B final pick — lean B; confirm `podman generate systemd` output is
  stable across the Quay 3.8.15 pod (pod + 4 member units).
- Does `mirror-registry` ever write absolute paths into the generated units that
  break when /opt is absent on restore? (Another reason to prefer B + stash.)
- Worth keeping a `--quayStorage` separate from graphroot, or collapse both
  under `/var/lib/quay-storage/containers`? Keeping `datastorage` separate
  preserves the existing blob path and avoids a data move on migration.
