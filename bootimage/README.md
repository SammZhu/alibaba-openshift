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
