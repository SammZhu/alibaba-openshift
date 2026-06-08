# RHCOS aliyun boot-image supply chain (P3-IMG.1)

Automates the unofficial aliyun-platform RHCOS image so each OCP/RHCOS bump is
handled by CI with a boot-free format gate — instead of a manual guestfish re-bake
that you only discover is broken when a node fails to boot.

**Design principle: store only the recipe, not the product.** The only durable
artifact is the few-KB provenance in `bootimage/provenance/`. The qcow2 is
re-derived on demand; OSS is transit-only (auto-expiring); the ECS image is
materialized when a version is actually deployed and deleted on teardown.

```
git (provenance + scripts)                     ← only durable thing; ~0 storage
   │ detect (GitHub-hosted): new RHCOS release?
   │ ── yes → bake job dispatched to self-hosted ──┐ outbound pull, zero inbound
   ▼                                                ▼
   provenance write-back (hosted)        internal RHEL runner (VPC, RAM role):
   + cosign sign                           re-bake → GATE → OSS(transit) → ImportImage → boot smoke
                                                │
                                                ▼ ECS image on-demand, deleted on teardown
```

## Pieces

| File | Role | Runs where | Offline-testable |
|---|---|---|---|
| `bootimage/version` | **the FLOOR** — the minimum supported OCP version. Committed (the runner has no local `all.yml`). NOT the list to bake; the set is derived at run time (floor→latest, minus provenance). | git | ✅ |
| `scripts/bootimage_detect.py` | resolve RHCOS via the installer `release-X.Y` stream — **no cluster/oc needed**. `--all-from` = matrix (enumerate floor→latest, skip what's in provenance, optionally `--ai-versions` AND); default = single (the floor line) | hosted / runner | ✅ |
| `scripts/ai_versions.py` | (optional, connected) fetch AI-supported versions from assisted-service `/openshift-versions` (offline token from `all.yml`) — feed to `detect --ai-versions` so the matrix only bakes minors a cluster can actually be **and records the precise z-stream** (#84) | runner | ✅ |
| `scripts/normalize_provenance.py` | rewrite any provenance `ocpVersion` that is a bare minor (`4.21`) to the highest GA z of that minor from the AI set (`4.21.x`) — so every entry is a precise, deployable version. One-off backfill + safety net | runner | ✅ |
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

## Status

Offline pieces done + unit-tested (detect, gate logic, provenance schema, workflow
skeleton). TODO (needs the runner + cloud): wire `10-prepare-worker-bootimage.yml`
to call `bootimage-gate.sh` between re-stamp and upload; implement OSS atime
lifecycle + on-demand ECS materialization; boot smoke; provenance write-back + cosign.
