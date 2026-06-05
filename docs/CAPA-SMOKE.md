# CAPA Smoke Test Runbook

Operational reference for verifying the Cluster API Provider for Alibaba
Cloud (CAPA) controller deployment into an installed OpenShift cluster.
Distilled from the P1-CAPA verification run on 2026-06-01.

For architecture context see:
- [README.md](../README.md) — top-level overview
- [E2E-RUNBOOK.md](E2E-RUNBOOK.md) — cluster install (this doc assumes a
  working installed cluster from there)
- [POST-INSTALL.md](POST-INSTALL.md) — broader post-install picture
  (CSI / OADP / CAPA)
- Upstream: <https://github.com/SammZhu/openshift-capi-alicloud> (CAPA source)

---

## 1. Scope of this smoke test

What we verify:
- CAPA controller image is reachable from cluster masters (via mirror)
- CAPA Deployment becomes `1/1 Running` and acquires leader election
- Applying an `AlibabaCloudMachine` CR causes the CAPA reconciler to
  receive and process it

What we do NOT verify here (out of scope, future work):
- End-to-end `RunInstances` flow that boots a new ECS as a Cluster API
  Machine — requires the upstream Cluster API core controller
  (`capi-controller-manager` + `clusters.cluster.x-k8s.io` /
  `machines.cluster.x-k8s.io` CRDs) which we don't install yet.
  CAPA correctly stops at the Cluster API gate: "Machine Controller
  has not yet set OwnerRef".

---

## 2. Prerequisites

- An OpenShift cluster installed by `playbooks/site.yml` (3 masters
  Ready, all 34 cluster operators Available).
- `mirror_enabled: true` and a populated mirror Quay (see
  [MIRROR.md](MIRROR.md)).
- The current `build-mirror-tarball.sh` (since commit 7054bfe /
  squashed da7322a) pins the CAPA image as an additionalImage in the
  imageset, so any fresh tarball contains it.  For older tarballs,
  `04-prepare-mirror.yml` includes a CAPA pre-pull belt-and-suspenders
  task that relays the image from quay.io through the operator and
  jumphost into the mirror.
- jumphost has `oc` + `kustomize` and a copy of the repo at
  `/root/openshift-alibaba/alibaba-openshift/`.
- kubeconfig at `/root/kubeconfig` on jumphost (Phase 07 scp's it
  there automatically).

---

## 3. Deploy CAPA — manual (current path)

Phase 08 (`08-deploy-post-install.yml`) is designed to deploy CAPA + CSI
+ OADP together, but it requires `ansible-playbook` on the jumphost
(currently not installed there).  For now, run CAPA's two manifests
directly on the jumphost via `oc`.

```bash
# On the operator box (RHEL 8), ssh to jumphost as root:
JUMP=$(awk '/^jump_host_ip:/{print $2}' ansible/state.yml)
ssh -i ~/work/alibabacloud/sshkey/20231118_ed25519 root@$JUMP

# (Once on jumphost) pull latest repo and apply CAPA manifests:
cd /root/openshift-alibaba/alibaba-openshift
git pull origin main

oc --kubeconfig=/root/kubeconfig apply -f custom_manifests/02-capa-crds.yaml
oc --kubeconfig=/root/kubeconfig apply -f custom_manifests/02-capa-controller.yaml
```

Verify:
```bash
oc --kubeconfig=/root/kubeconfig -n capa-system get pods
# capa-controller-manager-xxxxx   1/1   Running

oc --kubeconfig=/root/kubeconfig -n capa-system logs deploy/capa-controller-manager --tail=10
# Expect:
#   Starting cluster-api-provider-alibaba version=0.1.1
#   successfully acquired lease capa-system/controller-leader-election-capa
#   Starting Controller controller=alibabacloudmachine
#   Starting workers worker count=1
```

A line like

```
no matches for kind "Cluster" in version "cluster.x-k8s.io/v1beta2"
```

is **expected and benign** — see §6 below.

---

## 4. Smoke test — apply a minimal AlibabaCloudMachine

Write a minimal `AlibabaCloudCluster` + `AlibabaCloudMachine` pair.
Substitute values from your `ansible/state.yml`:
- `region` ← `group_vars/all.yml` (e.g. `cn-wulanchabu`)
- `zoneID` ← one of `zone` / `zone2`
- `vSwitchID` ← `mirror_private_vsw_1` (any private VSwitch in the VPC)
- `securityGroupIDs` ← `worker_sg`
- `ramRoleName` ← `mirror_node_ram_role` (e.g. `aliocp1-node-role`)
- `imageID` ← any Aliyun public image; for a non-OCP smoke test use
  `rockylinux_9_7_x64_20G_alibase_<date>.vhd` or similar (find via
  `aliyun ecs DescribeImages --ImageOwnerAlias system`)

```yaml
# /root/capa-smoke.yaml on jumphost
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: AlibabaCloudCluster
metadata:
  name: smoke-test
  namespace: default
spec:
  region: cn-wulanchabu
---
apiVersion: infrastructure.cluster.x-k8s.io/v1beta1
kind: AlibabaCloudMachine
metadata:
  name: smoke-test-worker
  namespace: default
spec:
  instanceType: ecs.t6-c1m1.large
  imageID: rockylinux_9_7_x64_20G_alibase_20260525.vhd
  zoneID: cn-wulanchabu-a
  vSwitchID: vsw-0jlq9kj88skxl5cqo9snl
  securityGroupIDs:
    - sg-0jl5x011zk06q6vu94kj
  ramRoleName: aliocp1-node-role
  systemDisk:
    category: cloud_essd
    size: 40
```

Apply:
```bash
oc --kubeconfig=/root/kubeconfig apply -f /root/capa-smoke.yaml
```

Expected reconcile log line in CAPA controller (within ~1 s):
```
"msg"="Machine Controller has not yet set OwnerRef"
"AlibabaCloudMachine"={"name":"smoke-test-worker","namespace":"default"}
"reconcileID"="<uuid>"
```

That single log line is the smoke-test success criterion — the
reconciler **received the CR, executed the controller code path
(`alibabacloudmachine_controller.go:70`), and correctly stopped at the
Cluster-API OwnerRef gate**.  See §6 for why we don't proceed further.

Cleanup:
```bash
oc --kubeconfig=/root/kubeconfig delete -f /root/capa-smoke.yaml
```

---

## 5. Known gotchas (2026-06-01)

### 5.1 First CAPA image (`:v0.1.0`) panicked at init()

`pkg/version/version.Raw` got injected with the build-time
`git describe --tags --always --dirty` value, which fell back to a bare
git short SHA (`7df900c-dirty`) when no tag was reachable.
`semver.MustParse` then panicked at package init() before main() ran,
sending the CAPA pod into `CrashLoopBackOff`:

```
panic: semver: Parse(7df900c-dirty): No Major.Minor.Patch elements found
```

Fixed in `SammZhu/openshift-capi-alicloud` commit a4dc369 — the parse is
now forgiving and falls back to `0.0.0-dev` instead of panicking.
Built and pushed as `quay.io/samzhu/openshift-capi-alicloud:v0.1.1`.

If you ever see this panic again, rebuild with an explicit semver
VERSION:
```bash
make push IMAGE_TAG=v0.1.X VERSION=v0.1.X
```

### 5.2 CAPA image: tag form vs digest form pull

`02-capa-controller.yaml` references the image by tag (`:v0.1.1`).
On `platform=external` master nodes the IDMS rule rewrites pulls to the
mirror, but with `pull-from-mirror = digest-only`.  cri-o therefore
first resolves the tag against quay.io to a digest, then pulls layers
from the mirror by that digest.

This is fine when:
- quay.io is reachable from the master (tag→digest is a tiny manifest call)
- the mirror has the same digest the quay tag points at

It breaks when:
- you `oc set image @sha256:<digest>` to a digest you computed locally
  (`podman image inspect .[0].Digest` is the local image ID, not the
  registry-side manifest digest).
- the mirror has a different manifest schema than quay (e.g. mirror
  stored docker-v2-schema while quay stored OCI — the bytes differ,
  the digests differ).  Observed 2026-06-01: pushing CAPA from
  operator's skopeo to mirror Quay implicitly converted the manifest
  format, leaving the registries with different digests for the same
  image.

If you hit `manifest unknown` after `oc set image @sha256:...`,
revert to tag form by re-applying `02-capa-controller.yaml`.

### 5.3 master-3 sometimes takes minutes to tag-resolve from quay

Cross-border quay.io tag-manifest GET from cn-wulanchabu can be slow
or flaky.  Observed master-1 = 26 s, master-3 = 4 minutes for the same
operation in different runs.  Subsequent pulls (image cached on node)
are sub-second.  If the first ImagePull takes too long, manually run
`crictl pull` on the node where the Pod is scheduled to prime the
cache.

---

## 6. Why the smoke test stops at "Machine Controller has not yet set OwnerRef"

CAPA is a Cluster API **infrastructure provider**.  It only acts when
its CR is owned by a Cluster API core `Machine` (group
`cluster.x-k8s.io`), which is created by the upstream
`capi-controller-manager`.  We have intentionally not installed the
upstream CAPI core CRDs + controller in this repo's scope — they're
non-trivial (≈10 CRDs, a controller deployment, webhooks) and orthogonal
to the OpenShift install flow that's the focus of P0-P1.

Concrete reconciler behavior:

```go
// alibabacloudmachine_controller.go (simplified)
if !util.HasOwner(awsMachine.OwnerReferences, ...) {
    log.Info("Machine Controller has not yet set OwnerRef")
    return reconcile.Result{}, nil
}
// ... actual ECS RunInstances call follows
```

For a fully E2E test that boots a new ECS:
1. Install upstream CAPI core (the `clusters.cluster.x-k8s.io`,
   `machines.cluster.x-k8s.io`, etc. CRDs + `capi-controller-manager`).
2. Apply a `Cluster` + `Machine` referencing the `AlibabaCloudCluster`
   + `AlibabaCloudMachine` via `infrastructureRef`.
3. Once Machine sets OwnerRef on AlibabaCloudMachine, CAPA's reconciler
   proceeds past the gate above and calls `ecs.RunInstances`.

This is tracked as a P2 follow-on ("install upstream Cluster API
core").  It's a significant addition (≈20-30 manifests, plus credential
plumbing) and should land as its own phase in `ansible/playbooks/`.

---

## 7. Recovery patterns

### CAPA Pod stuck in CrashLoopBackOff
```bash
oc -n capa-system logs deploy/capa-controller-manager --previous --tail=30
```
If the panic is the `semver.MustParse` one (§5.1), update to `:v0.1.1`
or newer.

### CAPA Pod stuck in ImagePullBackOff
```bash
oc -n capa-system describe pod -l app=capa-controller-manager
```
- If "manifest unknown" — you're probably referencing a digest the
  mirror doesn't have.  Revert to tag form (§5.2).
- If "context deadline exceeded" — slow quay tag-resolution.  Prime
  the node's image cache manually:
  ```bash
  ssh core@<master-with-pending-pod> sudo crictl pull quay.io/samzhu/openshift-capi-alicloud:v0.1.1
  ```

### Smoke test CR shows no events / no logs
- Check that the CRDs are actually installed:
  ```bash
  oc get crds | grep alibabacloud
  ```
  Expect 4: `alibabacloudclusters`, `alibabacloudclustertemplates`,
  `alibabacloudmachines`, `alibabacloudmachinetemplates`.
- Check the controller actually got reconciled to the CR — log line
  with the `reconcileID` field from §4.

---

## 8. PR1 (v0.1.3) — CAPI contract-compliance verification

Smoke-validated `quay.io/samzhu/openshift-capi-alicloud:v0.1.3` on a live SNO
cluster (cn-wulanchabu). This is the build from openshift-capi-alicloud PR1
(#23/#24/#25/#28). What was confirmed end-to-end:

| # | Behaviour | Observed result |
|---|---|---|
| #23 | providerID format | `alicloud://cn-wulanchabu/i-0jl2w0y4apxvj9d1j56r` — slash separator and a real region (no longer `alicloud://.<id>`). |
| #23 | delete-path region parse | `oc delete alibabacloudmachine` → `regionFromMachine` parsed the region → "Deleted ECS instance" → finalizer auto-cleared → object gone. **The original P2 finalizer-hang is fixed.** |
| #24 | bootstrap gate | Before `spec.bootstrap.dataSecretName` was set, the controller requeued with `WaitingForBootstrapData` and made **no** `RunInstances` call; once the data-secret existed it proceeded to create the ECS. |
| #25 | controlPlaneEndpoint mirror | `status.controlPlaneEndpoint.host = api-int.aliocp1.example.local` mirrored from spec; with the endpoint missing the cluster stayed `ready=false` reason `ControlPlaneEndpointMissing`. |
| #28 | paused | `cluster.x-k8s.io/paused` annotation skipped reconciliation as expected. |

Post-test: ECS instance count back to 0; smoke CRs deleted.

### CRD-regeneration gotcha (important)

`status.controlPlaneEndpoint` is a **new** status field added in #25. The API
server **prunes any status field absent from the deployed CRD's OpenAPI
schema** (structural-schema pruning). On first apply the field was silently
dropped — `oc get -o yaml` never showed it — until the regenerated CRD was
applied.

Rule: **after any Go struct change to a `*_types.go`, run `make generate` in
openshift-capi-alicloud AND re-apply the CRDs here** before trusting status.
The regenerated CRDs live in
[`custom_manifests/02-capa-crds.yaml`](../custom_manifests/02-capa-crds.yaml)
(commit `a59828e` carries the `status.controlPlaneEndpoint` schema).

```bash
# Sanity-check the deployed CRD actually carries the field:
oc get crd alibabacloudclusters.infrastructure.cluster.x-k8s.io -o json \
  | jq '.spec.versions[].schema.openAPIV3Schema.properties.status.properties.controlPlaneEndpoint'
# null  → CRD is stale, re-apply 02-capa-crds.yaml
# {...} → good
```

---

## 9. Version history

| Date | Change |
|---|---|
| 2026-06-01 | Initial — P1-CAPA verification run.  CAPA image v0.1.0 panic discovered; rebuilt as v0.1.1 with semver fallback; smoke test reached Cluster-API OwnerRef gate as expected.  All commits: `c7b4b9a` (alibaba-openshift) + `a4dc369` (openshift-capi-alicloud). |
| 2026-06-05 | PR1 contract-compliance (§8) — CAPA `v0.1.3` smoke-validated on live SNO: providerID slash+region, delete finalizer auto-clear (P2 hang fixed), bootstrap gate, controlPlaneEndpoint mirror, paused.  CRD-regen pruning gotcha documented.  Commits: `a59828e` (02-capa-crds.yaml) + `0239b97`/`0d91747` (openshift-capi-alicloud PR1 + CRD regen, tag `v0.1.3`). |
