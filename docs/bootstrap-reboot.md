# Bootstrap reboot on Alibaba ECS — design notes

This document captures one of the more counter-intuitive parts of installing
OpenShift on Alibaba Cloud: how the discovery ISO survives, then is
*destroyed*, on every node's first reboot — and why Phase 07 has to do
per-host recovery (`RebootInstance --ForceStop` vs `ReplaceSystemDisk`)
to keep AI's state machine happy.

If you ever hit "node hung mid-install, what do I do?" the answer is
in here.

## TL;DR

- Alibaba ECS has no virtual CD-ROM, so the Discovery ISO becomes the
  **content of `/dev/vda`** at boot, not a separate device.
- That makes `coreos-installer ... /dev/vda` impossible (vda is busy),
  so we install to `/dev/vdb` and clone vdb → vda at shutdown via the
  `clone-vdb-to-vda.service` ExecStop hook (injected by Phase 01 via
  ignition_config_override).
- The clone hook **only fires if shutdown reaches `shutdown.target`
  through normal systemd ordering**.  AI's "Rebooting" stage triggers
  the clone.  A `reboot -f` or aliyun `RebootInstance` bypasses it.
- After the clone runs, `/dev/vda` holds the half-installed RHCOS — a
  plain reboot lands on that broken layout with no agent and no path
  back to discovery.  Recovery requires `ReplaceSystemDisk` to re-image
  vda from the Phase 02 ECS image.
- Phase 07's reboot loop therefore reads each host's
  `AI host.progress.current_stage` and picks per-host:
  - `Rebooting` or later → **`ReplaceSystemDisk`** (vda was clobbered)
  - earlier            → **`StopInstance --ForceStop` + `StartInstance`**
    (vda still holds the live ISO, agent re-registers cleanly)

## Why no CD-ROM matters

On bare metal / vSphere / AWS the install flow is:

1. Boot from CD-ROM / removable media that exposes the Discovery ISO.
2. Discovery agent runs in RAM, registers with AI.
3. AI decides what to do; coreos-installer writes RHCOS to the *system
   disk* (a separate block device from the boot media).
4. On reboot the system firmware tries the CD-ROM, finds it empty / fails
   through, then boots the freshly-installed RHCOS from the system disk.

Alibaba ECS only offers persistent block devices.  When we
`ImportImage` from an OSS-uploaded ISO and boot an instance from it,
the ISO contents are written to `/dev/vda` and the kernel
auto-mounts the ISO9660 partition at `/run/media/iso`.  vda is
*busy* — `coreos-installer` cannot target it.

The workaround:

```
boot   →   ISO contents are on /dev/vda   (busy)
            attach a blank 100 GB cloud_essd as /dev/vdb
            coreos-installer  installs RHCOS to /dev/vdb
            ...AI flow continues...
shutdown → clone-vdb-to-vda.service ExecStop:
              dd  /dev/vdb → /dev/vda
              sgdisk -e /dev/vda     # relocate GPT backup if vda > vdb
reboot →   /dev/vda now holds RHCOS, boots normally
```

The hook lives in `ansible/files/clone-vdb-to-vda.{sh,service}`.  It is
injected unconditionally by Phase 01 — every Alibaba install needs it,
regardless of `mirror_enabled`.

## The shutdown ordering trap

The systemd unit pins itself ahead of `shutdown.target`:

```ini
DefaultDependencies=no
After=multi-user.target
Before=shutdown.target reboot.target halt.target poweroff.target
```

This is why the `dd` runs **as part of shutdown** rather than at boot.
For the clone to happen, the system has to walk through systemd's
shutdown sequence — which is what happens when:

- AI's coordinator issues a graceful reboot (the "Rebooting" stage),
- the user runs `systemctl reboot` or `shutdown -r` inside the node.

It does **not** happen when:

- the kernel panics,
- the agent crashes the box,
- somebody calls `aliyun ecs RebootInstance --ForceStop=true` (≈ power
  cycle from the hypervisor),
- somebody hits the "Force restart" button in the ECS console.

This is the root of the trap — see next section.

## Why mid-install force-reboot breaks AI

If the discovery agent gets wedged before AI's "Rebooting" stage (say,
waiting on a slow mirror pull or stuck on a validation), the natural
human reaction is to power-cycle the box to make it re-discover.  That
works **only** if vda still contains the live ISO.

Once AI reaches the Rebooting stage:

- The clone hook has fired.
- vda now contains a half-finished RHCOS layout (no NVRAM entry, no
  bootloader pointing at it, but real partitions and content).
- A power cycle reboots from vda, which now boots a broken RHCOS that
  has no discovery agent, no AI registration, and no path back.
- AI marks the host stuck — no recovery without re-imaging vda.

Empirically:

```
host stage in {known, discovering, insufficient, pending-for-input,
                preparing-for-installation, installing,
                installing-in-progress, writing-image-to-disk}
                                       → vda intact   → power-cycle OK
host stage in {rebooting, configuring, joined, installed, …}
                                       → vda clobbered → ReplaceSystemDisk
```

(See `advanced` set in `ansible/playbooks/07-install-cluster.yml`.)

## What Phase 07's reboot loop does

When `nodes_need_reboot=true` (set by an earlier reset, or when a prior
install attempt left hosts wedged), 07 builds a per-host **reboot
plan**:

1. Pull every host's `progress.current_stage` from AI.
2. For each cluster-stack ECS (excluding jump host + mirror ECS):
   - stage in the "advanced" set → method = `ReplaceSystemDisk`
   - otherwise                    → method = `RebootForce`
3. For `ReplaceSystemDisk` hosts:
   ```
   aliyun ecs StopInstance     --ForceStop=true     # sync
   wait for Stopped (poll, ~30–60 s)
   aliyun ecs ReplaceSystemDisk --ImageId <Phase 02 image>
   aliyun ecs StartInstance                          # Aliyun does NOT auto-start
                                                     # (some docs claim it does)
   ```
4. For `RebootForce` hosts:
   ```
   aliyun ecs RebootInstance --ForceStop=true        # power cycle
   ```
   vda still holds the ISO, so the box boots back into discovery and the
   agent re-registers with AI within ~2 min.

This logic is the reason `ecs_image_id` must still be present in
`state.yml` at Phase 07 time even though the original Phase 02 image was
"used" at Phase 06 stack-create.  An assert in 07 fails fast with a
clear message if it's missing.

## When can you skip ReplaceSystemDisk?

- Cluster install completed successfully (no hung hosts) → 07's reboot
  loop is skipped entirely.
- All hung hosts are pre-Rebooting → only `RebootForce`.  This is the
  common case for "validation never passed, agent is sitting on the
  discovery ISO" failures.
- Any host is past Rebooting → that one needs `ReplaceSystemDisk`, and
  the rest still go through `RebootForce`.  07 mixes the two correctly.

## Related landmines

- **Snapshot recovery preserves vda content.**  If a host crashes after
  the clone, restoring its vda snapshot (or `ReplaceSystemDisk` from a
  *post-clone* image) just resurrects the broken install.  Always
  re-image vda from the **discovery** image (the one Phase 02 produced),
  not from any post-install snapshot.
- **The clone hook is one-shot per ISO boot.**  The new RHCOS rootfs on
  vda doesn't carry the hook — so on the second boot (post-install)
  there's no risk of vda getting overwritten by stale vdb content.
- **vdb is sized 100 GB minimum.**  AI's "Disk is too small … 100 GB
  required" check is against vdb (the install target), not vda.  See
  cluster-stack.yaml `InstallDiskSize` default.

## See also

- `ansible/files/clone-vdb-to-vda.sh` — the clone script with detailed
  inline comments (block size choice, dd timeout strategy, GPT relocation).
- `ansible/files/clone-vdb-to-vda.service` — the systemd unit with the
  ordering rationale.
- `ansible/playbooks/01-prepare-iso.yml` — where the hook is injected
  into the discovery ISO via `infra-env ignition_config_override`.
- `ansible/playbooks/07-install-cluster.yml` `Build per-instance reboot
  plan` task — the implementation of the per-host recovery logic.
