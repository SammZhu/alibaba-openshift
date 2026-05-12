# OpenShift on Alibaba Cloud

Installing OpenShift Container Platform on Alibaba Cloud using the **External Platform** approach, modeled after [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift).

## Architecture

```
Part 1: Red Hat Hybrid Cloud Console
  └── Generate Discovery ISO

Part 2: Alibaba Cloud ROS (Resource Orchestration Service)
  └── run ros-templates/create-cluster.yaml
      ├── VPC / VSwitch / NAT / EIP
      ├── Security Groups
      ├── RAM Role + Policy (Instance Principal — no AK/SK needed)
      ├── API Server SLB (internal)
      ├── PrivateZone DNS (api.* / api-int.*)
      ├── ECS Instances (bootstrap + masters + workers)
      └── Outputs: install-config.yaml + cloud config values

Part 3: Red Hat Hybrid Cloud Console (Assisted Installer)
  ├── Assign node roles
  ├── Upload custom_manifests/03-machineconfig-providerid.yaml  # kubelet ProviderID (install-time)
  └── Start installation

Post-install:
  oc apply -f custom_manifests/01-alibaba-ccm.yaml        # Cloud Controller Manager
  oc apply -f custom_manifests/02-capa-crds.yaml          # CAPI CRDs
  oc apply -f custom_manifests/02-capa-controller.yaml    # CAPI Controller (node auto-scaling)
```

## Differences from OCI approach

| Feature | OCI | Alibaba Cloud |
|---------|-----|---------------|
| IaC tool | Terraform via OCI Resource Manager | ROS (native, no license issue) |
| Node auto-scaling | Manual Terraform `add-nodes` | **CAPA MachineSet** (automatic) |
| CCM auth | Instance Principal | RAM Role bound to ECS (equivalent) |
| CCM lifecycle | Direct manifest | Direct manifest |

## Prerequisites

- Alibaba Cloud account with RAM admin permissions
- Red Hat account with OpenShift subscription
- Alibaba Cloud CLI (`aliyun`) or console access
- `oc` CLI

## Installation Steps

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
   - `DiscoveryIsoUrl`: OSS pre-authenticated URL from Part 1
   - Region, zones, instance types as needed
4. Create the stack and wait for completion (~10 minutes)
5. From the **Outputs** tab, copy:
   - `InstallConfig` → paste into Red Hat console
   - `DynamicCustomManifest` → note the VPC/VSwitch IDs

### Part 3 — Complete Installation

1. In Red Hat Hybrid Cloud Console, the discovery agents will appear as nodes are booted
2. Assign roles: 3 masters + N workers
3. In **Custom manifests**, upload the following files:
   - `custom_manifests/03-machineconfig-providerid.yaml` — **must be uploaded here, not post-install**,
     so that kubelet ProviderID is set during first boot before the kubelet starts
   > Note: `01-alibaba-ccm.yaml` requires ROS output values; apply it post-install instead.
4. Start the installation

### Post-Installation

```bash
# Replace placeholder values in CCM config with ROS output values first
sed -i 's/ALIBABA_REGION/cn-hangzhou/g' custom_manifests/01-alibaba-ccm.yaml
sed -i 's/ALIBABA_VPC_ID/vpc-xxxxx/g' custom_manifests/01-alibaba-ccm.yaml
sed -i 's/ALIBABA_ZONE_ID/cn-hangzhou-h/g' custom_manifests/01-alibaba-ccm.yaml
sed -i 's/ALIBABA_VSWITCH_ID/vsw-xxxxx/g' custom_manifests/01-alibaba-ccm.yaml

oc apply -f custom_manifests/01-alibaba-ccm.yaml
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
│   └── create-cluster.yaml     # Main infrastructure stack
├── custom_manifests/
│   ├── 01-alibaba-ccm.yaml     # Cloud Controller Manager
│   ├── 02-capa-crds.yaml       # CAPI CRD definitions
│   ├── 02-capa-controller.yaml # CAPI controller deployment
│   └── 03-machineconfig-providerid.yaml  # kubelet ProviderID
└── docs/
    └── (additional guides)
```
