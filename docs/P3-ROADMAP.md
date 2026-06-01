# P3 路线图 — 从 smoke-test 到 production-grade

Planning doc for the P3 phase of the project.  After P0 (cluster
install), P1 (CAPA smoke test), P2 (CAPI core + RunInstances real ECS),
P3 brings the integration to production-grade — both on the **CAPA
controller** side (lifecycle correctness, CAPI contract compliance,
admission validation, test coverage) and the **CSI driver** side
(mirror pipeline, disk/NAS/snapshot E2E, OADP backup integration).

For the gap analysis behind these tasks see the design doc v2.5 §6
"待完成事项" and the conversation transcript from 2026-06-01.

---

## 1. Status snapshot

| Phase | Coverage | What works |
|:-|:-|:-|
| P0 — install | ✅ | cluster `installed`, 34 cluster operators Available |
| P1 — CAPA smoke | ✅ | CAPA pod Running, reconciler stops at OwnerRef gate |
| P2 — CAPI core | ✅ | CRDs-only path; CAPA calls ecs.RunInstances, real ECS boots |
| **P3 — production** | 🔄 | this doc |

The single end-to-end win from P2 (verified 2026-06-01): ECS
`i-0jl0ucoda565o2pq16wj` (cn-wulanchabu-a, Running, with CAPI tags
`kubernetes.io/cluster/smoke-test=owned` +
`k8s.io/cluster-api-machine=smoke-machine`).

---

## 2. Two parallel tracks

P3 has two completely independent tracks:

| Track | Owner | Repo touched | Risk |
|:-|:-|:-|:-|
| **P3-CAPA** (8 tasks) | controller dev | `SammZhu/openshift-capi-alicloud` + manifest in `alibaba-openshift` | Medium — Go code, contract semantics |
| **P3-CSI** (4 tasks) | infra/operator dev | `alibaba-openshift` (mirror pipeline + manifests) + verify on cluster | Low-Med — mostly mirror plumbing + E2E validation |

No cross-track dependencies.  Can be done by one person serially or
by two in parallel.

---

## 3. P3-CAPA — Production-grade CAPA controller

### Goal
Take CAPA from "smoke-test passes" to "manages a real OCP worker pool":
- Single-machine lifecycle (create + status + delete + adoption) compliant with
  [CAPI infra-machine contract](https://cluster-api.sigs.k8s.io/developer/providers/contracts/infra-machine.html)
- Cluster reconciliation compliant with
  [CAPI infra-cluster contract](https://cluster-api.sigs.k8s.io/developer/providers/contracts/infra-cluster.html)
- Admission webhook prevents bad spec
- ≥70% test coverage on controllers

### Task breakdown

| Task | Title | File:line ref | Size |
|:-:|:-|:-|:-:|
| #23 | providerID format + region resolution | `internal/controller/alibabacloudmachine_controller.go:338`<br>`:213-226` (regionFromMachine)<br>`:320` (createInstance) | S |
| #24 | Bootstrap data from `machine.Spec.Bootstrap.DataSecretName` | `:377-396` (getUserData) | S |
| #25 | controlPlaneEndpoint spec→status + BYO validation | `internal/controller/alibabacloudcluster_controller.go:147-152`<br>`:209-215` (reconcileSLB) | S |
| #26 | deleteInstance wait-for-terminated | `internal/controller/alibabacloudmachine_controller.go:345-355`<br>`:190-210` (reconcileDelete) | M |
| #27 | FailureReason / FailureMessage on terminal errors | `:248`, `:259` | S |
| #28 | Paused annotation handling | both reconcilers top-of-Reconcile | XS |
| #29 | Admission webhook — spec validation + defaulting | new `internal/webhook/*` | L |
| #30 | envtest integration suite + 70% coverage | `internal/controller/*_test.go` | L |

### Phasing (4 PRs)

| PR | Tasks | Story | Effort |
|:-:|:-:|:-|:-:|
| **PR1** | #23 #24 #25 #28 | "Controller is CAPI-contract compliant on the happy path" | ~1 week |
| **PR2** | #26 #27 | "Delete is graceful; terminal failures surface to MachineHealthCheck" | ~1 week |
| **PR3** | #29 | "Admission rejects garbage before reconciler sees it" | ~2 weeks |
| **PR4** | #30 | "Test suite locks the behavior" | ~2 weeks |

Total ~6 weeks single-person time.

### Why this order
PR1 alone is the highest-leverage step: after PR1, CAPA can be paired
with a standard CAPI bootstrap provider (KubeadmConfig — or our future
Ignition-aware one).  That unlocks the next phase (P4: bootstrap
provider for OCP).  PR2/PR3/PR4 are safety + quality, doable
incrementally.

### Out of scope for P3-CAPA (deferred to P4+)
- Full VPC/SLB provisioning (we're BYO-via-ROS by design; stubs stay)
- MachineDeployment + MachineSet rolling update logic
- AlibabaCloudMachineTemplate full plumbing
- **Ignition-aware bootstrap provider** — biggest single piece of P4
  work; see §5 below
- Cluster Autoscaler integration
- Conversion webhooks (v1beta1↔v1beta2)
- Cosign / SBOM / multi-arch image signing

---

## 4. P3-CSI — Storage E2E

### Goal
Make persistent storage real on the cluster:
- Disk CSI (cloud_essd / cloud_efficiency) E2E: PVC bind → mount →
  expand → snapshot → restore → delete
- NAS CSI (RWX) E2E with focus on OpenShift Virtualization live-migration
- VolumeSnapshotClass + CDI StorageProfile + OADP backup-to-OSS

### Task breakdown

| Task | Title | Key files | Size |
|:-:|:-|:-|:-:|
| #31 | 3 CSI images → mirror pipeline | `scripts/build-mirror-tarball.sh` (new [2d/8]–[2f/8]),<br>`ansible/playbooks/04-prepare-mirror.yml` (3 new pre-pull blocks + IDMS rules) | M |
| #32 | Disk CSI E2E smoke + `docs/CSI-SMOKE.md` | OLM chain → SC → PVC → mount → expand → snapshot → restore | M |
| #33 | NAS RWX + live-migration verify | `custom_manifests/04-csi-driver-cr.yaml` (add `nas` block), CNV install | L |
| #34 | VolumeSnapshotClass + CDI StorageProfile + OADP-to-OSS | new manifests + 08 playbook OADP wiring | L |

### Phasing

Each task is its own delivery; do them serially.

| Day | Task | Outcome |
|:-:|:-|:-|
| 1 | #31 | Mirror tarball + IDMS knows about CSI; old mirrors auto-recover via 04 pre-pull |
| 2-3 | #32 | Cluster has real block storage; `oc create pvc` works |
| 4-7 | #33 | VM disks on NAS, live-migration possible |
| 8-12 | #34 | Backup → OSS → restore round-trip works |

### Existing CSI assets (already in repo, no new work)
- `custom_manifests/04-csi-catalogsource.yaml`
- `custom_manifests/04-csi-operatorgroup.yaml`
- `custom_manifests/04-csi-subscription.yaml`
- `custom_manifests/04-csi-driver-cr.yaml`
- `docs/csi-driver-design.md` v0.4 (deep design)
- 3 OLM images public on quay (`v1.35.3`):
  - `samzhu/alibaba-cloud-csi-operator`
  - `samzhu/alibaba-cloud-csi-operator-bundle`
  - `samzhu/alibaba-cloud-csi-operator-catalog`

---

## 5. P4 preview — the bootstrap provider problem

The biggest open question for "real CAPI worker pool on OCP" is:
**OCP nodes boot from Ignition; upstream CAPI's default bootstrap
provider (KubeadmConfig) emits kubeadm cloud-init — not Ignition.**

Two paths, neither cheap:

| Option | Cost | Pro | Con |
|:-|:-|:-|:-|
| A. Fork `cluster-api-bootstrap-provider-kubeadm` → emit Ignition | 2-3 weeks | Stay close to upstream; reuse all of kubeadm's metadata wiring | OCP nodes don't actually run kubeadm — we'd just be using its CR shape, not its semantics. Awkward. |
| B. New `cluster-api-bootstrap-provider-machine-config-server` | 3-4 weeks | Clean fit — listens for `Machine` + cluster-name → curls Ignition from OCP MCS → puts it in Secret with key `value` | New project; have to write CRD + reconciler + RBAC + e2e |

Recommended: **B**.  It's cleaner architecturally and OCP-native.
Tracked separately; not in P3 scope.

---

## 6. Combined timeline view

```
            W1      W2      W3      W4      W5      W6
        ────┬──────┬──────┬──────┬──────┬──────┬───────
  CAPA      │ PR1  │ PR2  │ PR3  │ PR3  │ PR4  │ PR4  │
  track     │ #23  │ #26  │ #29  │ #29  │ #30  │ #30  │
            │ #24  │ #27  │      │      │      │      │
            │ #25  │      │      │      │      │      │
            │ #28  │      │      │      │      │      │
        ────┼──────┼──────┼──────┼──────┼──────┼──────│
  CSI       │ #31  │ #32  │ #33  │ #33  │ #34  │ #34  │
  track     │mirror│ disk │ NAS  │ NAS  │ snap+│ OADP │
            │      │ smoke│      │      │ CDI  │      │
        ────┴──────┴──────┴──────┴──────┴──────┴──────┘
```

Single-person serial: ~12 weeks.
Two-person parallel: ~6 weeks.

---

## 7. Definition of done (P3 exit criteria)

P3 is "done" when:

1. **P3-CAPA**: A fresh cluster + minimal `Cluster` + `Machine` +
   `KubeadmConfig` (no manual ownerRef patch, no manual finalizer
   strip) results in a real ECS that boots, joins, and can be deleted
   gracefully — entirely via standard CAPI flow.
2. **P3-CSI**: A fresh cluster has working block + RWX storage; a VM
   can live-migrate; a backup can round-trip through OSS.
3. CI green on both repos.  Test coverage ≥70% on CAPA controllers.
4. Docs current: `docs/CAPI-CORE.md` and `docs/CSI-SMOKE.md` reflect
   the new happy paths; design doc rolled to v2.6+.
5. No outstanding "stub" / "not yet implemented" code paths on the
   happy path (stubs remaining only for explicitly-deferred features
   like VPC/SLB provisioning, which we've BYO'd from ROS by design).

P4 (Ignition bootstrap provider + MachineDeployment) begins when both
P3 tracks are merged.

---

## 8. Version history

| Date | Note |
|:-|:-|
| 2026-06-02 | Initial — written at the start of P3.  Tasks #23-#34 created.  Design doc v2.5 referenced as the gap-analysis source. |
