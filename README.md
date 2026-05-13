# OpenShift on Alibaba Cloud

Installing OpenShift Container Platform on Alibaba Cloud using the **External Platform** approach, modeled after [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift).

Supports two installation methods via the same ROS template — choose `InstallationMethod` when creating the stack.

---

## Architecture

### Assisted Installer

```
Step 0 — Prepare boot image (one-time prerequisite)
  ├── Download Discovery ISO from cloud.redhat.com
  ├── Upload ISO to Alibaba Cloud OSS
  └── Import ISO as custom ECS image → note the Image ID (m-bp1xxx...)

Step 1 — Red Hat Hybrid Cloud Console
  └── Create cluster, set name + base domain, paste InstallConfig (from Step 2 output)

Step 2 — Alibaba Cloud ROS  (InstallationMethod=Assisted, ImageId=<from Step 0>)
  └── ros-templates/create-cluster.yaml
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

Step 3 — Alibaba Cloud ROS  (InstallationMethod=Agent-based, ImageId=<from Step 2>)
  └── ros-templates/create-cluster.yaml
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
| Alibaba Cloud account | RAM admin permissions for ROS stack creation |
| Red Hat account | OpenShift subscription + pull secret |
| `oc` CLI | Any version compatible with your OCP release |
| `openshift-install` | Agent-based only — download from [mirror.openshift.com](https://mirror.openshift.com/pub/openshift-v4/clients/ocp/) |

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
2. Upload `ros-templates/create-cluster.yaml`
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
Region:        cn-hangzhou
ZoneId:        cn-hangzhou-h
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
2. Upload `ros-templates/create-cluster.yaml`
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
│   └── create-cluster.yaml              # Main infrastructure stack
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
| `Region` | `cn-hangzhou` | Alibaba Cloud region |
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
