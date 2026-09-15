# RHCOS aliyun boot-image supply chain (P3-IMG.1)

Automates the unofficial aliyun-platform RHCOS image so each OCP/RHCOS bump is
handled by CI with a boot-free format gate — instead of a manual guestfish re-bake
that you only discover is broken when a node fails to boot.

**Design principle: store only the recipe, not the product.** The only durable
artifact is the few-KB provenance in `bootimage/provenance/`. The qcow2 is
re-derived on demand; the ECS image is materialized **by the normal install flow**
when a version is actually deployed, and deleted on teardown.

**Two halves, one re-stamp.** The supply chain is an *offline CI pre-flight*: it
proves a version's re-stamp is format-correct (gate) and records provenance. The
*cloud materialization* (OSS upload → ImportImage → ECS image) is **not** a separate
pipeline — it is exactly what the install flow already does in
`ansible/playbooks/10-prepare-worker-bootimage.yml` (standalone mode) for Route B
workers. Both halves run the **same re-stamp task**; they only differ in where they
stop. So #83 builds no separate cloud code — it reuses the install flow.

```
supply chain (offline CI)              install flow (at deploy time, playbook 10)
detect → re-stamp → GATE               re-stamp (cluster's current RHCOS) → GATE
       → provenance write-back                → OSS → ImportImage → ECS image → state
  proves "this version's re-stamp           materializes on demand; deleted on teardown.
   is format-correct" + records it          cluster == source of truth == your all.yml version
```

## Pieces

| File | Role | Runs where | Offline-testable |
|---|---|---|---|
| `bootimage/version` | **the FLOOR** — the minimum supported OCP version. Committed (the runner has no local `all.yml`). NOT the list to bake; the set is derived at run time (floor→latest, minus provenance). | git | ✅ |
| `scripts/bootimage_detect.py` | resolve RHCOS via the installer `release-X.Y` stream — **no cluster/oc needed**. `--all-from` = matrix (enumerate floor→latest, skip what's in provenance, optionally `--ai-versions` AND); default = single (the floor line) | hosted / runner | ✅ |
| `scripts/ai_versions.py` | (optional, connected) fetch AI-supported versions from assisted-service `/openshift-versions` (offline token from `all.yml`) — feed to `detect --ai-versions` so the matrix only bakes minors a cluster can actually be **and records the precise z-stream** (#84) | runner | ✅ |
| `scripts/normalize_provenance.py` | refresh each provenance `ocpVersion` to the latest GA z of its minor from the AI set (`4.21` → `4.21.12` → later `4.21.13` …) **only while** `release-X.Y` still points at that entry's `rhcosVersion` (else it's a historical image — left untouched, no drift). Idempotent; backfill now + schedule for ongoing refresh | runner | ✅ |
| `ansible/playbooks/10-prepare-worker-bootimage.yml` | the bake (guestfish re-stamp + OSS + ImportImage) | runner (VPC) | — |
| `scripts/bootimage-gate.sh` | **offline format gate**: qemu-img check + partition layout + extract + karg assertions, BEFORE any upload | runner | partial (needs an image) |
| `scripts/verify_kargs.py` (+ `_test.py`) | pure karg-assertion logic: all `ignition.platform.id=aliyun`, no residual, completeness, cross-version diff guard | anywhere | ✅ (unit-tested) |
| `bootimage/provenance/<rhcos>.yaml` | the recipe: source url+sha256, transform, tooling, `kargsBaseline` | git | ✅ |
| `.github/workflows/rhcos-aliyun-bootimage.yml` | detect / bake / provenance jobs | both | yaml |

## The offline gate (why this de-risks #5)

`bootimage-gate.sh <qcow2> [baseline-keys]` turns "silent broken image → cluster
boot failure (hours, burns a cluster)" into "loud CI failure at bake time (minutes,
free)". It asserts:

- every BLS entry + `grub.cfg` carries `ignition.platform.id=aliyun`, none residual
  (`metal/openstack/qemu/...`);
- a platform id exists at all (its absence = upstream changed the mechanism);
- qcow2 integrity (`qemu-img check`) + the expected ext4-boot / xfs-root layout;
- **diff guard**: the karg KEY set vs the previous version's `kargsBaseline` — a
  drift means RHCOS changed its kernel-arg scheme; the gate fails loud as an early
  warning before you ship a subtly-wrong image.

100% proof still needs the low-frequency **boot smoke** (launch one ECS, confirm
ignition runs as `aliyun` + kubelet joins) before a version is blessed.

### Why `bootSmoke` is still `pending` everywhere

Not because the smoke never happens — it happens on every install.  Phase 10
re-stamps the image, phase 12 boots CAPA workers from it, and they join Ready.
That *is* the boot smoke.

It cannot be written back because the two halves resolve RHCOS from different
places:

| | resolves RHCOS from |
| --- | --- |
| this supply chain | `openshift/installer`, branch `release-X.Y` — the **branch HEAD** |
| phase 10 (what a cluster boots) | the cluster's own `coreos-bootimages` — its **pinned payload** |

The branch head moves; a deployment stays on its patch.  Measured 2026-09-14: a
4.20.22 cluster boots RHCOS `9.6.20260217-1`, and provenance holds
`9.6.20260512-0`, `-20260520-0`, `-20260616-0`, `-20260815-0`, `-20260818-0` and
`10.2.20260715-0` — **no overlap at all**.  So there is no entry to flip.

What the deployed image does and does not get:

| | |
| --- | --- |
| absolute format checks (platform id, qcow2 integrity, layout) | **yes** — phase 10 runs the same `bootimage-gate.sh` before upload, and a failure blocks it |
| karg-drift diff guard | **no** — phase 10 calls the gate without a baseline, so the comparison against the previous version never runs |
| provenance entry | **no** — hence nowhere to record `bootSmoke` |

So the deployed image is format-checked; what it lacks is the early warning for
a changed kernel-arg scheme, and any record that it was checked at all.

### How the gap is closed

Not by baking twice in CI.  The deploy path already has everything: it resolves
the version, downloads the qcow, re-stamps it and runs the gate.  What it lacked
was a baseline and a record.  Phase 10 now:

1. picks the newest provenance entry **of the same RHCOS generation** and feeds
   its karg keys to the gate, so the diff guard runs on the deployed image too.
   Same generation matters — 9.x and 10.x are different RHEL bases whose karg
   sets may legitimately differ, and a cross-generation comparison would fail a
   good image and block an install;
2. writes a provenance entry for that version when none exists, with the gate
   result in it — the same `write_provenance.py` CI uses.

It never overwrites an existing entry (that one is the supply chain's, and may
already carry a `bootSmoke` result) and it never commits: pushing to the repo is
an outward action, so the file is written and the commit left to a person.

So a deployed version now ends up in `bootimage/provenance/` with
`gate: passed`, and **once phase 12's workers reach Ready that is its boot
smoke** — set `bootSmoke: passed` in that file and commit.  There is finally
something to record it on.

CI keeps baking the branch head, which is what makes it an early warning for a
new RHCOS; the deploy path covers what is actually deployed.  Neither replaces
the other.

## The self-hosted runner (internal RHEL, zero inbound)

The bake runs on a runner registered on the internal RHEL/ECS. The runner agent
holds an **outbound** HTTPS long-poll to GitHub; GitHub never connects inbound, so
no firewall hole. Register (private repo only):

```
# on the internal RHEL (in the Alibaba VPC, attached to a RAM role):
./config.sh --url https://github.com/<org>/alibaba-openshift \
  --token <JIT-token> --labels self-hosted,rhel8,alibaba-vpc --ephemeral
./run.sh         # or install as a systemd service
```

- **RAM role** on the runner ECS supplies cloud creds → no AK/SK in GitHub or on disk.
- Use `--ephemeral` (just-in-time) so the runner deregisters after one job.
- Only outbound is needed: `github.com:443` (honors `HTTPS_PROXY`), the RHCOS
  source (prefer the internal mirror), and the Alibaba OSS/ECS **internal** endpoints.
- **Security**: private repo, no fork-PR execution on self-hosted, pin to this workflow.

## Where the RHCOS stream comes from (and how 4.22 went missing)

Detection reads the installer's own stream metadata from
`openshift/installer`, branch `release-X.Y`, under `data/data/coreos/`.  The
filename is not stable:

| | |
| --- | --- |
| 4.21 and earlier | `rhcos.json` |
| 4.22 and later | `coreos-rhel-9.json` + `coreos-rhel-10.json` |

4.22 ships both, and **the rhel-9 one still declares `stream: rhcos-4.21`** —
it is the upgrade path.  So the file is chosen by matching `stream` to the
branch, not by filename order; for 4.22 that selects `coreos-rhel-10.json`,
RHCOS 10.2.x, **a RHEL 10 base this pipeline has never baked**.  Watch the
first 4.22 run rather than treating it as routine.

A minor whose files exist but where **none** declares `rhcos-<branch>` is
treated as not released yet — `release-4.23` is exactly that today: both files
are still on `rhcos-4.22`, and the rhel-10 one is at an *older* RHCOS than 4.22
itself.  Falling back to "whatever file is there" would bake 4.23 from a
4.22-era image with both sides reporting success.

This is how 4.22 stayed invisible for months: detection asked for `rhcos.json`,
got a genuine 404, read it as "this minor has no branch yet" and stopped — so
4.22 **and everything above it** disappeared while the job reported
`nothing to bake` and exited 0.  If upstream renames these files again the
detector now raises `StreamFileMissing` (the directory and its `OWNERS` are
there, the filenames are not) instead of silently truncating the scan.

## Choosing a version to deploy (operator)

`bootimage/provenance/` is the **menu**: every entry is a baked, gate-passed image
for a version that satisfies both AI (`--ai-versions` intersect) and CAPA (the
re-stamp + gate). `ocpVersion` is the precise, deployable z-stream and
`rhcosVersion` is the exact boot image behind it.

1. Look at `bootimage/provenance/*.yaml`, pick an `ocpVersion` (e.g. `4.20.22`).
2. Put that in the operator's local `ansible/group_vars/all.yml` →
   `openshift_version`. AI installs that z-stream; CAPA workers boot the matching
   `rhcosVersion` aliyun image — no drift, because the recorded z is the one whose
   RHCOS == the baked image.

`bootimage/version` (the committed FLOOR) only bounds what the matrix bakes; it is
**not** the deploy version — the deploy version comes from this menu.

The menu stays current automatically: the scheduled workflow runs
`normalize_provenance.py` after the bake, so when a newer z of a minor ships on the
*same* RHCOS (e.g. `4.21.12` → `4.21.13`), the recorded `ocpVersion` follows; when
it ships a *new* RHCOS, the matrix bakes a fresh entry and the old one stays pinned.

## Status

Offline supply chain done + production-validated on the self-hosted runner: detect
(matrix ∩ AI), re-stamp, gate, provenance write-back + auto-refresh, scheduled
workflow. The gate is now wired into the install path too
(`10-prepare-worker-bootimage.yml` standalone runs `bootimage-gate.sh` before
upload).

**Cloud materialization = the install flow, not a separate pipeline.** OSS upload +
ImportImage + ECS image are already done by `10-prepare-worker-bootimage.yml`
standalone at deploy time (and torn down with the cluster) — #83 reuses it, so the
earlier "OSS atime lifecycle + on-demand materialization" pipeline is dropped.

Remaining (optional): boot smoke write-back (a real Route B worker join flips
provenance `bootSmoke: pending → passed`); cosign-sign provenance.
