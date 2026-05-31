# OpenShift on Alibaba Cloud

Installing OpenShift Container Platform on Alibaba Cloud using the **External Platform** approach, modeled after [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift).

Supports two installation methods via the same ROS template — choose `InstallationMethod` when creating the stack.

> **👉 想直接动手？**
> - **全自动（推荐）**：[`ansible/README.md`](ansible/README.md) —— 一条命令端到端，~90 分钟
> - **手动 / 调试**：[`QUICKSTART.md`](QUICKSTART.md) —— 五个 Phase 分步指导
> - **第一次跑 / 想理解每步原理**：[`docs/test-walkthrough.md`](docs/test-walkthrough.md) —— 含预期输出 + 故障排查
> - **China region / 跨境拉 quay.io 不稳**：[`docs/MIRROR.md`](docs/MIRROR.md) —— 私有 mirror 完整方案（架构 / 成本 / 工作流 / 故障排查）
> - **Alibaba CCM（platform=external 契约 / image / config / 升级）**：[`docs/CCM.md`](docs/CCM.md) —— CCM 设计 + 踩坑表
> - **Teardown 模式**（cluster / mirror / both / 各种 flag）：[`docs/TEARDOWN.md`](docs/TEARDOWN.md)
> - **Bootstrap reboot / 节点挂住怎么恢复**（clone-vdb-to-vda hook、ReplaceSystemDisk vs RebootForce）：[`docs/bootstrap-reboot.md`](docs/bootstrap-reboot.md)
>
> 本 README 是设计参考。

---

## Architecture

The current recommended deployment uses **two decoupled ROS stacks** driven by
the Ansible Phase 00–08 playbooks.  A legacy single-stack flow is preserved
for reference (see "Legacy monolithic flow" below).

```
┌────────────────────────────────────────────────────────────────────────┐
│ Persistent — ros-templates/mirror-stack.yaml                            │
│ (created once by Phase 03; survives cluster rebuilds)                   │
│                                                                         │
│   VPC + VSwitches (×2) + NAT + EIP                                      │
│   NodeRamRole (Instance Principal — no AK/SK on nodes)                  │
│   Optional jump host (SSH bastion) + JumpHost SG                        │
│   Mirror ECS (Quay + Postgres + Redis) + 200 GB cloud_essd data disk    │
│                                                                         │
│   Outputs consumed by cluster-stack:                                    │
│     VpcId, PrivateVSwitchId{,2}, NodeRamRoleName,                       │
│     JumpHostSecurityGroupId, MirrorIp                                   │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Short-lived — ros-templates/cluster-stack.yaml                          │
│ (created by Phase 06; torn down freely with teardown_target=cluster)    │
│                                                                         │
│   Control-plane + worker SecurityGroups (+ cross-SG ingress)            │
│   API NLB + ServerGroups + Listeners (6443 / 22623)                     │
│   PrivateZone (api / api-int / *.apps / etcd-N) + reverse zones         │
│   3 × master ECS  + optional worker InstanceGroup                       │
│                                                                         │
│   Outputs: ApiLBEndpoint, master IPs, DynamicCustomManifest             │
└────────────────────────────────────────────────────────────────────────┘

Phase pipeline (driven by `ansible/playbooks/`, run via `site.yml` or one by one):

  00-preflight             sanity-check tooling, RAM, NLB service-linked role
  01-prepare-iso           download Discovery ISO + upload to OSS
  02-import-image          ImportImage → custom ECS image
  03-create-mirror-stack   create the persistent mirror-stack (fast-path:
                           restore from prior snapshot + custom image)
  04-prepare-mirror        oc-mirror d2m → push to Quay (idempotent, re-runnable)
  05-verify-mirror         smoke-test pulls + snapshot mirror vda + vdb
  06-create-cluster-stack  create the short-lived cluster-stack
  07-install-cluster       drive AI: register infra-env, boot nodes, inject mirror
                           CA + custom manifests (CCM, OVN MTU, ProviderID MCs),
                           wait for install-complete
  08-deploy-post-install   apply CAPA + verify Ingress SLB DNS

  99-teardown              tears down by `teardown_target=cluster|mirror|both`
                           (full matrix in docs/TEARDOWN.md)
```

The default `mirror_enabled: true` path uses the private mirror (China region
/ unreliable cross-border egress).  Set `mirror_enabled: false` to make
Phase 03/04/05 no-op and have nodes pull from `quay.io` / `registry.redhat.io`
directly — useful for outside-China test installs.

For the **manual / console-driven Assisted or Agent-based flow** (no Ansible,
single legacy `create-cluster-LEGACY.yaml` stack), see "Legacy monolithic flow"
and the step-by-step sections lower in this file.

---

## Differences from OCI Approach

| Feature | OCI | Alibaba Cloud |
|---------|-----|---------------|
| IaC tool | Terraform (BSL licence) | ROS (native, no licence issues) |
| Installation method | Assisted or Agent-based | Assisted or Agent-based |
| Node auto-scaling | Manual Terraform `add-nodes` | **CAPA MachineDeployment** (automatic) |
| CCM auth | Instance Principal | RAM Role on ECS (equivalent) |
| Dynamic Manifest | Terraform output | ROS `DynamicCustomManifest` output |
| Ingress SLB | Manual | CCM-managed (auto-created for LoadBalancer Services) |

---

## Prerequisites

| Item | Notes |
|------|-------|
| Alibaba Cloud account | RAM user with the managed policies listed below |
| Red Hat account | OpenShift subscription + pull secret |
| `oc` CLI | Any version compatible with your OCP release |
| `openshift-install` | Agent-based only — download from [mirror.openshift.com](https://mirror.openshift.com/pub/openshift-v4/clients/ocp/) |

### Required RAM managed policies

Attach all of these to the RAM user whose AK/SK lives in `~/.aliyun/config.json`
(the `aliyun_profile` in `group_vars/all.yml`).  `AliyunNLBFullAccess` is
easy to miss because NLB is a separate service from classic SLB — without
it Phase 03 fails with `Forbidden.NoPermission` when ROS tries to create
the API load balancer.

| Managed policy | Used by |
|---|---|
| `AliyunECSFullAccess` | ROS provisions ECS instances + disks; ImportImage |
| `AliyunVPCFullAccess` | VPC / VSwitch / NAT / EIP / SG |
| `AliyunNLBFullAccess` | **API server load balancer (NLB, not classic SLB)** |
| `AliyunSLBFullAccess` | CCM-managed LoadBalancer Services (post-install) |
| `AliyunRAMFullAccess` | ROS creates node RAM role + attaches policy |
| `AliyunROSFullAccess` | Stack CRUD |
| `AliyunPvtzFullAccess` | PrivateZone (`api`, `api-int`, `*.apps`, etcd records) |
| `AliyunOSSFullAccess` | Upload Discovery ISO + mirror tarball |
| `AliyunSTSAssumeRoleAccess` | Required by ImportImage's service-linked role |

Shortcut for non-production accounts: `AdministratorAccess` covers all of the
above.  For least-privilege, attach the individual policies.

### Mirror registry sizing (when `mirror_enabled: true`)

The mirror ECS hosts Quay + Postgres + Redis + oc-mirror's d2m staging
cache.  Peak runtime memory is 6-8 GB on top of Quay's ~3 GB resident
set; peak data-disk usage is ~100 GB during the brief window when the
downloaded tarball, the extracted chunks, the d2m cache, and Quay's
own datastorage all coexist.

| Resource | Minimum | Default in `mirror-stack.yaml` |
|---|---|---|
| Instance type | `ecs.g7.xlarge` (4 vCPU / **16 GB**) | `ecs.g7.xlarge` |
| Data disk | 200 GB cloud_essd | 200 GB |

Smaller types (`g7.large` 8 GB, `c7.large` 4 GB) get OOM-killed
during the chunk-extract phase of `oc-mirror v2 d2m`.  See
[docs/MIRROR.md → "附录 A — oc-mirror v1 → v2 迁移踩坑笔记"](docs/MIRROR.md#附录-a--oc-mirror-v1--v2-迁移踩坑笔记2026-05)
for the full failure-mode table and recovery cookbook.

---

## Two flows: split (recommended) vs monolithic (legacy)

This repo ships two flows for provisioning the infrastructure.

### Flow A — Split (mirror-stack + cluster-stack)  ← recommended

Two ROS stacks: `mirror-stack.yaml` (VPC, RAM, jump host, mirror
ECS — persistent) and `cluster-stack.yaml` (SGs, NLB, PrivateZone,
masters, workers — short-lived).  Cluster-stack reads mirror-stack
outputs (VpcId, VSwitches, NodeRamRoleName, JumpHostSGId, MirrorIp)
as parameters.

**Tearing down the cluster does not touch the mirror**, so the next
install skips the ~30 min mirror prep entirely.

```bash
# One-time:
ansible-playbook ansible/playbooks/00-preflight.yml
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml    # ← persistent
ansible-playbook ansible/playbooks/04-prepare-mirror.yml         # ← one-time
ansible-playbook ansible/playbooks/05-verify-mirror.yml          # ← + snapshots
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml   # ← short-lived
ansible-playbook ansible/playbooks/07-install-cluster.yml
ansible-playbook ansible/playbooks/08-deploy-post-install.yml

# Cluster rebuild only (mirror survives, ~30 min faster):
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=cluster -e teardown_confirmed=true
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

### Flow B — Monolithic (legacy, single ROS stack)

One `create-cluster-LEGACY.yaml` stack contains everything (VPC + RAM +
optional jump host + mirror ECS + masters + NLB + PrivateZone).
Simpler to reason about; **tearing the cluster down also tears down
the mirror**, so each cluster rebuild re-runs the ~30 min mirror
preparation.

```bash
ansible-playbook ansible/playbooks/00-preflight.yml
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-stack-LEGACY.yml          # ← monolithic stack
ansible-playbook ansible/playbooks/04-prepare-mirror.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml

# teardown (uses the LEGACY-specific teardown playbook + teardown_from index):
ansible-playbook ansible/playbooks/99-teardown-LEGACY.yml \
  -e teardown_from=3 -e teardown_confirmed=true
```

> **完整 teardown 参考**：模式矩阵 / 互斥规则 / state 字段处置 / 典型场景 / 故障排查
> → [`docs/TEARDOWN.md`](docs/TEARDOWN.md)
> 包含 `teardown_target` × `teardown_preserves_ai` × `delete_mirror_snapshots` 所有合法组合。
> snapshot-restore 场景下用 `-e teardown_preserves_ai=true` 可把重建时间压到 ~10 min。

### Which to pick?

| Use case | Flow |
|---|---|
| Iterating on cluster install (frequent teardown + rebuild) | **A (split, recommended)** |
| Sharing one mirror across multiple clusters | A (split, with distinct ClusterName per cluster-stack) |
| One-shot proof-of-concept install | B (monolithic) |
| Limited Aliyun RAM permissions | B — split needs both RAM full access scopes |

The two flows write distinct fields in `state.yml`
(`mirror_stack_id` + `cluster_stack_id` for A vs `ros_stack_id` for B)
so they do not collide; teardown playbooks match accordingly
(`99-teardown.yml` for A, `99-teardown-LEGACY.yml` for B).

### One-time NLB service-linked role

If you've never used NLB on this account before, create its service-linked
role once — otherwise Phase 03 fails with `OperationDenied.ServiceLinkedRoleNotExist`:

```bash
aliyun resourcemanager CreateServiceLinkedRole \
  --ServiceName nlb.aliyuncs.com \
  --endpoint resourcemanager.aliyuncs.com \
  --profile <your-profile>
```

Phase 00 (`00-preflight.yml`) checks for this role and fails fast with the
exact command above if it's missing.

---

## Legacy monolithic flow — manual / console-driven

> The two sections below document the **legacy single-stack flow** driven by
> hand from the Red Hat Hybrid Cloud Console + Alibaba ROS Console, using
> `ros-templates/create-cluster-LEGACY.yaml`.  They are kept for reference
> and for users who cannot run Ansible.
>
> For the **recommended path** (Ansible Phase 00–08 against the split
> mirror-stack + cluster-stack), see [`ansible/README.md`](ansible/README.md)
> and the Architecture section above.

## Step-by-Step (legacy): Assisted Installer

### Step 0 — Import Discovery ISO as Custom ECS Image

> **This step is required before creating the ROS stack.**  
> ECS instances use an Alibaba Cloud image ID, not a URL.

1. Go to [cloud.redhat.com/openshift](https://cloud.redhat.com/openshift) → **Create cluster** → **Assisted Installer**
2. Set cluster name and base domain — note them down (they go into the ROS parameters too)
3. Download the **Discovery ISO**
4. Upload the ISO file to an Alibaba Cloud OSS bucket
5. In the ECS Console: **Images → Import Image**
   - OSS Object URL: your uploaded ISO
   - Architecture: `x86_64`
   - OS Type: `Linux`
   - Image Format: `ISO`
6. Wait for the import to complete (5–15 minutes)
7. Copy the resulting **Image ID** (e.g. `m-bp1xxxxxxxxxxxxxxxxx`)

### Step 1 — Provision Infrastructure

1. Open **Alibaba Cloud ROS Console** → Create Stack
2. Upload `ros-templates/create-cluster-LEGACY.yaml`
3. Fill in parameters:

   | Parameter | Value |
   |-----------|-------|
   | `ClusterName` | Same as Red Hat console |
   | `BaseDomain` | Your base domain (e.g. `example.com`) |
   | `InstallationMethod` | `Assisted` |
   | `ImageId` | Image ID from Step 0 |
   | `Region` | Target region |
   | `ZoneId` / `ZoneId2` | Two AZs in the region |
   | Instance types | Adjust for your workload |

4. Create the stack — wait ~10 minutes for completion
5. From the **Outputs** tab:
   - `InstallConfig` → paste into Red Hat console (replaces the console-generated one)
   - `DynamicCustomManifest` → save as `alibaba-ccm-config.yaml`

### Step 2 — Complete Installation in Red Hat Console

1. Discovery agents will appear in the console as nodes boot (~5 minutes)
2. Assign roles: **3 masters** + N workers
3. Go to **Custom manifests** → upload these **3 files**:

   | File | Source |
   |------|--------|
   | `alibaba-ccm-config.yaml` | ROS `DynamicCustomManifest` output |
   | `01-alibaba-ccm.yaml` | `custom_manifests/01-alibaba-ccm.yaml` in this repo |
   | `03-machineconfig-providerid.yaml` | `custom_manifests/03-machineconfig-providerid.yaml` in this repo |

4. Click **Start installation** — takes ~45 minutes

### Post-Installation

```bash
# Apply CAPA controller (enables node auto-scaling)
oc apply -f custom_manifests/02-capa-crds.yaml
oc apply -f custom_manifests/02-capa-controller.yaml

# Get Ingress SLB IP (CCM creates it automatically for the ingress-operator Service)
oc get svc -n openshift-ingress router-default -o jsonpath='{.status.loadBalancer.ingress[0].ip}'

# Add wildcard DNS record in Alibaba Cloud PrivateZone (or your public DNS):
# *.apps.<ClusterName>.<BaseDomain>  →  <Ingress SLB IP>
```

---

## Step-by-Step (legacy): Agent-based Installer

### Step 0 — Decide Parameters

Choose values for all parameters **before** generating the ISO — they must match between the ISO and the ROS stack:

```
ClusterName:   my-cluster
BaseDomain:    example.com
Region:        cn-wulanchabu
ZoneId:        cn-wulanchabu-a
RendezvousIp:  10.0.16.5   (must be within PrivateSubnetCidr 10.0.16.0/20)
```

### Step 1 — Generate Agent ISO

```bash
mkdir -p install-dir/openshift

# install-config.yaml — fill in pullSecret and sshKey
cat > install-dir/install-config.yaml <<'EOF'
apiVersion: v1
metadata:
  name: my-cluster
baseDomain: example.com
networking:
  networkType: OVNKubernetes
  clusterNetwork:
    - cidr: 10.128.0.0/14
      hostPrefix: 23
  machineNetwork:
    - cidr: 10.0.0.0/16
  serviceNetwork:
    - 172.30.0.0/16
compute:
  - name: worker
    replicas: 3
controlPlane:
  name: master
  replicas: 3
platform:
  external:
    platformName: AlibabaCloud
    cloudControllerManager: External
pullSecret: '<your-pull-secret>'
sshKey: '<your-ssh-public-key>'
EOF

# agent-config.yaml — rendezvousIP must match RendezvousIp ROS parameter
cat > install-dir/agent-config.yaml <<'EOF'
apiVersion: v1alpha1
kind: AgentConfig
metadata:
  name: my-cluster
rendezvousIP: 10.0.16.5
hosts: []
EOF

# Copy static manifests (baked into the ISO)
cp custom_manifests/01-alibaba-ccm.yaml install-dir/openshift/
cp custom_manifests/03-machineconfig-providerid.yaml install-dir/openshift/
# Note: alibaba-ccm-config.yaml is added AFTER the ROS stack outputs are available

# Generate the agent ISO
openshift-install agent create image --dir install-dir/
# Output: install-dir/agent.x86_64.iso
```

### Step 2 — Import Agent ISO as Custom ECS Image

1. Upload `install-dir/agent.x86_64.iso` to Alibaba Cloud OSS
2. In the ECS Console: **Images → Import Image** (same steps as Assisted Step 0)
3. Copy the resulting **Image ID**

### Step 3 — Provision Infrastructure

1. Open **Alibaba Cloud ROS Console** → Create Stack
2. Upload `ros-templates/create-cluster-LEGACY.yaml`
3. Fill in parameters — use the **exact same values** chosen in Step 0:

   | Parameter | Value |
   |-----------|-------|
   | `InstallationMethod` | `Agent-based` |
   | `ImageId` | Image ID from Step 2 |
   | `RendezvousIp` | Same as agent-config.yaml |
   | Other params | Same as Step 0 |

4. Create the stack — nodes boot automatically from the agent ISO

### Step 4 — Add Dynamic Manifest & Monitor

```bash
# Save CCM ConfigMap from ROS Outputs → DynamicCustomManifest
# (contains actual Region/VPC/VSwitch values from the stack)
# Save it as:
cp /path/to/ros-output install-dir/openshift/alibaba-ccm-config.yaml

# Monitor installation
openshift-install agent wait-for bootstrap-complete --dir install-dir/
openshift-install agent wait-for install-complete --dir install-dir/
```

### Post-Installation

```bash
oc apply -f custom_manifests/02-capa-crds.yaml
oc apply -f custom_manifests/02-capa-controller.yaml

# Get Ingress SLB IP and add *.apps DNS record (same as Assisted post-install)
oc get svc -n openshift-ingress router-default -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

---

## Node Auto-Scaling (CAPA)

Unlike the OCI approach (manual Terraform `add-nodes`), this implementation uses **Cluster API** for automatic scaling. See `examples/capi-machinedeployment.yaml` for a complete example.

```bash
# Scale workers
oc scale machinedeployment <cluster>-workers --replicas=5 -n openshift-cluster-api
```

---

## Repository Structure

```
alibaba-openshift/
├── ros-templates/
│   ├── mirror-stack.yaml                       # Split: persistent (VPC + RAM + jump + mirror ECS)
│   ├── cluster-stack.yaml                      # Split: short-lived (SGs + NLB + DNS + masters/workers)
│   └── create-cluster-LEGACY.yaml              # Legacy monolithic stack (single-stack flow)
│                                               #   Key output: DynamicCustomManifest
│                                               #     → save as alibaba-ccm-config.yaml
├── custom_manifests/
│   ├── 00-ovn-mtu.yaml                         # OVN geneve MTU pin              [install-time]
│   ├── 01-alibaba-ccm.yaml                     # CCM: SA, RBAC, Deployment       [install-time]
│   ├── 02-capa-crds.yaml                       # CAPI CRD definitions            [post-install]
│   ├── 02-capa-controller.yaml                 # CAPA controller                 [post-install]
│   ├── 03-machineconfig-providerid-master.yaml # kubelet ProviderID (master)     [install-time]
│   ├── 03-machineconfig-providerid-worker.yaml # kubelet ProviderID (worker)     [install-time]
│   ├── 04-csi-*.yaml                           # Alibaba Cloud CSI driver        [post-install]
│   ├── 05-oadp-*.yaml                          # OADP / backup (optional)        [post-install]
│   └── butane/                                 # Butane sources for the MCs above
├── ansible/
│   ├── playbooks/
│   │   ├── 00-preflight.yml … 08-deploy-post-install.yml   # split-flow phases
│   │   ├── 03-create-stack-LEGACY.yml                      # legacy monolithic create
│   │   ├── 99-teardown.yml          # split-flow teardown (teardown_target=…)
│   │   ├── 99-teardown-LEGACY.yml   # legacy teardown (teardown_from=…)
│   │   ├── mirror-rebuild.yml       # refresh mirror contents without touching cluster
│   │   └── site.yml                 # runs Phase 00→07 sequentially
│   └── tasks/                       # shared task includes (load_state, create_stack,
│                                    # mirror_defaults, refresh_tag_mapping, …)
├── docs/
│   ├── MIRROR.md                    # private mirror architecture + oc-mirror v2 cookbook
│   ├── TEARDOWN.md                  # teardown_target × _preserves_ai × snapshot matrix
│   ├── CCM.md                       # Alibaba CCM design + lessons learned
│   ├── csi-driver-design.md         # CSI driver layout (matches 04-csi-*.yaml)
│   ├── boot-image-import.md         # ISO → custom ECS image walkthrough
│   ├── test-walkthrough.md          # end-to-end test run with expected output
│   └── legacy/                      # frozen reference docs (design summary,
│                                    # 2026-05 validation checklist) — see
│                                    # docs/legacy/README.md for index
└── scripts/                         # legacy bash deployment scripts (RHEL8-only;
                                     # kept for reference — Ansible is the active path)
```

---

## ROS Template Parameters Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ClusterName` | — | OpenShift cluster name; must match Red Hat console |
| `BaseDomain` | — | Base domain (e.g. `example.com`) |
| `Region` | `cn-wulanchabu` | Alibaba Cloud region |
| `ZoneId` | — | Primary AZ (masters 1/2 + workers) |
| `ZoneId2` | — | Secondary AZ (master 3) |
| `ImageId` | — | Custom ECS image ID from imported ISO |
| `InstallationMethod` | `Assisted` | `Assisted` or `Agent-based` |
| `RendezvousIp` | `10.0.16.5` | Agent-based only: fixed IP for master-1 |
| `RamRoleName` | `openshift-node-role` | RAM Role name for instance principal |
| `VpcCidr` | `10.0.0.0/16` | VPC CIDR |
| `PrivateSubnetCidr` | `10.0.16.0/20` | Primary private subnet (masters 1/2 + workers) |
| `PrivateSubnetCidr2` | `10.0.32.0/20` | Secondary private subnet (master 3) |
| `ControlPlaneInstanceType` | `ecs.c6.2xlarge` | Master node instance type |
| `ComputeInstanceType` | `ecs.c6.xlarge` | Worker node instance type |
| `SystemDiskCategory` | `cloud_essd` | Disk type for all nodes |
| `SystemDiskSize` | `120` | System disk size in GiB |
