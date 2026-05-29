# OpenShift on Alibaba Cloud

Installing OpenShift Container Platform on Alibaba Cloud using the **External Platform** approach, modeled after [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift).

Supports two installation methods via the same ROS template — choose `InstallationMethod` when creating the stack.

> **👉 想直接动手？**
> - **全自动（推荐）**：[`ansible/README.md`](ansible/README.md) —— 一条命令端到端，~90 分钟
> - **手动 / 调试**：[`QUICKSTART.md`](QUICKSTART.md) —— 五个 Phase 分步指导
> - **第一次跑 / 想理解每步原理**：[`docs/test-walkthrough.md`](docs/test-walkthrough.md) —— 含预期输出 + 故障排查
> - **China region / 跨境拉 quay.io 不稳**：[`docs/MIRROR.md`](docs/MIRROR.md) —— 私有 mirror 完整方案（架构 / 成本 / 工作流 / 故障排查）
>
> 本 README 是设计参考。

---

## Architecture

### Assisted Installer

```
Step 0 — Prepare boot image (one-time prerequisite)
  ├── Download Discovery ISO from cloud.redhat.com
  ├── Upload ISO to Alibaba Cloud OSS
  └── Import ISO as custom ECS image → note the Image ID (m-bp1xxx...)
  📖 Detailed walkthrough: docs/boot-image-import.md

Step 1 — Red Hat Hybrid Cloud Console
  └── Create cluster, set name + base domain, paste InstallConfig (from Step 2 output)

Step 2 — Alibaba Cloud ROS  (InstallationMethod=Assisted, ImageId=<from Step 0>)
  └── ros-templates/create-cluster-LEGACY.yaml
      ├── VPC / VSwitch (×3) / NAT / EIP / SNAT
      ├── Security Groups (master + worker, incl. 80/443 for Ingress)
      ├── RAM Role + Policy (Instance Principal — no AK/SK)
      ├── API Server SLB (intranet) + listeners (6443, 22623)
      ├── PrivateZone DNS  api.* / api-int.* → SLB
      ├── ECS: bootstrap + master-1/2/3 + workers
      ├── SLB backend attachment: master-1/2/3 → ApiSLB
      └── Outputs: InstallConfig / DynamicCustomManifest

Step 3 — Red Hat Hybrid Cloud Console
  ├── Discovery agents appear as nodes boot
  ├── Assign node roles (3 masters + N workers)
  ├── Upload Custom Manifests (3 files):
  │     ├── alibaba-ccm-config.yaml        ← ROS DynamicCustomManifest output
  │     ├── custom_manifests/01-alibaba-ccm.yaml
  │     └── custom_manifests/03-machineconfig-providerid.yaml
  └── Start installation

Post-install:
  oc apply -f custom_manifests/02-capa-crds.yaml
  oc apply -f custom_manifests/02-capa-controller.yaml
  # Add *.apps DNS record → Ingress SLB IP (CCM creates it automatically)
```

### Agent-based Installer

```
Step 0 — Decide parameters first (no chicken-and-egg)
  ├── Choose: ClusterName, BaseDomain, Region, ZoneId, RendezvousIp (default 10.0.16.5)
  └── These values are needed for both the ISO and the ROS stack

Step 1 — Generate Agent ISO locally
  ├── mkdir -p install-dir/openshift
  ├── Write install-config.yaml  (use chosen ClusterName / BaseDomain)
  ├── Write agent-config.yaml    (rendezvousIP: <RendezvousIp from Step 0>)
  ├── Copy manifests into openshift/:
  │     ├── 01-alibaba-ccm.yaml
  │     └── 03-machineconfig-providerid.yaml
  │   (alibaba-ccm-config.yaml not yet available — add after Step 2)
  └── openshift-install agent create image --dir install-dir/
      → install-dir/agent.x86_64.iso

Step 2 — Prepare boot image
  ├── Upload agent.x86_64.iso to Alibaba Cloud OSS
  └── Import ISO as custom ECS image → note the Image ID (m-bp1xxx...)
  📖 Detailed walkthrough: docs/boot-image-import.md

Step 3 — Alibaba Cloud ROS  (InstallationMethod=Agent-based, ImageId=<from Step 2>)
  └── ros-templates/create-cluster-LEGACY.yaml
      ├── Same infrastructure as Assisted
      ├── master-1 (RendezvousInstance) gets fixed private IP = RendezvousIp
      └── Outputs: InstallConfig / AgentConfig / DynamicCustomManifest

Step 4 — Finalize install directory & monitor
  ├── Save ROS DynamicCustomManifest → install-dir/openshift/alibaba-ccm-config.yaml
  ├── Verify agent-config.yaml rendezvousIP matches ROS RendezvousIp parameter
  ├── openshift-install agent wait-for bootstrap-complete --dir install-dir/
  └── openshift-install agent wait-for install-complete --dir install-dir/

Post-install:
  oc apply -f custom_manifests/02-capa-crds.yaml
  oc apply -f custom_manifests/02-capa-controller.yaml
  # Add *.apps DNS record → Ingress SLB IP (CCM creates it automatically)
```

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

## Two flows: monolithic vs split (mirror/cluster decoupled)

This repo ships two flows for provisioning the infrastructure.

### Flow A — Monolithic (legacy, single ROS stack)

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

# teardown:
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_from=3 -e teardown_confirmed=true
```

### Flow B — Split (mirror-stack + cluster-stack)

Two ROS stacks: `mirror-stack.yaml` (VPC, RAM, jump host, mirror
ECS — persistent) and `cluster-stack.yaml` (SGs, NLB, PrivateZone,
masters, workers — short-lived).  Cluster-stack reads mirror-stack
outputs (VpcId, VSwitches, NodeRamRoleName, JumpHostSGId, MirrorIp)
as parameters.

**Tearing down the cluster does not touch the mirror**, so the next
install skips the 30-min mirror prep entirely.

```bash
# One-time:
ansible-playbook ansible/playbooks/00-preflight.yml
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml    # ← persistent
ansible-playbook ansible/playbooks/04-prepare-mirror.yml         # ← one-time
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml    # ← short-lived
ansible-playbook ansible/playbooks/07-install-cluster.yml

# Cluster rebuild only (mirror survives, ~30 min faster):
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=cluster -e teardown_confirmed=true
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

> **完整 teardown 参考**：模式矩阵 / 互斥规则 / state 字段处置 / 典型场景 / 故障排查
> → [`docs/TEARDOWN.md`](docs/TEARDOWN.md)
> 包含 `teardown_target` × `teardown_preserves_ai` × `delete_mirror_snapshots` 所有合法组合。
> snapshot-restore 场景下用 `-e teardown_preserves_ai=true` 可把重建时间压到 ~10 min。

### Which to pick?

| Use case | Flow |
|---|---|
| Single proof-of-concept install | A (monolithic) |
| Iterating on cluster install (frequent teardown + rebuild) | **B (split)** |
| Sharing one mirror across multiple clusters | B (split, with distinct ClusterName per cluster-stack) |
| Limited Aliyun RAM permissions | A — split needs both RAM full access scopes |

The two flows write distinct fields in `state.yml`
(`ros_stack_id` for A vs `mirror_stack_id` + `cluster_stack_id` for
B) so they do not collide; teardown playbooks match accordingly
(`99-teardown.yml` for A, `99-teardown.yml` for B).

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

## Step-by-Step: Assisted Installer

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

## Step-by-Step: Agent-based Installer

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
│   └── create-cluster-LEGACY.yaml       # Legacy monolithic stack
│                                        # Key output: DynamicCustomManifest
│                                        #   → save as alibaba-ccm-config.yaml
├── custom_manifests/
│   ├── 01-alibaba-ccm.yaml              # CCM: SA, RBAC, Deployment  [install-time]
│   ├── 02-capa-crds.yaml                # CAPI CRD definitions       [post-install]
│   ├── 02-capa-controller.yaml          # CAPA controller            [post-install]
│   └── 03-machineconfig-providerid.yaml # kubelet ProviderID         [install-time]
└── docs/
    ├── design-and-development-summary.md
    └── validation-checklist.md
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
