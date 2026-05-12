# OpenShift on Alibaba Cloud

Installing OpenShift Container Platform on Alibaba Cloud using the **External Platform** approach, modeled after [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift).

Supports two installation methods via the same ROS template — choose `InstallationMethod` when creating the stack:

## Architecture

### Assisted Installer

```
Part 1: Red Hat Hybrid Cloud Console
  └── Generate Discovery ISO → upload to OSS

Part 2: Alibaba Cloud ROS (InstallationMethod=Assisted)
  └── run ros-templates/create-cluster.yaml
      ├── VPC / VSwitch / NAT / EIP
      ├── Security Groups
      ├── RAM Role + Policy (Instance Principal — no AK/SK needed)
      ├── API Server SLB (internal)
      ├── PrivateZone DNS (api.* / api-int.*)
      ├── ECS Instances (bootstrap + masters + workers)
      └── Outputs: InstallConfig / DynamicCustomManifest

Part 3: Red Hat Hybrid Cloud Console
  ├── Assign node roles
  ├── Upload Custom Manifests (3 files):
  │     ├── alibaba-ccm-config.yaml          # from ROS DynamicCustomManifest output
  │     ├── custom_manifests/01-alibaba-ccm.yaml
  │     └── custom_manifests/03-machineconfig-providerid.yaml
  └── Start installation

Post-install (CAPA only):
  oc apply -f custom_manifests/02-capa-crds.yaml
  oc apply -f custom_manifests/02-capa-controller.yaml
```

### Agent-based Installer

```
Part 1: Alibaba Cloud ROS (InstallationMethod=Agent-based)
  └── run ros-templates/create-cluster.yaml (DiscoveryIsoUrl=placeholder)
      ├── Same infrastructure as Assisted
      ├── RendezvousInstance (master-1) gets fixed private IP
      └── Outputs: InstallConfig / AgentConfig / DynamicCustomManifest

Part 2: Local workstation
  ├── mkdir install-dir/openshift
  ├── Save ROS InstallConfig  → install-dir/install-config.yaml
  ├── Save ROS AgentConfig    → install-dir/agent-config.yaml
  ├── Save ROS DynamicCustomManifest → install-dir/openshift/alibaba-ccm-config.yaml
  ├── Copy custom_manifests/01-alibaba-ccm.yaml → install-dir/openshift/
  ├── Copy custom_manifests/03-machineconfig-providerid.yaml → install-dir/openshift/
  └── openshift-install agent create image --dir install-dir/
      → generates install-dir/agent.x86_64.iso

Part 3: Upload & boot
  ├── Upload agent.x86_64.iso to OSS → get pre-authenticated URL
  ├── Update ROS stack: set DiscoveryIsoUrl to agent ISO URL → re-apply
  │   (or redeploy ECS instances with new ImageId)
  └── openshift-install agent wait-for install-complete --dir install-dir/

Post-install (CAPA only):
  oc apply -f custom_manifests/02-capa-crds.yaml
  oc apply -f custom_manifests/02-capa-controller.yaml
```

## Differences from OCI approach

| Feature | OCI | Alibaba Cloud |
|---------|-----|---------------|
| IaC tool | Terraform via OCI Resource Manager | ROS (native, no license issue) |
| Installation method | Assisted or Agent-based (same stack) | Assisted or Agent-based (same stack) |
| Node auto-scaling | Manual Terraform `add-nodes` | **CAPA MachineSet** (automatic) |
| CCM auth | Instance Principal | RAM Role bound to ECS (equivalent) |
| CCM lifecycle | Direct manifest | Direct manifest |

## Prerequisites

- Alibaba Cloud account with RAM admin permissions
- Red Hat account with OpenShift subscription
- `oc` CLI
- Agent-based only: `openshift-install` binary (download from [mirror.openshift.com](https://mirror.openshift.com/pub/openshift-v4/clients/ocp/))

## Installation Steps — Assisted Installer

### Part 1 — Generate Discovery ISO

1. Go to [Red Hat Hybrid Cloud Console](https://console.redhat.com/openshift)
2. Create a new cluster → **Assisted Installer**
3. Set cluster name and base domain (must match ROS template parameters)
4. Download the Discovery ISO
5. Upload the ISO to Alibaba Cloud OSS and create a pre-authenticated URL

### Part 2 — Provision Infrastructure

1. Open **Alibaba Cloud ROS Console** → Create Stack
2. Upload `ros-templates/create-cluster.yaml`
3. Fill in parameters:
   - `ClusterName`: same as Red Hat console
   - `BaseDomain`: your base domain
   - `InstallationMethod`: `Assisted`
   - `DiscoveryIsoUrl`: OSS pre-authenticated URL from Part 1
   - Region, zones, instance types as needed
4. Create the stack and wait for completion (~10 minutes)
5. From the **Outputs** tab:
   - `InstallConfig` → paste into Red Hat console
   - `DynamicCustomManifest` → copy and save as `alibaba-ccm-config.yaml`

### Part 3 — Complete Installation

1. In Red Hat Hybrid Cloud Console, discovery agents will appear as nodes boot
2. Assign roles: 3 masters + N workers
3. In **Custom manifests**, upload these three files:
   - `alibaba-ccm-config.yaml` — from ROS `DynamicCustomManifest` output (CCM cloud.conf with real values)
   - `custom_manifests/01-alibaba-ccm.yaml` — CCM ServiceAccount, RBAC, Deployment
   - `custom_manifests/03-machineconfig-providerid.yaml` — kubelet ProviderID (must be install-time)
4. Start the installation

### Post-Installation

```bash
oc apply -f custom_manifests/02-capa-crds.yaml
oc apply -f custom_manifests/02-capa-controller.yaml
```

---

## Installation Steps — Agent-based Installer

### Part 1 — Provision Infrastructure (first pass, placeholder ISO)

1. Open **Alibaba Cloud ROS Console** → Create Stack
2. Upload `ros-templates/create-cluster.yaml`
3. Fill in parameters:
   - `ClusterName`, `BaseDomain`, Region, zones as needed
   - `InstallationMethod`: `Agent-based`
   - `RendezvousIp`: fixed IP for the first master (default `10.0.16.5`, must be in `PrivateSubnetCidr`)
   - `DiscoveryIsoUrl`: any placeholder value (ECS instances will be restarted after ISO is ready)
4. Create the stack — infrastructure provisions but nodes won't install yet
5. From the **Outputs** tab, save:
   - `InstallConfig` → `install-dir/install-config.yaml` (fill in `pullSecret` and `sshKey`)
   - `AgentConfig` → `install-dir/agent-config.yaml`
   - `DynamicCustomManifest` → `install-dir/openshift/alibaba-ccm-config.yaml`

### Part 2 — Generate Agent ISO

```bash
# Prepare install directory
mkdir -p install-dir/openshift

# Copy static manifests into openshift/ (baked into the ISO)
cp custom_manifests/01-alibaba-ccm.yaml install-dir/openshift/
cp custom_manifests/03-machineconfig-providerid.yaml install-dir/openshift/
# alibaba-ccm-config.yaml already saved from ROS output above

# Generate the agent ISO
openshift-install agent create image --dir install-dir/
# Produces: install-dir/agent.x86_64.iso
```

### Part 3 — Boot & Complete Installation

```bash
# Upload ISO to OSS and get a pre-authenticated URL
# Update the ROS stack's DiscoveryIsoUrl parameter to the agent ISO URL,
# then restart ECS instances so they boot from the agent ISO.

# Monitor installation progress
openshift-install agent wait-for bootstrap-complete --dir install-dir/
openshift-install agent wait-for install-complete --dir install-dir/
```

### Post-Installation

```bash
oc apply -f custom_manifests/02-capa-crds.yaml
oc apply -f custom_manifests/02-capa-controller.yaml
```

## Node Auto-Scaling (CAPA)

Unlike the OCI approach (manual Terraform), this implementation uses **Cluster API** for automatic node scaling:

```yaml
apiVersion: cluster.x-k8s.io/v1beta1
kind: MachineDeployment
metadata:
  name: worker-cn-hangzhou
spec:
  replicas: 3   # change this to scale
  template:
    spec:
      infrastructureRef:
        kind: AlibabaCloudMachineTemplate
        name: worker-template
```

## Repository Structure

```
alibaba-openshift/
├── ros-templates/
│   └── create-cluster.yaml             # Main infrastructure stack
│                                       #   Output: DynamicCustomManifest → save as alibaba-ccm-config.yaml
├── custom_manifests/
│   ├── 01-alibaba-ccm.yaml             # CCM static resources (SA, RBAC, Deployment) — upload at install-time
│   ├── 02-capa-crds.yaml               # CAPI CRD definitions — apply post-install
│   ├── 02-capa-controller.yaml         # CAPI controller deployment — apply post-install
│   └── 03-machineconfig-providerid.yaml  # kubelet ProviderID — upload at install-time
└── docs/
    └── (additional guides)
```
