# CAPA day-2 worker operations

Operations reference for the declarative, self-healing CAPA worker lifecycle on
OpenShift-on-Alibaba (platform=external / day-2). Covers scale, rolling update,
drain, self-healing (MHC), delete-safety, IMDS hardening, the externally-managed
control plane, and the air-gap image strategy — with the operator procedure, the
pass criteria, and the gotchas found in testing.

Companions:
- [CAPA-MULTI-AZ.md](CAPA-MULTI-AZ.md) — the multi-AZ pool model / design (one
  MachineDeployment per AZ).
- [CAPA-WORKER-JOIN.md](CAPA-WORKER-JOIN.md) — Route B boot (aliyun-platform RHCOS
  + user-data Ignition).
- [MIRROR.md](MIRROR.md) / [SNAPSHOT.md](SNAPSHOT.md) — disconnected mirror.
- [COST.md](COST.md) / [TEARDOWN.md](TEARDOWN.md).

Conventions used below: `oc` runs on the jump host (`KUBECONFIG=/root/kubeconfig`,
namespace `default`); cloud checks use the `aliyun` CLI. The worker cluster name
is `caworkers`; pools are `caworkers-<az-key>` (one MachineDeployment per AZ).

---

## 0. Model in one paragraph

The OCP control plane (masters) is installed out-of-band (Assisted Installer +
ROS). CAPI core + the CAPA provider run on that cluster and manage **workers**
declaratively: one `MachineDeployment` per AZ pinned by `failureDomain`, a shared
`AlibabaCloudMachineTemplate` (zone/vSwitch resolved per-Machine from the failure
domain), a `MachineHealthCheck` for self-healing, and an `AlibabaCloudControlPlane`
(mode=external) that adopts the existing masters so CAPI core marks the Cluster's
`ControlPlaneInitialized=True`. Without that control-plane object, worker
Machines never reach `readyReplicas` even though the nodes are Ready (CAPI gates
worker node-health on `ControlPlaneInitialized`).

---

## 0.1 Resource ownership — who creates whom

Three systems interlock. We own only the CAPA infra-provider layer; MachineSet is
created by **CAPI core**, and MachineConfig belongs to the **OpenShift MCO** — we
consume/integrate with both, we don't re-design them.

```
                       ┌──────────────────────────────────────────────────┐
   OCP control plane:  │  masters installed out-of-band (Assisted + ROS)   │  not ours
                       └───────────────────────┬──────────────────────────┘
                                               │ adopted by
   OURS (CAPA CRDs/controllers + orchestration)▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ Cluster ──controlPlaneRef──▶ AlibabaCloudControlPlane (mode=external)  │ ours: CRD+ctrl,
   │   │                          → reports ControlPlaneInitialized=True    │ status-only, no cloud
   │   └──infrastructureRef──▶ AlibabaCloudCluster (region/failureDomains)  │ ours
   │ MachineDeployment (one per AZ) ──▶ AlibabaCloudMachineTemplate         │ ours
   │ MachineHealthCheck                                                     │ ours
   └───────────┬──────────────────────────────────────────────────┬───────┘
               │ CAPI core: MachineDeployment ctrl creates          │ MHC ctrl
               ▼                                                    │ remediates
        ┌─────────────┐  ← created by CAPI CORE (not us)            │
        │  MachineSet  │                                            │
        └──────┬───────┘                                            │
               ▼ MachineSet ctrl creates                            │
        ┌─────────────┐  ← CAPI core                                │
        │   Machine    │◀────────────────────────────────────────────┘
        └──────┬───────┘   nodeRef bound by core (exact providerID match, #78)
               ▼ creates infra machine from the template
        ┌──────────────────────┐  ← OURS (CRD + controller)
        │  AlibabaCloudMachine  │  RunInstances on create · frees ECS + sweeps tag on
        └──────┬────────────────┘  delete (G8) · IMDS hardening after nodeRef (G14)
               ▼
        ┌──────────────────────┐      ┌──────── OCP / Alibaba, existing — NOT ours ────────┐
        │  ECS (RHCOS, booted   │ join │ CCM: writes Node.providerID, clears uninitialized  │
        │  via user-data        │─────▶│   taint, zone labels                               │
        │  Ignition, Route B)   │      │ MCS: serves the worker pointer Ignition            │
        └──────────────────────┘      │ MCO: renders MachineConfig / registries.conf       │
                                       │   (IDMS/ITMS → why applying an ITMS rolls workers) │
                                       └────────────────────────────────────────────────────┘
```

Ownership in one line:
- **Ours** = the `AlibabaCloud*` CRDs/controllers (Cluster / Machine / MachineTemplate
  / ControlPlane) + CSR auto-approval + the orchestration manifests (MachineDeployment,
  MachineHealthCheck, the Cluster ref wiring).
- **CAPI core** (upstream) = MachineDeployment → **MachineSet** → Machine → nodeRef, and
  the MHC remediation logic.
- **OpenShift** (existing) = the control plane, **MachineConfig/MCO**, the CCM, the MCS.

Note the two distinct MachineSets: the **CAPI** `cluster.x-k8s.io` MachineSet (used
here) vs the **OCP MachineAPI** `machine.openshift.io` MachineSet (a different system
we do NOT use; the legacy `config/` machineset.openshift.io CRD is dead CCCMO-era code).

## 1. Externally-managed control plane (prerequisite)

`AlibabaCloudControlPlane` (apiGroup `controlplane.cluster.x-k8s.io`, mode=external)
is the Cluster's `controlPlaneRef`. It owns no cloud resources — it simply reports
the pre-existing OCP control plane as initialized.

Verify:
```
oc -n default get alibabacloudcontrolplane caworkers \
  -o jsonpath='{.status.initialization.controlPlaneInitialized} {.status.externalManagedControlPlane} {.status.ready}{"\n"}'
oc -n default get cluster caworkers \
  -o jsonpath='{.status.conditions[?(@.type=="ControlPlaneInitialized")].status}{"\n"}'
```
Pass: ACP `initialized=true externalManaged=true ready=true`; Cluster
`ControlPlaneInitialized=True`. (If false, worker MachineDeployments stay at
`readyReplicas=0` regardless of node health.)

---

## 2. Scale (up / down, cross-AZ)

Each pool scales independently; replicas land in the pool's pinned AZ.
```
oc -n default scale machinedeployment caworkers-b --replicas=2   # up
oc -n default scale machinedeployment caworkers-b --replicas=1   # down
```
Watch:
```
oc -n default get machinedeployment -l cluster.x-k8s.io/cluster-name=caworkers \
  -o custom-columns=NAME:.metadata.name,REPL:.spec.replicas,READY:.status.readyReplicas,FD:.spec.template.spec.failureDomain
oc -n default get alibabacloudmachine \
  -o jsonpath='{range .items[*]}{.metadata.name} zone={.spec.zoneID} id={.status.instanceID}{"\n"}{end}'
```
Pass: scale-up — new Machine `failureDomain` == the pool's AZ, ECS created there,
node joins Ready, pool converges. Scale-down — MachineSet deletes a Machine, the
controller releases its ECS (see §5), no orphan, pool converges.

Scale-from-zero is supported (`--replicas=0` empties the pool and frees all its
ECS; `--replicas=1` rebuilds from zero).

---

## 3. Rolling update

CAPI rolls a MachineDeployment only when **`spec.template`** changes — editing the
referenced `AlibabaCloudMachineTemplate` in place does NOT trigger a rollout.
Strategy defaults to `RollingUpdate maxSurge=1 maxUnavailable=0` (surge a new
replica → Ready → drain+delete an old one → repeat), so the AZ never goes empty.

To change instance config (e.g. instanceType), create a NEW template and repoint:
```
# 1) pick an instanceType available in the pool's AZ (avoid the "sold out in AZ" trap)
aliyun ecs DescribeAvailableResource --RegionId <region> \
  --DestinationResource InstanceType --ZoneId <pool-az> --InstanceChargeType PostPaid

# 2) create caworkers-worker-v2 = current template with the new instanceType, then:
oc -n default patch machinedeployment caworkers-a --type=merge \
  -p '{"spec":{"template":{"spec":{"infrastructureRef":{"name":"caworkers-worker-v2"}}}}}'
```
A label-only bump (`spec.template.metadata.labels`) also forces a rollout without
changing instance config — useful to exercise the rollout machinery alone.

Watch `oc get machineset` (a new MachineSet appears, old scales to 0 then is
removed) and confirm the new Machines carry the new `instanceType` and all old
ECS are released.

---

## 4. Self-healing (MachineHealthCheck, #69)

The MHC remediates a worker whose Node goes unreachable: it sets `OwnerRemediated`,
the MachineSet deletes the Machine, the controller frees the ECS, and a replacement
is rebuilt (in another AZ if the original AZ is out of capacity).

> ⚠️ **CAPI v1beta2 field layout.** The MHC MUST use the v1beta2 schema. The old
> v1beta1 top-level fields (`maxUnhealthy`, `nodeStartupTimeout`,
> `unhealthyConditions`) were RELOCATED under `spec.checks` and `spec.remediation`.
> Writing the old names under `apiVersion cluster.x-k8s.io/v1beta2` makes the API
> **silently prune** them (unknown fields) — the MHC then has no unhealthy
> conditions and never remediates. Correct layout:
> ```yaml
> spec:
>   checks:
>     nodeStartupTimeoutSeconds: 1200
>     unhealthyNodeConditions:
>       - {type: Ready, status: "False",  timeoutSeconds: 300}
>       - {type: Ready, status: Unknown, timeoutSeconds: 300}
>   remediation:
>     triggerIf: {unhealthyLessThanOrEqualTo: 40%}
> ```
> Verify it stuck (not pruned): `oc -n default get machinehealthcheck caworkers-mhc -o yaml`
> should show `checks.unhealthyNodeConditions`; `status.remediationsAllowed`
> reflects the cap (e.g. 40% of 4 → 1).

Fault-injection test (cloud-side stop simulates node failure):
```
aliyun ecs StopInstance --RegionId <region> --InstanceId <i-...> --ForceStop true
```
Chain: node NotReady → `Ready=Unknown` → after the 300s timeout MHC remediates →
Machine deleted → controller deletes the (Stopped) ECS → MachineSet rebuilds →
new node Ready → pool back to desired, `status.currentHealthy` restored.

---

## 5. Delete-safety: no orphan ECS (G8)

When a Machine is deleted (scale-down, rolling, MHC, teardown), `reconcileDelete`
must free the billable ECS before dropping its finalizer. The controller resolves
the instance from the most durable signal — `Status.InstanceID`, else
`Spec.ProviderID` (`alicloud://<region>.<id>`) — and, before releasing the
finalizer on a "gone" describe, sweeps by the per-machine tag
(`k8s.io/cluster-api-machine=<name>`) to catch a tagged orphan whose status write
was lost. `createInstance` also persists the resolved region onto `Spec.RegionID`
so the region is always recoverable on delete. (Provider v0.1.17 + v0.1.18.)

Audit after any delete-heavy operation:
```
# tagged worker ECS should equal the live worker count, none Stopped/orphaned:
aliyun ecs DescribeInstances --RegionId <region> \
  --Tag.1.Key openshift-worker-pool --Tag.1.Value capa-multiaz
# no detached disks (DeleteWithInstance=true):
aliyun ecs DescribeDisks --RegionId <region> --Status Available
```
Pass: tagged ECS count == live workers, all Running; `Available` disks = 0.

Note: `oc delete machine <name>` blocks until cordon+drain+ECS-delete+node-removal
finish (the terminal "hangs" — that is the drain completing, not a failure).

---

## 6. IMDS post-boot hardening (G14)

Workers MUST boot with tokenless IMDS (`httpTokens: optional`) — RHCOS Ignition
fetches user-data without an IMDSv2 token (a `required` instance returns 403 and
the node never joins). Leaving the node on IMDSv1 forever is an SSRF/metadata
weakness, so the controller flips it to IMDSv2 **after the node has joined**.

Set both on the template:
```yaml
metadataOptions: {httpEndpoint: enabled, httpTokens: optional, httpTokensAfterBoot: required}
```
`httpTokensAfterBoot` is opt-in (empty = no post-boot change). The controller
applies it only once the owning Machine has a `nodeRef` — the signal that
Ignition's tokenless fetch is done — so the flip can't lock out an in-flight boot.
`status.metadataHardened` guards it to one call. (Provider v0.1.19.)

Verify (after a worker joins):
```
oc -n default get alibabacloudmachine <name> -o jsonpath='{.status.metadataHardened}{"\n"}'
aliyun ecs DescribeInstances --RegionId <region> --InstanceIds '["<i-...>"]' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["Instances"]["Instance"][0]["MetadataOptions"])'
```
Pass: `metadataHardened=true`, the instance reports `HttpTokens=required`, AND the
node stays Ready (confirm the Alibaba CCM / cloud-node-manager / kubelet still read
IMDS via the token flow — if anything depended on tokenless IMDS it would break
here; that is the key check for this feature).

---

## 7. Air-gap image strategy (digest pin + IDMS + ITMS)

A restricted cluster pulls everything from the mirror. The install-config emits
only `imageDigestSources` → an **ImageDigestMirrorSet (IDMS)**, which redirects
pulls **by digest only**. Manifests that pin images by **tag** are therefore NOT
redirected and escape to public quay.io — invisible on a node that already cached
the image, but a stuck `ImagePullBackOff` (and needless PayByTraffic egress) on any
node a pod is rescheduled to (scale / rolling / drain / MHC). Two layers fix this:

1. **Digest-pin** the CAPA controller image in `08-deploy-post-install` (resolve the
   mirror digest via `oc image info`, sed the manifest to `quay.io/...@sha256:...`),
   mirroring what `08a-capi-core` does for the CAPI core image. The IDMS then
   covers it directly and the reference is immutable.
2. **ImageTagMirrorSet (ITMS)** `samzhu-tag-mirror` (applied in
   `08-deploy-post-install`) redirects tag pulls for the `samzhu/*` repos — the
   backstop for CSI (OLM catalog/operator) and any remaining tag pull.

Both are gated on the mirror being enabled (no-op on a connected cluster). Note:
applying/changing an IDMS or ITMS is rendered into `registries.conf` by the MCO and
triggers a rolling node config update (worker reboots).

Check:
```
oc get imagedigestmirrorset image-digest-mirror -o yaml   # digest rules (samzhu/*)
oc get imagetagmirrorset samzhu-tag-mirror -o yaml        # tag rules (samzhu/*)
oc -n capa-system get deploy capa-controller-manager -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```
Pass: the CAPA image is an `@sha256:...` digest (air-gap) or a mirror-redirected
tag; no node logs `dial ... quay.io ... i/o timeout`; capa HA = 2/2.

---

## 8. Quick verification script

`scripts/verify-capi-pools.sh` (read-only, run on the jump host) bundles the
contract-label / failureDomains / control-plane / pool / cross-AZ / providerID→
nodeRef / MHC checks in one shot — see section 1c for the control-plane chain.

---

## 9. Known traps (all fixed; here as a checklist)

- **MHC pruned** — v1beta1 field names under v1beta2 apiVersion → MHC never
  remediates (§4).
- **Tag image escapes mirror** — IDMS is digest-only; tag-pinned images dial public
  quay.io on uncached nodes (§7).
- **Orphan ECS on delete** — delete path must not key solely off the (least durable)
  `Status.InstanceID` (§5).
- **IMDS flip timing** — harden only after `nodeRef`, never at ECS-Running, or you
  brick Ignition (§6).
- **CRD field pruned** — any new CRD field must be regenerated INTO the cluster CRD
  (`custom_manifests/02-capa-crds.yaml` from `config/crd/bases`) or the API prunes
  it silently (same class as the MHC trap).
- **ISO false-stale rebuild** — `01-prepare-iso` uses an `infra_env_id` sidecar, not
  a binary grep, to decide freshness (avoids a needless ~15-30 min ISO/image
  rebuild). See [MIRROR.md](MIRROR.md).
- **AZ sold out** — pick the rolling/scale instanceType via
  `DescribeAvailableResource` for the target AZ.

> Cost: multi-replica (e.g. 50) stress is intentionally NOT run; a few replicas
> across a/b/c prove the logic. See [COST.md](COST.md) and [TEARDOWN.md](TEARDOWN.md).
