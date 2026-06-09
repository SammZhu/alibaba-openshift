# Cluster API Core — CRDs-only Path for CAPA E2E

Operational reference for verifying that the Cluster API Provider for
Alibaba Cloud (CAPA) controller can actually call `ecs.RunInstances`
and create a real ECS — by installing **only** the upstream Cluster
API core CRDs and crafting the OwnerRefs by hand, without running the
full `capi-controller-manager` Deployment.

Distilled from the P2-CAPI verification run on 2026-06-01.

For context see:
- [README.md](../README.md) — top-level overview
- [CAPA-SMOKE.md](CAPA-SMOKE.md) — Phase 1 (controller deploy + CR smoke
  test up to the OwnerRef gate; this doc picks up where that one stopped)
- [E2E-RUNBOOK.md](E2E-RUNBOOK.md) — cluster install

---

## 1. Why CRDs-only and not the full CAPI manager

CAPA (the infrastructure provider) gates on two things from upstream
Cluster API core:

1. A `cluster.x-k8s.io/v1beta2 Machine` whose `infrastructureRef`
   points at our `AlibabaCloudMachine` — and whose UID becomes the
   `ownerReferences[0]` on that AlibabaCloudMachine.  This is the
   "Machine Controller has not yet set OwnerRef" gate
   (`alibabacloudmachine_controller.go:70`).
2. A `cluster.x-k8s.io/v1beta2 Cluster` whose `infrastructureRef`
   points at our `AlibabaCloudCluster` — and whose UID becomes the
   ownerReferences[0] on AlibabaCloudCluster.  Without this,
   AlibabaCloudCluster reconciles with `ClusterInfrastructureNotReady`.

The full upstream CAPI deployment is what normally sets those
OwnerRefs.  But the full deployment is ~3 MB of YAML, brings in
`capi-controller-manager` + webhook server + cert-manager dependency
+ ~17 CRDs, and is orthogonal to the OpenShift install flow.

**Shortcut:** install the CRDs only (`oc apply` 17 CRDs), then craft
the Cluster + Machine objects yourself and manually patch the
`ownerReferences` onto the AlibabaCloud* objects to point at them.
This satisfies the gates without spinning up a CAPI controller.

This shortcut is appropriate for:
- Smoke-testing the CAPA controller code path end-to-end.
- One-shot verification that `ecs.RunInstances` works.

It is **not** appropriate for production multi-machine management —
you'd want the real `capi-controller-manager` for Machine lifecycle,
MachineSet/MachineDeployment, draining, etc.

---

## 2. Prerequisites

- CAPA controller deployed and `1/1 Running` per
  [CAPA-SMOKE.md](CAPA-SMOKE.md), with `alibaba-creds` Secret in
  `capa-system` namespace (see §5.4 below for how to create it).
- CAPA image is `:v0.1.2` or newer.  `:v0.1.1` panics-or-loops on
  credentials (see §5.2).
- jumphost has `oc` + kubeconfig at `/root/kubeconfig`.

---

## 3. Step-by-step (verified 2026-06-01)

### 3.1 Download the CAPI v1.12.7 component bundle

```bash
# On any box with internet:
curl -L \
  -o cluster-api-components.yaml \
  https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.12.7/cluster-api-components.yaml
wc -l cluster-api-components.yaml   # ~53 500 lines
```

### 3.2 Extract only the 17 CRDs

```bash
# Split on document separators, keep only the CRD objects
awk '
  /^---$/ { p=0 }
  /^kind: CustomResourceDefinition$/ { p=1; print "---" }
  p==1 { print }
' cluster-api-components.yaml > capi-crds-only.yaml
grep -c '^kind: CustomResourceDefinition' capi-crds-only.yaml   # → 17
```

### 3.3 Ship to jumphost + apply

```bash
scp -i SSHKEY capi-crds-only.yaml root@$JUMP:/root/

# On jumphost:
oc --kubeconfig=/root/kubeconfig apply --server-side=true --force-conflicts \
  -f /root/capi-crds-only.yaml
```

The `--force-conflicts` is **required**: OpenShift 4.20 ships its own
copies of three IPAM CRDs (`ipaddresses.ipam.cluster.x-k8s.io`,
`ipaddressclaims.ipam.cluster.x-k8s.io`,
`ipaddressclaims.ipam.cluster.x-k8s.io`).  The cluster-version-operator
owns the schema fields on those, and a plain server-side apply hits:

```
Apply failed with 3 conflicts: conflicts with "cluster-version-operator"
```

`--force-conflicts` makes our apply take ownership.  See §5.3.

### 3.4 Craft a Cluster + Machine pair

The smoke pair is intentionally minimal — no bootstrap config, no
KubeadmControlPlane, no MachineDeployment.  Just enough to satisfy the
OwnerRef gates.

```yaml
# /root/capi-core-smoke.yaml on jumphost
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: Cluster
metadata:
  name: smoke-cluster
  namespace: default
spec:
  infrastructureRef:
    # NOTE: v1beta2 uses apiGroup, NOT apiVersion (see §5.1)
    apiGroup: infrastructure.cluster.x-k8s.io
    kind: AlibabaCloudCluster
    name: smoke-test
---
apiVersion: cluster.x-k8s.io/v1beta2
kind: Machine
metadata:
  name: smoke-machine
  namespace: default
  labels:
    cluster.x-k8s.io/cluster-name: smoke-cluster
spec:
  clusterName: smoke-cluster
  infrastructureRef:
    apiGroup: infrastructure.cluster.x-k8s.io
    kind: AlibabaCloudMachine
    name: smoke-test-worker
  bootstrap:
    # No bootstrap controller — supply a literal empty data secret name
    # ref so the validation passes.  CAPA never reads this field.
    dataSecretName: smoke-bootstrap-data
```

```bash
oc --kubeconfig=/root/kubeconfig apply -f /root/capi-core-smoke.yaml
```

### 3.5 Manually patch OwnerRefs onto the AlibabaCloud* objects

The real `capi-controller-manager` would do this; we mimic it by hand.

```bash
# Get the Cluster's UID
CLUSTER_UID=$(oc --kubeconfig=/root/kubeconfig -n default get cluster smoke-cluster -o jsonpath='{.metadata.uid}')

# Get the Machine's UID
MACHINE_UID=$(oc --kubeconfig=/root/kubeconfig -n default get machine smoke-machine -o jsonpath='{.metadata.uid}')

# Patch AlibabaCloudCluster ownerRef → Cluster
oc --kubeconfig=/root/kubeconfig -n default patch alibabacloudcluster smoke-test --type=merge -p "{\"metadata\":{\"ownerReferences\":[{\"apiVersion\":\"cluster.x-k8s.io/v1beta2\",\"kind\":\"Cluster\",\"name\":\"smoke-cluster\",\"uid\":\"$CLUSTER_UID\",\"controller\":true,\"blockOwnerDeletion\":true}]}}"

# Patch AlibabaCloudMachine ownerRef → Machine
oc --kubeconfig=/root/kubeconfig -n default patch alibabacloudmachine smoke-test-worker --type=merge -p "{\"metadata\":{\"ownerReferences\":[{\"apiVersion\":\"cluster.x-k8s.io/v1beta2\",\"kind\":\"Machine\",\"name\":\"smoke-machine\",\"uid\":\"$MACHINE_UID\",\"controller\":true,\"blockOwnerDeletion\":true}]}}"
```

### 3.6 Observe RunInstances

Watch the CAPA logs — within a few seconds of the second patch you
should see the cluster reconcile succeed, then the machine reconcile
fire and call RunInstances:

```bash
oc --kubeconfig=/root/kubeconfig -n capa-system logs deploy/capa-controller-manager --tail=50 -f
```

Expected lines (in order):

```
"alibaba: using AccessKey credential from environment"     # V(2), may not appear at default verbosity
"AlibabaCloudCluster reconciled successfully" reconcileID=...
"VPC creation not yet implemented, skipping"               # see §5.5
"SLB creation not yet implemented, skipping"
"AlibabaCloudCluster is not ready yet, requeueing"         # transient — gone next reconcile
# ... a few seconds later ...
"Created ECS instance i-..."
```

### 3.7 Verify the ECS in Aliyun

```bash
INSTANCE_ID=$(oc --kubeconfig=/root/kubeconfig -n default get alibabacloudmachine smoke-test-worker -o jsonpath='{.status.instanceID}')
echo "Instance: $INSTANCE_ID"

# From operator box:
aliyun ecs DescribeInstances --RegionId cn-wulanchabu --InstanceIds "[\"$INSTANCE_ID\"]" \
  | jq '.Instances.Instance[] | {Id:.InstanceId, Status, Type:.InstanceType, Image:.ImageId, Created:.CreationTime, PrivIP:.VpcAttributes.PrivateIpAddress.IpAddress, Tags:.Tags.Tag}'
```

The instance should be `Running` within a couple of minutes, with
the CAPI tags applied:
- `kubernetes.io/cluster/<cluster-name>=owned`
- `k8s.io/cluster-api-machine=<machine-name>`

### 3.8 Cleanup

The cleanest way to remove the smoke ECS is to delete the
AlibabaCloudMachine — CAPA's finalizer will stop + delete the ECS.
Then delete the Cluster/Machine + the AlibabaCloudCluster.

```bash
oc --kubeconfig=/root/kubeconfig -n default delete alibabacloudmachine smoke-test-worker
oc --kubeconfig=/root/kubeconfig -n default delete machine smoke-machine
oc --kubeconfig=/root/kubeconfig -n default delete alibabacloudcluster smoke-test
oc --kubeconfig=/root/kubeconfig -n default delete cluster smoke-cluster
```

---

## 4. End-to-end success criteria (P2-CAPI.a, 2026-06-01)

| Check | Expected | Verified |
|---|---|---|
| 17 CAPI CRDs installed | `oc get crds | grep cluster.x-k8s.io | wc -l` → 17+ | ✓ |
| Cluster + Machine CR created without webhook | only CRDs installed, no validating webhook → passes | ✓ |
| OwnerRef gate on AlibabaCloudMachine | log shows `alibabacloudmachine_controller.go:70` no longer hit | ✓ |
| Credentials resolved | reconciler does NOT return `UnsupportedCredential` | ✓ |
| AlibabaCloudCluster Ready=True | `.status.ready=true, reason=AlibabaCloudClusterReady` | ✓ |
| RunInstances call | AlibabaCloudMachine `.status.instanceID` populated | ✓ (`i-0jl0ucoda565o2pq16wj`) |
| Real ECS running in Aliyun | `aliyun ecs DescribeInstances` → `Status=Running` | ✓ |
| Tags applied per CAPI convention | `kubernetes.io/cluster/smoke-test=owned` + `k8s.io/cluster-api-machine=smoke-machine` | ✓ |

---

## 5. Known gotchas (2026-06-01)

### 5.1 v1beta2 uses `apiGroup`, not `apiVersion`, in infrastructureRef

The CAPI v1beta2 `Cluster.spec.infrastructureRef` schema uses
`apiGroup` (without version).  Earlier alphas used `apiVersion`.
Submitting an `apiVersion` produces a validation error like:

```
Cluster.cluster.x-k8s.io "smoke-cluster" is invalid:
  spec.infrastructureRef.apiVersion: Unsupported value
```

Same applies to `Machine.spec.infrastructureRef` and
`Machine.spec.bootstrap.configRef`.

### 5.2 CAPA versions before v0.1.2 won't reach RunInstances

- **`:v0.1.0`** — `pkg/version/version.go` calls `semver.MustParse` on
  the build-time `git describe --tags --always --dirty` value, which
  panics on a bare SHA.  Pod CrashLoopBackOff.  Fixed in v0.1.1.
- **`:v0.1.1`** — `pkg/client/capi.go:63` passes `nil` credential to
  `ecs.NewClientWithOptions`.  The aliyun Go SDK does **not**
  auto-discover env vars on `nil`; it returns
  `[SDK.UnsupportedCredential] type = nil` on every reconcile.
  Env-var injection on the Pod is necessary but not sufficient — the
  CAPA code itself must build a credential.  Fixed in v0.1.2: see
  [openshift-capi-alicloud commit `bb1f73e`](https://github.com/SammZhu/openshift-capi-alicloud/commit/bb1f73e).
- **`:v0.1.2`** — explicit `resolveCredential()` reads
  `ALIBABA_CLOUD_ACCESS_KEY_{ID,SECRET}` (or the older
  `ALIBABACLOUD_*` spelling), falls back to ECS RAM role via
  `ALIBABA_CLOUD_ECS_METADATA`, falls back to `nil`.  Works.

### 5.3 CVO ownership on ipam CRDs

OCP 4.20 ships its own copies of three CRDs in the
`ipam.cluster.x-k8s.io` group.  The cluster-version-operator owns the
schema fields on them.  A plain `oc apply` fails with

```
Apply failed with 3 conflicts: conflicts with "cluster-version-operator"
```

Use `--force-conflicts` on server-side apply to take ownership.  This
is safe — the CRDs are upstream CAPI definitions and CVO's copy is
identical to ours.

### 5.4 `alibaba-creds` Secret must exist before CAPA starts

The CAPA Deployment in `02-capa-controller.yaml` references the Secret
with `envFrom.secretRef.name=alibaba-creds` and `optional: false`.
The Pod will not start until the Secret exists; CrashLoopBackOff with
`Error creating: configmap "alibaba-creds" not found`.

Create it once per cluster after install:

```bash
oc -n capa-system create secret generic alibaba-creds \
  --from-literal=ALIBABA_CLOUD_ACCESS_KEY_ID=$AK \
  --from-literal=ALIBABA_CLOUD_ACCESS_KEY_SECRET=$SK
```

The AK/SK should be the operator's RAM user credentials (read from
`~/.aliyun/config.json` profile `openshift-test` on the operator
box).  These are the same credentials the OpenShift CCM uses.

### 5.5 "VPC/SLB creation not yet implemented, skipping"

This log line is **expected and benign**.  Upstream
`openshift-capi-alicloud` assumes VPC + SLB already exist (provisioned
by `03-create-cluster-stack.yml` via ROS), and the controller does not
provision them.  The cluster still goes Ready=True because the
reconciler's "happy path" is the no-op path.  This is a known
simplification of the upstream provider that's appropriate for our
use case (ROS owns the infra; CAPI owns the worker nodes).

### 5.6 Manifest stage failure can leave AlibabaCloudCluster stuck

If the `oc apply` of the AlibabaCloud{Cluster,Machine} happens before
the Cluster + Machine CRs (i.e. before §3.4), the patch in §3.5
points at non-existent UIDs.  CAPA will log:

```
"cluster infrastructure not ready (ClusterInfrastructureNotReady)"
```

with the old `lastTransitionTime`.  Fix: re-patch with the actual UIDs
of the real Cluster + Machine objects.  CAPA reconciles every 10s and
will pick up the corrected OwnerRef next pass.

---

## 6. Recovery patterns

### Cluster never goes Ready=True
- Check `oc get alibabacloudcluster smoke-test -o yaml | grep -A5 conditions`
  for the actual error message
- If `UnsupportedCredential` — you're on `:v0.1.1` or older.  See §5.2.
- If `ClusterInfrastructureNotReady` — the ownerRef UID doesn't match
  any Cluster object.  See §5.6.

### AlibabaCloudMachine stays at `instanceState: Pending`
- The ECS is being created or booting.  Wait 1–2 minutes.
- If `Failed` — see the CAPA logs for the Aliyun API error
  (insufficient quota, wrong vSwitch, wrong image, etc.).
- If `instanceID` is empty after 30s — RunInstances itself failed.
  Check logs for the Aliyun SDK error message.

### "Machine Controller has not yet set OwnerRef" returns
- The patch from §3.5 didn't stick (UID typo, or you patched the
  wrong object).  Re-run with the real UIDs.

---

## 7. Future work (P3+)

This CRDs-only path is the minimum viable path to demonstrate
RunInstances.  Production deployment of CAPI in this OCP cluster would
add:

1. `cert-manager` (CAPI core webhooks need it for serving certs).
2. The full `capi-controller-manager` Deployment + Service +
   webhooks.
3. `MachineDeployment` + `MachineSet` + a bootstrap provider
   (`KubeadmConfig` won't work for OCP — we'd need a custom bootstrap
   that produces an Ignition config and machine-config server URL
   instead of cloud-init).
4. The upstream CAPA `AlibabaCloudClusterTemplate` and
   `AlibabaCloudMachineTemplate` flow (not exercised here).

The biggest open question for "actually scale with CAPI" is the
bootstrap provider — kubeadm doesn't speak Ignition.  Options are
either fork `cluster-api-bootstrap-provider-kubeadm` to emit Ignition,
or build a thin `cluster-api-bootstrap-provider-machine-config-server`
that simply curls Ignition from the OCP MCS and stuffs it into
UserData.  Tracked as a P3 / design discussion.

---

## 8. CAPI contract behaviour as of CAPA v0.1.3

The CRDs-only path in this doc crafts `Cluster`/`Machine` owners and patches
OwnerRefs by hand (§3). With CAPA `v0.1.3` (PR1) the provider follows the
upstream CAPI contract more faithfully, which changes what to expect when
driving it manually:

- **A `Machine` with no `spec.bootstrap.dataSecretName` will NOT boot an ECS.**
  The machine controller requeues with `WaitingForBootstrapData`. When testing
  RunInstances by hand, either set `bootstrap.dataSecretName` on the owning
  Machine or fall back to `AlibabaCloudMachine.spec.userDataSecret`.
- **providerID is `alicloud://<region>/<instanceID>`** (slash). Delete now
  parses the region back out of providerID and clears its own finalizer — the
  hand-patched-OwnerRef delete no longer hangs.
- **`AlibabaCloudCluster` stays `ready=false` until
  `status.controlPlaneEndpoint.host` is set** (`ControlPlaneEndpointMissing`).
  Supply `spec.controlPlaneEndpoint` (BYO api-int) on the cluster CR or it never
  goes ready. Mind the CRD-regen pruning gotcha —
  see [CAPA-SMOKE.md §8](CAPA-SMOKE.md).

Full smoke evidence lives in [CAPA-SMOKE.md §8](CAPA-SMOKE.md).

---

## 9. The CRDs-only ceiling — declarative MD pools need the full core controller

The CRDs-only path above is enough for **Route B**: you hand-craft a single
Cluster+Machine and CAPA reconciles the AlibabaCloudMachine directly. It is **not**
enough for the **declarative multi-AZ MachineDeployment pools** (#62): a
MachineDeployment needs the **CAPI core controller** (MD → MachineSet → Machine) to
derive Machines. With CRDs only, `12`'s pools apply but never spin a single worker.

Confirmed 2026-06-09 on a live SNO cluster: CAPA v0.1.12 reached 2/2 Ready (after
fixing a chain of default-chain gaps — image SSOT, vSwitch allocator, template
path, missing webhook Service), but no Machine/worker materializes without the core
controller. The OCP-hosted cluster-capi-operator does **not** supply core on
platform=external (verified: `clusters.cluster.x-k8s.io` and the
`openshift-cluster-api` namespace both NotFound), so we must self-manage it.

**Self-managed core controller — IMPLEMENTED 2026-06-09 (offline, in the default
chain; pending a live-cluster verification run):**
- **core-only** — `scripts/gen-cluster-api-core.py` derives
  `custom_manifests/cluster-api-core.yaml` from cluster-api v1.12.7's components,
  dropping the kubeadm bootstrap/control-plane providers (we BYO bootstrap). 24
  resources; regenerate via the script to bump versions — do not hand-edit.
- **webhooks via service-ca** — the generator drops the cert-manager
  Certificate+Issuer and stamps the webhook Service with `serving-cert-secret-name`
  and every webhook (2 admission + 13 CRD conversion) with `inject-cabundle`, so
  OCP service-ca mints `capi-webhook-service-cert` and injects the CA bundle (same
  pattern as CAPA's 02-capa-webhooks).
- **mirror + IDMS + apply** — `ansible/playbooks/08a-capi-core.yml`: `oc image
  mirror` pushes `registry.k8s.io/cluster-api/cluster-api-controller:v1.12.7` into
  the air-gapped registry, applies an IDMS, digest-pins (the 09-smoke recipe), and
  applies the manifest (which **also installs the CAPI core CRDs** — replacing the
  curl-GitHub awk path), then waits for the rollout.
- **default chain** — imported at the head of `site-post` (08a → 08 → 10 → 12), so
  core CRDs + controller exist before CAPA (08) and the MD pools (12).

Commits `557fb27` (generator + manifest) and `2caaab5` (08a + wiring). Still to
verify on a live cluster: the jump host reaching `registry.k8s.io` + mirror push
auth, the controller coming Ready under service-ca, and 12's MD pools deriving
Machines into workers. `11`/`09` still curl GitHub for CRDs (optional paths) — fold
them onto the vendored manifest later.

---

## 10. Version history

| Date | Change |
|---|---|
| 2026-06-01 | Initial — P2-CAPI verification run.  CRDs-only path works; CAPA v0.1.2 (with the credential fix from openshift-capi-alicloud `bb1f73e`) calls RunInstances and a real ECS `i-0jl0ucoda565o2pq16wj` boots in cn-wulanchabu.  Commits: `6033915` (alibaba-openshift v0.1.1→v0.1.2 + envFrom alibaba-creds) + `bb1f73e` (openshift-capi-alicloud resolveCredential). |
| 2026-06-05 | CAPA `v0.1.3` (PR1) tightens CAPI-contract behaviour (§8): bootstrap-data gate, slash providerID + self-clearing finalizer, controlPlaneEndpoint Ready gate.  Affects how the manual CRDs-only path must be driven. |
| 2026-06-09 | Live SNO run drove the default chain end-to-end under v0.1.12 + declarative MD pools. CAPA v0.1.12 reached 2/2 Ready after fixing 5 default-chain gaps (`4cad336`/`afb4daa`/`806cd22`/`5f1aae1`/`933e6bc`). Confirmed the CRDs-only ceiling (§9): MD pools need the self-managed core controller. #79 step-0 done — OCP supplies no hosted core on platform=external. |
