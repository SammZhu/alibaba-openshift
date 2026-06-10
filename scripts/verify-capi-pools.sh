#!/usr/bin/env bash
# verify-capi-pools.sh — READ-ONLY verification of the self-managed-core
# MachineDeployment worker pools. Pairs with playbook 12 and checks, in one shot:
#   #62  declarative multi-AZ pools  (infra contract + failureDomains + spread)
#   #78  providerID -> Machine.status.nodeRef association
#   #69  MachineHealthCheck remediation wiring
#
# Touches NOTHING (only `oc get` / `oc logs`). Safe to re-run while site-post 12
# is still looping. Run on the jump host (has /root/kubeconfig):
#
#   bash /root/openshift-alibaba/alibaba-openshift/scripts/verify-capi-pools.sh
#
# Override via env: KUBECONFIG, NS, CLUSTER.
set -uo pipefail
export KUBECONFIG="${KUBECONFIG:-/root/kubeconfig}"
NS="${NS:-default}"
CLUSTER="${CLUSTER:-caworkers}"
OC=(oc --kubeconfig "${KUBECONFIG}")

# Colour only on an interactive TTY (keeps ansible-captured output clean).
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  c_hdr=$'\033[1;36m'; c_ok=$'\033[1;32m'; c_warn=$'\033[1;33m'; c_off=$'\033[0m'
else
  c_hdr=''; c_ok=''; c_warn=''; c_off=''
fi
section() { printf '\n%s== %s ==%s\n' "$c_hdr" "$1" "$c_off"; }
kv()      { printf '  %-34s %s\n' "$1" "$2"; }

printf '%sCAPI worker-pool verification%s  (ns=%s cluster=%s  %s)\n' \
  "$c_hdr" "$c_off" "$NS" "$CLUSTER" "$(date -u +%FT%TZ)"

# ── 0. controllers + contract labels ─────────────────────────────────────────
section "0. core + CAPA controller health"
"${OC[@]}" -n capi-system get deploy capi-controller-manager \
  -o 'custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,AVAIL:.status.availableReplicas' 2>&1
"${OC[@]}" -n capa-system get deploy capa-controller-manager \
  -o 'custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,AVAIL:.status.availableReplicas' 2>&1

section "0b. infra CRD contract labels (MUST contain cluster.x-k8s.io/v1beta2)"
for crd in alibabacloudclusters alibabacloudmachines; do
  lbl="$("${OC[@]}" get crd "${crd}.infrastructure.cluster.x-k8s.io" \
        -o jsonpath='{.metadata.labels.cluster\.x-k8s\.io/v1beta2}' 2>/dev/null)"
  if [ -n "$lbl" ]; then kv "$crd" "${c_ok}cluster.x-k8s.io/v1beta2=${lbl}${c_off}"
  else                   kv "$crd" "${c_warn}MISSING contract label — core can't resolve contract${c_off}"; fi
done

# ── 1. #62 cluster infra readiness + failureDomains copy ─────────────────────
section "1. #62 — Cluster infra readiness + failureDomains"
"${OC[@]}" -n "$NS" get cluster "$CLUSTER" \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,INFRA:.status.infrastructureReady,CP:.status.controlPlaneReady' 2>&1
kv "Cluster.status.failureDomains[].name" \
   "$("${OC[@]}" -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.failureDomains[*].name}' 2>&1)"
echo "  raw Cluster.status.failureDomains:"
"${OC[@]}" -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.failureDomains}' 2>&1; echo

section "1b. AlibabaCloudCluster own status (provider side)"
kv "ACC ready"        "$("${OC[@]}" -n "$NS" get alibabacloudcluster "$CLUSTER" -o jsonpath='{.status.ready}' 2>&1)"
kv "ACC provisioned"  "$("${OC[@]}" -n "$NS" get alibabacloudcluster "$CLUSTER" -o jsonpath='{.status.initialization.provisioned}' 2>&1)"
kv "ACC failureDomains[].name" \
   "$("${OC[@]}" -n "$NS" get alibabacloudcluster "$CLUSTER" -o jsonpath='{.status.failureDomains[*].name}' 2>&1)"

# ── 1c. externally-managed control plane (gates worker node-health → MD ready) ─
# CAPI v1.12 gates worker Machine node-health (and thus MachineDeployment
# readyReplicas) on the Cluster's ControlPlaneInitialized. The AlibabaCloudControlPlane
# (mode=external) adopts the existing OCP control plane and reports it initialized.
# Expect: ACP initialized=true + externalManaged=true → Cluster ControlPlaneInitialized=True.
section "1c. control plane (AlibabaCloudControlPlane → ControlPlaneInitialized)"
kv "Cluster ControlPlaneInitialized" \
   "$("${OC[@]}" -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.conditions[?(@.type=="ControlPlaneInitialized")].status}' 2>&1)"
kv "Cluster controlPlaneReady" \
   "$("${OC[@]}" -n "$NS" get cluster "$CLUSTER" -o jsonpath='{.status.controlPlaneReady}' 2>&1)"
kv "ACP initialized" \
   "$("${OC[@]}" -n "$NS" get alibabacloudcontrolplane "$CLUSTER" -o jsonpath='{.status.initialization.controlPlaneInitialized}' 2>&1)"
kv "ACP externalManaged" \
   "$("${OC[@]}" -n "$NS" get alibabacloudcontrolplane "$CLUSTER" -o jsonpath='{.status.externalManagedControlPlane}' 2>&1)"
kv "ACP ready / version" \
   "$("${OC[@]}" -n "$NS" get alibabacloudcontrolplane "$CLUSTER" -o jsonpath='{.status.ready}  {.status.version}' 2>&1)"

# ── 2. #62 MachineDeployment / MachineSet pools ──────────────────────────────
section "2. #62 — MachineDeployment + MachineSet pools"
"${OC[@]}" -n "$NS" get machinedeployment -l "cluster.x-k8s.io/cluster-name=$CLUSTER" \
  -o 'custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas,AVAIL:.status.availableReplicas,FD:.spec.template.spec.failureDomain' 2>&1
"${OC[@]}" -n "$NS" get machineset -l "cluster.x-k8s.io/cluster-name=$CLUSTER" \
  -o 'custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,READY:.status.readyReplicas' 2>&1

# ── 3. #62 Machines + cross-AZ spread ────────────────────────────────────────
section "3. #62 — Machines + cross-AZ spread"
"${OC[@]}" -n "$NS" get machine -l "cluster.x-k8s.io/cluster-name=$CLUSTER" \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,FD:.spec.failureDomain,NODE:.status.nodeRef.name' 2>&1
echo "  -- AlibabaCloudMachine resolved zone / vSwitch / provisioned:"
"${OC[@]}" -n "$NS" get alibabacloudmachine \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"  zone="}{.spec.zoneID}{"  vsw="}{.spec.vSwitchID}{"  provisioned="}{.status.initialization.provisioned}{"\n"}{end}' 2>&1
distinct="$("${OC[@]}" -n "$NS" get alibabacloudmachine -o jsonpath='{range .items[*]}{.spec.zoneID}{"\n"}{end}' 2>/dev/null | sort -u | grep -c .)"
kv "distinct worker zones" "${distinct}  (want >= 3 for full spread)"

# ── 4. #78 providerID format match + nodeRef ─────────────────────────────────
section "4. #78 — providerID format match + nodeRef (THE head-risk check)"
echo "  -- Machine.spec.providerID  ->  Machine.status.nodeRef:"
"${OC[@]}" -n "$NS" get machine -l "cluster.x-k8s.io/cluster-name=$CLUSTER" \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"\n      machine.spec.providerID = "}{.spec.providerID}{"\n      machine.status.nodeRef  = "}{.status.nodeRef.name}{"\n"}{end}' 2>&1
echo
echo "  -- Node.spec.providerID (written by Alibaba CCM — COMPARE FORMAT to above):"
"${OC[@]}" get nodes \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"  providerID="}{.spec.providerID}{"\n"}{end}' 2>&1
echo
echo "  -- AlibabaCloudMachine.spec.providerID (provider-written, alicloud://<region>/<id>):"
"${OC[@]}" -n "$NS" get alibabacloudmachine \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"  "}{.spec.providerID}{"\n"}{end}' 2>&1
echo
echo "  NOTE: nodeRef needs Node.spec.providerID == Machine.spec.providerID EXACTLY."
echo "        If Node shows '<region>.<id>' or no 'alicloud://' prefix, the format"
echo "        differs from CAPA's 'alicloud://<region>/<id>' → nodeRef stays empty (#78)."

# ── 5. #69 MachineHealthCheck ────────────────────────────────────────────────
section "5. #69 — MachineHealthCheck"
"${OC[@]}" -n "$NS" get machinehealthcheck "${CLUSTER}-mhc" \
  -o 'custom-columns=NAME:.metadata.name,MAXUNHEALTHY:.spec.maxUnhealthy,EXPECTED:.status.expectedMachines,CURRENT:.status.currentHealthy,REMEDIATIONS:.status.remediationsAllowed' 2>&1
echo "  -- MHC conditions:"
"${OC[@]}" -n "$NS" get machinehealthcheck "${CLUSTER}-mhc" \
  -o jsonpath='{range .status.conditions[*]}    {.type}={.status} ({.reason}){"\n"}{end}' 2>&1

# ── 6. core controller log tail (contract / failureDomains / errors) ─────────
section "6. core controller recent logs (contract / failureDomains / errors)"
"${OC[@]}" -n capi-system logs deploy/capi-controller-manager --tail=120 2>&1 \
  | grep -iE "failuredomain|contract|provision|infrastructure ready|caworkers|error|reconcil" \
  | tail -20 || echo "  (no matching lines)"

section "DONE — paste this whole output back for triage"
