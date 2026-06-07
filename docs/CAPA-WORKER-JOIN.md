# CAPA worker join — Route B (aliyun-platform RHCOS + user-data Ignition)

How a CAPA-provisioned ECS becomes a **real OpenShift worker node** on Alibaba
Cloud, the upstream-standard way — no Assisted Installer day-2.

Validated end-to-end on a live SNO cluster on **2026-06-07** (OCP 4.20.22,
RHCOS 9.6, CAPA `v0.1.8`).

> Reusable playbooks:
> - [`ansible/playbooks/10-prepare-worker-bootimage.yml`](../ansible/playbooks/10-prepare-worker-bootimage.yml) — build the boot image (one-time)
> - [`ansible/playbooks/11-capa-routeb-join.yml`](../ansible/playbooks/11-capa-routeb-join.yml) — deploy CAPA + join a worker + verify

---

## 1. The problem

CAPA (the Cluster API infra provider for Alibaba) creates ECS instances. To turn
one into an OCP worker, the node must run **RHCOS** and consume the cluster's
**worker Ignition** (a pointer to the in-cluster Machine Config Server). OpenShift
nodes boot from Ignition, not cloud-init — and upstream CAPI's default bootstrap
provider emits cloud-init. That mismatch is the long-standing open question
(design doc §4.18).

There were two candidate routes:

| | **Route B — user-data Ignition (chosen)** | Route A — AI day-2 discovery |
|---|---|---|
| Mechanism | RHCOS reads the worker pointer Ignition from ECS **user-data**; fetches `config/worker` from the MCS; kubelet joins | Boot the Assisted day-2 discovery image; AI installs + joins |
| Runtime deps | none beyond the cluster itself | Assisted Installer SaaS + per-host install orchestration |
| Alignment | identical to upstream `cluster-api-provider-oci` / the old `openshift/cluster-api-provider-alibaba` | bespoke |

**Route B is correct and simpler** because of one fact that is easy to get wrong:

> **Alibaba Cloud (`aliyun`) is a first-class Ignition platform.** Per the
> [Ignition supported-platforms list](https://coreos.github.io/ignition/supported-platforms/),
> on `aliyun` *"Ignition will read its configuration from the instance
> userdata"* — exactly like AWS / OCI. So passing the worker pointer Ignition as
> ECS user-data is all RHCOS needs.

CAPA's `getUserData` already pipes the bootstrap dataSecret straight to ECS
user-data (base64), matching CAPOCI's `metadata["user_data"]` verbatim — so the
controller needed **no change** for this.

The only catch: OpenShift stopped publishing an RHCOS **aliyun boot image** after
the `alibabacloud` platform was removed (~4.15). The installed nodes here run
`ignition.platform.id=metal` (baked by the Assisted discovery install), which
does **not** read cloud user-data. So Route B's one piece of real work is
**producing an aliyun-platform RHCOS image**.

---

## 2. End-to-end flow

```
10-prepare-worker-bootimage.yml  (one-time)
  generic openstack RHCOS qcow2  ──guestfish──▶  ignition.platform.id = aliyun
        (from cluster coreos-bootimages stream)        │
                                                       ▼
                                          OSS upload → ecs ImportImage (Platform=CoreOS, QCOW2, UEFI)
                                                       │
                                                       ▼
                                   worker_boot_image_id = m-xxxx   → state.yml

11-capa-routeb-join.yml
  AlibabaCloudCluster.spec.bootImageID = m-xxxx
  AlibabaCloudMachine (NO imageID → falls back to bootImageID)
  bootstrap secret value = worker pointer Ignition (from openshift-machine-api/worker-user-data)
        │
        ▼  CAPA RunInstances(image = bootImageID, user_data = worker.ign-pointer)
   ECS boots RHCOS (platform=aliyun)
        │  Ignition reads user-data → merges https://api-int:22623/config/worker (+ MCS CA)
        ▼
   kubelet starts → node-bootstrapper CSR → (auto-approved) → Node Ready (role worker)
```

---

## 3. Building the boot image (Phase 10)

`virt-customize` can't introspect RHCOS's OSTree layout (`no operating systems
were found`), so we edit the BootLoaderSpec karg directly with **guestfish**:

```bash
# generic openstack RHCOS qcow2 is at architectures.x86_64.artifacts.openstack
# .formats["qcow2.gz"].disk.location in the cluster's coreos-bootimages cm.
gunzip rhcos.qcow2.gz
# boot partition is the ext4 one (RHCOS: p1 bios, p2 EFI vfat, p3 boot ext4, p4 root xfs)
guestfish --rw -a rhcos.qcow2 run : mount /dev/sda3 / : copy-out /loader/entries /tmp/b : copy-out /grub2/grub.cfg /tmp/b
sed -i 's/ignition\.platform\.id=[a-z]*/ignition.platform.id=aliyun/g' /tmp/b/entries/*.conf /tmp/b/grub.cfg
guestfish --rw -a rhcos.qcow2 run : mount /dev/sda3 / : upload <each entry> /loader/entries/... : upload /tmp/b/grub.cfg /grub2/grub.cfg
```

Then import (qcow2 imports directly; Alibaba recognises the `CoreOS` platform):

```bash
aliyun ecs ImportImage --RegionId <region> --ImageName rhcos-worker-aliyun \
  --OSType Linux --Platform CoreOS --Architecture x86_64 \
  --DiskDeviceMapping.1.Format QCOW2 --DiskDeviceMapping.1.OSSBucket <bucket> \
  --DiskDeviceMapping.1.OSSObject rhcos-worker-aliyun.qcow2 --BootMode UEFI
```

The resulting image is **version-bound (OCP/RHCOS), not infra-env-bound**, so it
is reusable across cluster reinstalls of the same version. Phase 10 is idempotent
— pass `-e worker_boot_image_id=m-xxxx` (or have it in state) and it skips the
whole build when the image is already `Available`.

Tooling note: RHEL8 needs `libguestfs-tools` + `libguestfs-tools-c` (the package
is `guestfs-tools` on RHEL9). On a nested-virt-less operator run guestfish with
`LIBGUESTFS_BACKEND=direct`.

---

## 4. Joining a worker (Phase 11)

1. Deploy the CAPA build under test: CRDs → **controller** (creates the
   `capa-system` namespace) → **webhooks** (Service + configs; service-ca mints
   the serving cert) → `alibaba-creds` → set the controller image to the mirror
   **digest** (air-gapped IDMS is digest-only) → rollout.
2. **Wait for the webhook Service to have endpoints** before applying any
   `AlibabaCloud*` object — the webhooks are `failurePolicy: Fail`, and the pod
   crash-restarts a couple of times while service-ca mints the cert, so
   rollout-complete alone isn't enough.
3. Install the CAPI core CRDs if missing.
4. Worker Ignition: `oc -n openshift-machine-api get secret worker-user-data
   -o jsonpath='{.data.userData}' | base64 -d` → a spec-3.2 pointer config
   (merges `https://api-int.<cluster>:22623/config/worker` + the MCS CA). Put it
   in a Secret's `value` key and reference it as the Machine bootstrap.
5. Apply the chain: `AlibabaCloudCluster` (with `bootImageID`,
   `controlPlaneEndpoint`) + `AlibabaCloudMachine` (**no `imageID`** → bootImage
   fallback) + CAPI `Cluster` + `Machine`; patch OwnerRefs.
6. Wait for `status.instanceID`, then auto-approve node CSRs in a loop until the
   new node is `Ready`.

---

## 5. Live validation (2026-06-07)

```
TASK [Wait until CAPA boots the ECS]                 → i-0jlbqw6zx62v1xchi1qk
node/iz0jlbqw6zx62v1xchi1qkz   Ready   worker   v1.33.11      ← CAPA-provisioned ECS
csr-l2xlf  ...node-bootstrapper...  Approved,Issued           ← kubelet CSR signed
```

A CAPA `AlibabaCloudMachine` with **no `imageID`** booted the cluster's
`bootImageID` (the re-stamped aliyun RHCOS), read the worker pointer Ignition
from ECS user-data, pulled `config/worker` from the MCS, and joined as a `Ready`
worker — proving Route B end-to-end. (The `bootImageID` fallback path, P3-CAPA.16,
and the webhook, P3-CAPA.7, were validated live in the same run.)

---

## 6. Gotchas (all already handled in the playbooks)

| Symptom | Cause / fix |
|---|---|
| controller pod `CrashLoopBackOff` after enabling webhooks | webhook server needs serving certs; `02-capa-webhooks.yaml` Service + service-ca mints them, mounted at `/tmp/k8s-webhook-server/serving-certs`. |
| `namespaces "capa-system" not found` applying webhooks | apply **controller before webhooks** (controller manifest creates the namespace). |
| `no endpoints available for service "capa-webhook-service"` on first apply | `failurePolicy: Fail` + pod still rolling/cert-minting; wait for Service **endpoints** before applying `AlibabaCloud*`. |
| controller pod hangs in `ContainerCreating` on a fresh tag | air-gapped IDMS is `pull-from-mirror = digest-only`; deploy by **digest**, not tag (P3-FIX.11). |
| new spec fields (spot/disk/bootImageID) silently pruned | re-apply the regenerated `02-capa-crds.yaml` (structural-schema pruning). |
| node never goes Ready | worker SG must allow egress to `api-int:22623` (MCS) + `:6443`; node CSRs need approving (no machine-api machine backs them). |
| guestfish `no operating systems were found` | RHCOS is OSTree; edit `/boot` (BLS entries) directly via guestfish `mount`+`copy-out/upload`, not `virt-customize --run-command`. |

---

## 7. Cost

The boot image (`m-xxxx`, ~16 G image + 16 G backing snapshot) is a deliberate
keep — reusable across reinstalls of the same OCP version. The validation worker
ECS + node are ephemeral and swept by Phase 11's `always` cleanup. Delete the
image + its OSS object (`rhcos-worker-aliyun.qcow2`) if you no longer need worker
provisioning for that version.

---

## 8. Self-service worker join — controller-side CSR approval (B1)

Route B nodes have no machine-api Machine, so OpenShift's cluster-machine-approver
never approves their kubelet CSRs — they would hang NotReady until someone ran
`oc adm certificate approve`. The CAPA controller (>= v0.1.9) ships a
`CertificateSigningRequestReconciler` that approves them, bound to a CAPA machine:

- **bootstrap** (`kube-apiserver-client-kubelet`): approved only for the
  node-bootstrapper SA when a provisioned `AlibabaCloudMachine` is awaiting its node.
- **serving** (`kubelet-serving`): approved only when the node exists, is backed
  by a CAPA machine (providerID match), and every SAN is one of that node's addresses.

Validated live 2026-06-07: a worker joined Ready with **no manual approval** —
both CSRs auto-approved (`reason=CAPAApprove`). 11-capa-routeb-join.yml therefore
just waits for Ready by default (`-e routeb_manual_csr=true` restores playbook-side
approval for older controller images).

Two bugs this surfaced (both fixed):

| Bug | Cause / fix |
|---|---|
| serving CSR rejected | node providerID (Alibaba CCM, **dot**: `alicloud://<region>.<id>`) vs machine providerID (CAPA, **slash**: `alicloud://<region>/<id>`) differ — compare by normalised instance id (`providerInstanceID`). v0.1.10. |
| two ECS created, one orphaned | `findOrCreateInstance` created unconditionally when `Status.InstanceID==nil`; a lost status write → a duplicate. `CreateECSInstance` now adopts an existing instance found by the `k8s.io/cluster-api-machine=<name>` tag before RunInstances. v0.1.11 (P3-CAPA.21). |
