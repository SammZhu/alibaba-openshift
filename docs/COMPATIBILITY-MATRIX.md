# Compatibility matrix & fork contingency (P6-DIST.7 / #81)

Running OpenShift on Alibaba Cloud stitches together **four independently-versioned
parties** — OCP/Kubernetes (the base), CAPI core (the framework), and the three
integration components CAPA / CCM / CSI (see [COMPONENTS-AND-ORDER.md](COMPONENTS-AND-ORDER.md)).
None of them shares a release train, and **nobody upstream owns the combined
matrix** — so it is owned *here*. This page is the contract: the verified baseline,
each party's version constraint and its provenance, the misfit risks, and the
**fork contingency** for the pieces we depend on but do not control (chiefly CCM).

> Architecture risk register, item #4 (v3.4): *"four-party version compat,
> unmaintained: CCM v2.14 vs k8s 1.33; three components upgrade independently …
> CCM upstream is not ours to control (could stall / CVE / break on new OCP)."*
> This document is the mitigation.

---

## 1. Verified baseline (the one combination proven on a live cluster)

Everything below is pinned in the repo and was exercised end-to-end on a real
cluster (Route B worker join, v2.8 — `node/iz0jlbqw… Ready worker v1.33.11`).

| Party | Pinned version | Pinned where |
|---|---|---|
| **OpenShift** | **4.20.22** | `ansible/group_vars/all.yml(.example)` → `openshift_version` |
| **Kubernetes** (derived) | **1.33** (`OCP 4.N → k8s 1.(N+13)`); workers seen as `v1.33.11` | follows OCP |
| **RHCOS** | **9.6** (aliyun boot image, self-baked — Phase 10) | `10-prepare-worker-bootimage.yml` |
| **CAPI core** | **v1.12.7** | `11-capa-routeb-join.yml` / `09-capa-smoke.yml` → `capi_core_version` |
| **CAPA** (this project) | image **v0.1.12** (HEAD `82e0af8`); built against cluster-api lib `v1.12.7`, controller-runtime `v0.22.5`, client-go **1.34** (`k8s.io/* → v0.34.3` via go.mod replace) | `openshift-capi-alicloud` `go.mod`; deployed by `02-capa-controller.yaml` |
| **CCM** | **v2.14.0** (`…/acs/cloud-controller-manager:v2.14.0`) | `custom_manifests/01-alibaba-ccm.yaml.j2` |
| **CSI** | **v1.35.3** (alibaba-cloud-csi-operator) | `04-csi-*.yaml`, `08-deploy-post-install.yml`, `04-prepare-mirror.yml` |

**This exact row is the only combination we assert as known-good.** Any deviation
falls back to the per-party constraints in §2 — which are looser and, for CCM,
unverified.

---

## 2. Per-party constraint & provenance

The question that matters for each party: *what Kubernetes (hence OCP) range does
it support, and how solid is that claim?*

| Party (version) | Supported k8s range | Our k8s 1.33 lands… | Provenance | Confidence |
|---|---|---|---|---|
| **CAPI core v1.12** | mgmt **1.31–1.35**, workload **1.29–1.35** | inside both ✓ | upstream version-support table | **High** (published, exact) |
| **CAPA v0.1.12** | client-go 1.34 + controller-runtime 0.22 ⇒ effective **1.33–1.35** (client-go skews ±1 minor vs apiserver) | at the **lower edge** (1.34 client on a 1.33 apiserver — within skew) ✓ | our `go.mod` + Kubernetes client skew policy | **Med-High** (library-derived) |
| **CCM v2.14.0** | upstream states only `k8s > 1.7.2` (>1.19 for ALB ingress); **no published upper bound per release** | not contradicted, but **not asserted** by upstream | upstream README/getting-started; release notes don't pin a k8s range | **Low** — only *our* live test on 1.33 backs it |
| **CSI v1.35.3** | decoupled from the kube minor via the stable CSI interface; the **external sidecars** (provisioner/attacher/resizer/snapshotter) carry the real k8s floor | works on 1.33 ✓ | CSI spec stability + our deploy; sidecar versions bundled in the operator | **Med** (interface-stable, sidecars to watch) |
| **RHCOS 9.6** | bound 1:1 to the OCP release | n/a (tracks OCP) | OCP release | **High**, but **self-baked aliyun image** is a separate maintenance burden (risk #5) |

**Reading the table:** the framework (CAPI) and the base (OCP/RHCOS) have hard,
published constraints and we sit comfortably inside them. CAPA is derived from its
build libraries and sits at the lower edge of the client-skew window. **CCM is the
soft spot** — upstream publishes no per-release k8s ceiling, so "v2.14 works on
1.33" rests entirely on our own live test, not an upstream guarantee.

---

## 3. Misfit risks (what breaks when a version moves)

1. **CCM is "depended-on but not controlled."** Upstream `cloud-provider-alibaba-cloud`
   sets its own cadence and ships no k8s compatibility matrix. A new OCP (newer
   k8s) can remove an API the CCM still calls, or upstream can stall / ship a CVE
   with no fix. Because *every* node comes up `uninitialized`-tainted until CCM
   clears it, a broken CCM = an unusable cluster. → **fork contingency, §4.**

2. **CAPA at the client-skew edge.** CAPA is built against client-go 1.34 and runs
   on a 1.33 apiserver — fine (skew is ±1). But if OCP is *downgraded* / pinned to
   1.32 or older, CAPA falls **outside** the skew window. Keep CAPA's build libs
   within one minor of the target cluster; bump them when raising the OCP floor.

3. **No single upgrade lever.** The four parties upgrade independently, so raising
   OCP is a *fan-out*: new k8s must stay inside CAPI core's window (§2), CAPA's
   libs must stay within skew, and CCM/CSI must have a release that runs on the new
   k8s — and the latter two are not on our schedule. Treat an OCP bump as "re-verify
   the whole row in §1," not a one-line change. → **upgrade checklist, §5.**

4. **CSI sidecar floor.** The driver itself is interface-stable, but its external
   sidecars have a minimum k8s. On a big OCP jump, confirm the operator's bundled
   sidecar versions still satisfy the new floor.

5. **Self-baked RHCOS aliyun image.** OCP ≥4.15 ships no aliyun boot image, so we
   guestfish one per RHCOS version (Phase 10). Every OCP/RHCOS bump re-bakes it;
   this is a standing cost, tracked as risk #5, not solved here.

---

## 4. Fork contingency — for the pieces we don't control

**Default posture: do not fork.** Forking adds permanent drift and maintenance, so
the steady state is to consume CCM as the upstream manifest and CSI/CAPI core as
released artifacts ([COMPONENTS-AND-ORDER.md §5](COMPONENTS-AND-ORDER.md)). The fork
is a **contingency**, entered only on a concrete trigger and exited as soon as the
trigger clears.

### 4.1 CCM (the primary case)

**Triggers (any one):**
- upstream stalls — no release for a new OCP/k8s we must ship on;
- a CVE in the running CCM with no upstream fix in a usable timeframe;
- a new OCP/k8s breaks CCM at runtime (removed/renamed API; CCM pod crashloops or
  nodes never lose the `uninitialized` taint).

**Contingency steps:**
1. Fork `kubernetes/cloud-provider-alibaba-cloud` at the last-good tag (v2.14.0).
2. Apply the **minimal** patch only: bump `k8s.io/*` client libs to the target
   minor, fix removed-API call sites, address the CVE. Resist feature drift — the
   goal is "runs on the new k8s," nothing more.
3. Build + push to a registry we control (same Quay org as the CSI images), and
   **mirror it** (add to `04-prepare-mirror.yml` + the IDMS map, like the CSI/CAPA
   images) so air-gapped installs resolve it by digest.
4. Repoint the image reference in `custom_manifests/01-alibaba-ccm.yaml.j2`
   (`registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager:v2.14.0`)
   to the fork tag, and update §1 of this page.
5. **Exit criterion:** when upstream ships a release that runs on the target k8s,
   discard the fork and repoint back to the upstream image. The fork is a bridge,
   not a destination.

**Cost to weigh before pulling this lever:** you now own SLB reconciliation and
Node-init correctness on Alibaba — the highest-blast-radius component (a bug here
takes down node usability cluster-wide). Budget for it; prefer pushing upstream
first if the timeline allows.

### 4.2 CSI

Lower urgency: a broken CSI only blocks PVC-backed workloads, not the cluster. We
**already ship CSI as our own mirrored operator** (`quay.io/samzhu/alibaba-cloud-csi-operator`),
so "fork" here means bumping that operator/driver + sidecars and re-mirroring —
the muscle already exists (the P3-CSI mirror pipeline). Same exit criterion.

### 4.3 CAPI core

We consume only the released `cluster-api-components.yaml` (CRDs, and core
controllers when fully installed). A fork is a last resort; the realistic levers
are (a) stay on a CAPI minor whose support window covers the target k8s (§2), or
(b) for the OCP-managed core, defer to cluster-capi-operator (see the coexistence
gate, #79 / P3-CAPA.29). Forking CAPI core would be a strategic move (upstreaming
the Alibaba provider, route 2), not a contingency patch.

---

## 5. Upgrade checklist (run before raising any pinned version)

Raising OCP is the common case and the riskiest, because it drags k8s with it.
Before bumping `openshift_version`:

- [ ] **k8s ↔ CAPI core:** new k8s minor still inside CAPI v1.12's window (mgmt
      1.31–1.35 / workload 1.29–1.35)? If not, bump CAPI core first and re-check.
- [ ] **k8s ↔ CAPA:** target k8s within ±1 of CAPA's client-go (currently 1.34)?
      If the new k8s ≥ 1.36, bump CAPA's `go.mod` libs + controller-runtime and
      re-release.
- [ ] **k8s ↔ CCM:** is there a CCM release validated (by us, on a live cluster)
      on the new k8s? If upstream is silent and our test fails → §4.1 fork.
- [ ] **k8s ↔ CSI:** operator's bundled sidecars satisfy the new k8s floor?
- [ ] **RHCOS:** re-bake the aliyun boot image for the new RHCOS (Phase 10).
- [ ] **Re-verify the §1 row** end-to-end (Route B join smoke), then update §1's
      versions and the "verified" note with the new date/cluster.

---

## 6. Maintenance

This matrix is part of the distribution contract — **it must be updated whenever
any pinned version in §1 changes.** A version bump that doesn't update this page is
incomplete. When CAPA and CSI are both OLM operators in one catalog, some of these
constraints can move into OLM dependency declarations; until then, this page is the
single source of truth for "what versions go together."

Owner: this project (the integrator). Upstream owns each component; we own the
combination.

---

## 7. Bumping the CAPA image version (single source of truth)

The CAPA controller image tag has **one source of truth**: `ansible/vars/images.yml`
(`capa_image_tag`). The mirror (`04`), deploy (`08`), smoke (`09`) and Route B (`11`)
playbooks pull it via `vars_files` as `{{ capa_image }}` — bump the tag in that one
file and the whole chain follows. Do **not** hardcode the tag in a playbook or
manifest: it drifted to v0.1.2 in four places once and shipped a crash-looping
controller while Route B was on v0.1.11 and smoke on v0.1.4.

Two spots live outside ansible's var loading and are kept in sync by hand (both
carry a comment pointing here): `scripts/build-mirror-tarball.sh`
(`OPENSHIFT_CAPI_IMAGE` default) and `custom_manifests/02-capa-controller.yaml`
(the static `image:` fallback that `08` sed-overrides at deploy time).

**Auto-sync from the provider CI.** The provider repo (`openshift-capi-alicloud`)
CI builds + pushes the image on a `v*` tag, then a `sync-deploy-tag` job rewrites
`capa_image_tag` here and commits to `main` — so a plain `git pull` tracks the
latest provider build, no hand-editing. Requires a repo secret `ANSIBLE_REPO_TOKEN`
(a token with `contents:write` on this repo); without it the job no-ops.

**Updating the image on a running cluster** (no rebuild needed):
1. provider tags `v0.1.X` → CI bumps `capa_image_tag` → `git pull` here.
2. `ansible-playbook -i inventory.yml playbooks/04-prepare-mirror.yml`
   — idempotently pulls+pushes the new tag into the air-gapped mirror (only when
   MISSING; existing tags are skipped). Needs the mirror ECS up (Phase 03, or a
   fast-path snapshot restore).
3. `ansible-playbook -i inventory.yml playbooks/08-deploy-post-install.yml`
   — rolls the controller to the new tag.

Phases 01–03 / 06–07 stay untouched. Only a from-scratch build runs `04` as part
of the full `site.yml` chain.
