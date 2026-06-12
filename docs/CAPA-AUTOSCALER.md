# Cluster Autoscaler with CAPA worker pools (B3 / #63)

Scale the per-AZ worker MachineDeployments automatically with the **upstream
cluster-autoscaler's `clusterapi` cloud provider**. It is cloud-agnostic: it
discovers node groups from CAPI `MachineDeployment`s, scales them by patching
`spec.replicas`, and our controller turns each replica change into an ECS
instance (or frees one). No CAPA code is involved in the scaling itself.

> OpenShift's built-in `ClusterAutoscaler`/`MachineAutoscaler` CRs drive the
> **MachineAPI** (`machine.openshift.io`), NOT Cluster API. On `platform=external`
> we run worker lifecycle through CAPI, so we use the upstream cluster-autoscaler
> with `--cloud-provider=clusterapi` instead.

## How it integrates

1. **Node groups = MachineDeployments.** Each per-AZ pool (`caworkers-a`,
   `caworkers-b`, …) is an independent node group, so the autoscaler scales each
   zone separately (and respects per-zone capacity / sold-out failover, PR-B).
2. **Bounds** come from annotations on the MachineDeployment:
   ```
   cluster.x-k8s.io/cluster-api-autoscaler-node-group-min-size: "0"
   cluster.x-k8s.io/cluster-api-autoscaler-node-group-max-size: "5"
   ```
3. **Scale-from-zero** needs the node shape a pool *would* create when it has 0
   replicas (no live Node to copy). The clusterapi provider reads it from the
   **infra template** (`AlibabaCloudMachineTemplate`) annotations:
   ```
   capacity.cluster-autoscaler.kubernetes.io/cpu: "4"
   capacity.cluster-autoscaler.kubernetes.io/memory: "16Gi"
   capacity.cluster-autoscaler.kubernetes.io/ephemeral-disk: "40Gi"
   ```

Both sets are emitted by `custom_manifests/capa-worker-machinedeployment.yaml`
when you opt in (see below); they are absent by default so a non-autoscaled
cluster keeps full manual control of `replicas`.

## 1. Enable the annotations on the pools

Render the worker pools (Phase 12) with the autoscaler vars. The capacity MUST
match `instance_type`:

```
ansible-playbook ansible/playbooks/12-capa-machinedeployment.yml \
  -e autoscale_enabled=true \
  -e autoscale_min=0 -e autoscale_max=5 \
  -e autoscale_cpu=4 -e autoscale_memory=16Gi
```

### Capacity table (common ECS types)
| instanceType      | cpu | memory |
|-------------------|-----|--------|
| ecs.g7.large      | 2   | 8Gi    |
| ecs.g7.xlarge     | 4   | 16Gi   |
| ecs.g7.2xlarge    | 8   | 32Gi   |
| ecs.g7.4xlarge    | 16  | 64Gi   |
| ecs.c7.xlarge     | 4   | 8Gi    |
| ecs.r7.xlarge     | 4   | 32Gi   |

Pick `autoscale_cpu` / `autoscale_memory` from the row matching your
`instance_type`. `memory` is the node's *advertised* capacity — leave headroom;
the autoscaler compares pending-pod requests against it. If they mismatch the
real node, scale-from-zero decisions will be wrong (over/under-provision).

## 2. Deploy the cluster-autoscaler (clusterapi provider)

Opt-in — not applied by `site-post`. Management and workload are the SAME cluster
here, so the autoscaler runs in-cluster with the default kubeconfig for both.

> **Air-gap:** mirror the image first (it is NOT in the OCP release payload):
> `oc image mirror registry.k8s.io/autoscaling/cluster-autoscaler:v1.33.0 <mirror>:8443/autoscaling/cluster-autoscaler:v1.33.0`
> and reference the mirror (a companion ITMS/IDMS entry or the mirror ref directly).

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: cluster-autoscaler
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: cluster-autoscaler
  namespace: cluster-autoscaler
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-autoscaler-clusterapi
rules:
  # Cluster API node groups + the scale subresource.
  - apiGroups: ["cluster.x-k8s.io"]
    resources: ["machinedeployments", "machinedeployments/scale", "machines", "machinesets", "machinesets/scale"]
    verbs: ["get", "list", "update", "watch", "patch"]
  # Infra templates — read for scale-from-zero capacity annotations.
  - apiGroups: ["infrastructure.cluster.x-k8s.io"]
    resources: ["alibabacloudmachinetemplates", "alibabacloudmachines"]
    verbs: ["get", "list", "watch"]
  # Scheduling simulation (what the upstream autoscaler always needs).
  - apiGroups: [""]
    resources: ["nodes", "pods", "services", "replicationcontrollers", "persistentvolumeclaims", "persistentvolumes", "namespaces"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["daemonsets", "replicasets", "statefulsets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs", "cronjobs"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["policy"]
    resources: ["poddisruptionbudgets"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses", "csinodes", "csidrivers", "csistoragecapacities"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create", "patch"]
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["create", "get", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cluster-autoscaler-clusterapi
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-autoscaler-clusterapi
subjects:
  - kind: ServiceAccount
    name: cluster-autoscaler
    namespace: cluster-autoscaler
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cluster-autoscaler
  namespace: cluster-autoscaler
  labels: {app: cluster-autoscaler}
spec:
  replicas: 1
  selector:
    matchLabels: {app: cluster-autoscaler}
  template:
    metadata:
      labels: {app: cluster-autoscaler}
    spec:
      serviceAccountName: cluster-autoscaler
      containers:
        - name: cluster-autoscaler
          image: registry.k8s.io/autoscaling/cluster-autoscaler:v1.33.0  # match k8s minor; mirror for air-gap
          command:
            - /cluster-autoscaler
            - --cloud-provider=clusterapi
            # Auto-discover every MachineDeployment of this cluster that carries the
            # min/max annotations, in the namespace the CAPI resources live in.
            - --node-group-auto-discovery=clusterapi:namespace=default,clusterName=caworkers
            - --scale-down-enabled=true
            - --scale-down-delay-after-add=10m
            - --scale-down-unneeded-time=10m
            - --balance-similar-node-groups=true   # spread scale-up across AZs
            - --expander=least-waste
            - --v=2
          resources:
            requests: {cpu: 100m, memory: 300Mi}
            limits: {cpu: 500m, memory: 600Mi}
```

Adjust `--node-group-auto-discovery` `namespace=`/`clusterName=` to where your
MachineDeployments live (the live default is namespace `default`, cluster
`caworkers`). For explicit pools instead of auto-discovery, use one
`--nodes=MIN:MAX:default/caworkers-a` per pool.

## Verify

```
oc -n cluster-autoscaler logs deploy/cluster-autoscaler | grep -iE "clusterapi|node group|scale"
oc -n default get machinedeployment -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas
# Create unschedulable demand and watch a pool scale up:
oc create deployment hog --image=registry.k8s.io/pause:3.9 --replicas=20
oc patch deployment hog --type=json -p '[{"op":"add","path":"/spec/template/spec/containers/0/resources","value":{"requests":{"cpu":"1"}}}]'
# → autoscaler scales caworkers-* up; `oc get machine` shows new Machines, ECS created.
oc delete deployment hog   # → after scale-down-unneeded-time, pools scale back toward min.
```

## Caveats
- **CCM must be running.** A scaled-up worker that the Alibaba CCM hasn't
  initialized (taint not cleared, no providerID) never becomes Ready → its
  Machine never reaches Running → the autoscaler treats the scale-up as failed.
- **Scale-from-zero accuracy.** The capacity annotations are static — keep them in
  sync with `instance_type`. Wrong values cause over/under-provisioning.
- **min-size 0** lets a pool drain to zero ECS when idle (cost saving), but the
  FIRST pod that needs that zone waits a full boot+join (~minutes). Set min-size ≥1
  per zone if you need always-warm capacity.
- **MHC + autoscaler coexist**: MHC remediates *unhealthy* nodes (replace);
  autoscaler adjusts *count* (demand). They don't conflict — different triggers.
- Don't also hand-edit `replicas` on an autoscaled pool; the autoscaler owns it.
