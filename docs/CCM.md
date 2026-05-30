# Alibaba Cloud Controller Manager (CCM)

Reference doc for the CCM deployment used by this project: design
contract, image source, config schema, landmines we hit.

For the high-level architecture see [design-and-development-summary.md](design-and-development-summary.md).
For day-to-day install flow see [QUICKSTART](../QUICKSTART.md).

---

## 1. `platform=external` design contract

Our install-config sets `platform: external` with `platformName: alibaba`.
That choice is a binding contract with OpenShift — **you must run a
working CCM in the cluster**, or install deadlocks.

| install-config `platform` | kubelet `--cloud-provider` | `cloudprovider.uninitialized` taint on nodes | LoadBalancer Service | Who initializes node fields (providerID / addresses / topology labels) |
|---|---|---|---|---|
| `external` (us)            | `external`             | **added** at boot, removed only when CCM finishes initialization | supported (via your CCM)  | **your external CCM** |
| `none`                     | empty                  | not added                                                        | unsupported                | nobody (manual / MachineConfig hacks)           |
| `aws` / `azure` / `gcp` / `vsphere` / ... | the matching name | added | supported (built-in CCM operator) | OCP-managed cloud-controller-manager-operator   |

### Why the taint exists

`node.cloudprovider.kubernetes.io/uninitialized:NoSchedule` is the
scheduler's enforcement of the contract.  Until CCM has:

1. Pulled instance metadata from the cloud API
2. Written `node.spec.providerID`
3. Written `node.status.addresses` (Internal/External IP, Hostname)
4. Set the standard cloud labels (`topology.kubernetes.io/region`,
   `topology.kubernetes.io/zone`, `node.kubernetes.io/instance-type`,
   ...)
5. **Removed the taint**

...the scheduler refuses to place pods that don't explicitly tolerate
the taint.  Most cluster-network-operator / Ingress / OLM pods do **not**
tolerate it — by design, so they can't run on a half-configured node.

### What CCM does after that

Steady-state responsibilities:

- **Service controller** — watch Service `type=LoadBalancer`, create /
  reconcile cloud LB (NLB or CLB on Alibaba), wire backend pool to
  worker nodes, manage annotations like
  `service.beta.kubernetes.io/alibaba-cloud-loadbalancer-*`.
- **Node controller** — watch Node, update addresses + topology labels
  when underlying VM changes, evict on `NotReady` past tolerance.
- **Route controller** (optional, **disabled here**) — when
  `--configure-cloud-routes=true`, allocates per-node Pod CIDR and
  writes VPC route table entries.  We use OVN-Kubernetes overlay
  (Geneve) so VPC routing is unnecessary; we run with
  `--configure-cloud-routes=false`.

### Why not `platform=none`?

Would silence the taint and avoid the dependency, but in exchange:
- No LoadBalancer Service (you'd need MetalLB / manual NLB plumbing)
- No automatic node IP / labels (some operators rely on these)
- No cloud-aware Service `externalTrafficPolicy=Local` source IP
  preservation
- No PV provisioner integration (CSI works but lacks topology)

Acceptable for a throwaway test cluster, **not acceptable for any
real workload**.  Cluster requires CCM long-term — `platform=external`
is the right call.

### Why not OCP-built-in Alibaba CCM?

OCP 4.x does not list Alibaba as a first-class platform with an
OCP-managed CCM operator.  We have to bring our own.

---

## 2. Image source

**Canonical image** (per upstream
[v2.14.0 release notes](https://github.com/kubernetes/cloud-provider-alibaba-cloud/releases/tag/v2.14.0)):

```
registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager:v2.14.0
```

The repo `registry.k8s.io/provider-alibaba-cloud/alibaba-cloud-controller-manager`
is registered but has **no manifests published** for any v2.x tag — older
docs that point there are stale.  Don't waste time trying to pull from
registry.k8s.io.

The alias `registry.cn-hangzhou.aliyuncs.com/acs/cloud-controller-manager-amd64:v2.14.0`
is the same blob (verified via `podman inspect` — identical
sha256:cab36121c7b9c1862bcc6bae434b3d153a0b4369ee71be7e196355cb68e303fd).
Both paths work; we standardise on the `.ack.aliyuncs.com` form because
that's what upstream documents.

### Mirror flow in this project

1. `scripts/build-mirror-tarball.sh` pins CCM image to digest via
   `skopeo inspect`, writes into `imageset-config.yaml`'s
   `additionalImages` + records in `tag-mapping.tsv` so the tag form
   survives mirror push.
2. Tarball built once → uploaded to OSS → 04 pulls + extracts on the
   mirror ECS, oc-mirror v2 pushes blob to local Quay.
3. `04-prepare-mirror.yml` adds an IDMS rule:
   `registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager` →
   `<mirror_ip>:8443/acs/cloud-controller-manager`.  Masters never cross
   the border to Hangzhou — they pull from in-VPC mirror.
4. Gap-bridge: 04 also has an **idempotent pre-pull task** that runs on
   the mirror ECS — `if mirror lacks the image, pull from
   registry-cn-hangzhou.ack.aliyuncs.com (fast intra-Alibaba backbone)
   and push to mirror Quay`.  Covers the case where the tarball was
   built before CCM was added to the additionalImages list.

---

## 3. Cloud config schema (`alibaba-cloud-config` ConfigMap)

`v2.x` uses **JSON/YAML** (parsed via `sigs.k8s.io/yaml.Unmarshal` over
the `CloudConfig{}` struct in `pkg/config/cloud_config.go`).  The legacy
`v1.x` INI format (`[Global]\n...`) was dropped and is silently
incompatible — pod crashes immediately with `cannot unmarshal array
into Go value of type config.CloudConfig`.

ROS template `cluster-stack.yaml`'s `DynamicCustomManifest` output
generates this minimal config:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: alibaba-cloud-config
  namespace: alibaba-cloud-controller-manager
data:
  cloud.conf: |
    {
      "Global": {
        "clusterID": "aliocp1",
        "region":    "cn-wulanchabu",
        "vpcid":     "vpc-...",
        "zoneid":    "cn-wulanchabu-a",
        "vswitchid": "vsw-..."
      }
    }
```

**No AccessKeyID / AccessKeySecret** — CCM's default
`--enable-imdsv2=true` makes it read RAM-role credentials from the ECS
instance metadata service (100.100.100.200).  The role is created in
mirror-stack ROS (`mirror_node_ram_role`) and attached to cluster nodes
via the cluster-stack ROS template.

For the full available fields see
`pkg/config/cloud_config.go` upstream.  Anything not in our minimum
list has sensible defaults.

LoadBalancer behaviour that used to live under `[LoadBalancer]
address-type=intranet` is now per-Service via the annotation
`service.beta.kubernetes.io/alibaba-cloud-loadbalancer-address-type:
intranet`.

---

## 4. Flag set (`v2.x` vs `v1.x` / generic CCM template)

The v2.x binary has its own minimal flag set.  Many flags from the
generic kube-controller-manager / out-of-tree provider templates **do
not exist** and cause `unknown flag` errors:

| Flag                                          | v2.x? | Notes                                                                            |
|-----------------------------------------------|-------|----------------------------------------------------------------------------------|
| `--cloud-config=<path>`                       | ✓     | path to the JSON config above                                                    |
| `--cloud-provider=alibabacloud`               | ✗     | binary always targets alibabacloud; flag removed                                 |
| `--secure-port=10258`                         | ✗     | replaced by `--health-probe-bind-addr=:10258`                                    |
| `--use-service-account-credentials=true`      | ✗     | removed; auth is via RAM role + leader-elect via cluster API                     |
| `--configure-cloud-routes`                    | ✓     | **defaults to true**; pair with `--cluster-cidr` OR set to false (we choose false because OVN handles pod net) |
| `--health-probe-bind-addr=:10258`             | ✓     | default `:10258`                                                                 |
| `--leader-elect=true`                         | ✓     |                                                                                  |
| `--leader-elect-resource-namespace=<ns>`      | ✓     | default `kube-system`; we use `alibaba-cloud-controller-manager`                 |
| `--enable-imdsv2=true`                        | ✓     | default true; required for RAM-role auth                                         |
| `--controllers`                               | ✓     | default `[node,route,service,nlb]`                                               |
| `--network=vpc`                               | ✓     | default `vpc`                                                                    |
| `-v=2`                                        | ✓     |                                                                                  |

The binary entrypoint is `/cloud-controller-manager` (not
`/bin/alibaba-cloud-controller-manager` as some older manifests assume).

---

## 5. Trusted CA: do NOT mount `inject-trusted-cabundle` ConfigMap

Earlier iterations of the CCM manifest had:

```yaml
volumes:
  - name: trusted-ca
    configMap:
      name: ccm-trusted-ca   # has config.openshift.io/inject-trusted-cabundle: "true" annotation
      items: [{key: ca-bundle.crt, path: tls-ca-bundle.pem}]
```

This is the OCP **chicken-and-egg trap**:

```
CNO injects ca-bundle.crt into ccm-trusted-ca ──┐
                                                ↓
                              CCM pod can finally mount the ConfigMap
                                                ↓
                              CCM removes cloudprovider.uninitialized taint
                                                ↓
                              CNO Pod can finally be scheduled  ────┐
                                                ↓                    │
                              CNO starts and (eventually) injects ca-bundle.crt
                                                ↓                    │
                                                └────────────────────┘
                                       (deadlock — neither can start first)
```

Witnessed 2026-05-30 14:00 CST.  Fix: **don't mount this ConfigMap at
all**.  RHCOS ships with the Mozilla root CA bundle in
`/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem` which already
validates `*.aliyuncs.com` API endpoints (they use public Let's Encrypt
/ DigiCert certs).  No trusted-ca volume needed.

If a future release of CCM adds dependencies that need cluster proxy
CA (rare), put the inject-trusted-cabundle ConfigMap on **a different
operator** that doesn't gate node initialization.

---

## 6. Upgrading CCM versions

When kubernetes/cloud-provider-alibaba-cloud cuts a new release:

1. Bump `image:` tag in
   `custom_manifests/01-alibaba-ccm.yaml`.
2. Bump default `ALIBABA_CCM_IMAGE` in `scripts/build-mirror-tarball.sh`
   (or override per-build via `ALIBABA_CCM_IMAGE=...` env var).
3. Rebuild mirror tarball + upload to OSS.
4. On existing mirror ECS, 04 will pick up the new digest and push it
   via the idempotent pre-pull task.  No teardown needed if version
   compatible.
5. Check release notes for flag / config schema changes — v2.x line
   has been stable but new major could break our manifest.

For a sanity smoke test before rolling out widely:

```bash
podman pull registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager:vNEW
podman run --rm registry-cn-hangzhou.ack.aliyuncs.com/.../cloud-controller-manager:vNEW --help \
  | head -50
```

Confirm `/cloud-controller-manager` is the binary path and that our
flag set is still all recognized.

---

## 7. Landmines reference (in case any recur)

| Symptom                                                                 | Root cause                                                          | Fix                                                                              |
|-------------------------------------------------------------------------|---------------------------------------------------------------------|----------------------------------------------------------------------------------|
| CCM pod stuck `ContainerCreating` with "non-existent config key: ca-bundle.crt" | trusted-ca volume mounted before CNO injects                       | drop the volume (this project does)                                              |
| CCM `Error`: "failed to initialize certificate reloader: ... /etc/tls/private/tls.crt no such file" | service-ca-operator hasn't generated serving cert yet              | normal during early install; pod will retry; do not intervene                    |
| CCM `Error`: "unknown flag: --secure-port"                              | v2.x dropped the flag                                               | replace with `--health-probe-bind-addr=:10258`                                   |
| CCM `Error`: "--cluster-cidr must be set when --configure-cloud-routes=true" | route controller default-on with no CIDR                          | add `--configure-cloud-routes=false` (OVN handles routing)                       |
| CCM `Error`: "cannot unmarshal array into Go value of type config.CloudConfig" | cloud.conf still in INI format                                     | rewrite as JSON / YAML mapping (see schema above)                                |
| CCM `Error`: `/bin/alibaba-cloud-controller-manager: No such file`      | wrong binary path from older docs                                   | use `/cloud-controller-manager`                                                  |
| Image pull `manifest unknown` from registry.k8s.io                      | upstream doesn't publish there despite repo slot existing           | use registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager           |
| Other cluster operators all `Pending`, `FailedScheduling: untolerated taint cloudprovider.uninitialized` | CCM not running → never removes taint                              | fix CCM (this whole doc); or as one-shot workaround `kubectl taint nodes --all node.cloudprovider.kubernetes.io/uninitialized:NoSchedule-` |
