#!/usr/bin/env bash
#
# deploy-post-install.sh — apply post-install components to an OpenShift cluster
# installed on Alibaba Cloud (External Platform).
#
# WHAT THIS SCRIPT DOES
# =====================
# Assumes the cluster is already installed via Agent-based or Assisted Installer
# with the install-time custom manifests in place (CCM, MachineConfig provider-id,
# cloud-config ConfigMap).
#
# This script applies the post-install layer:
#   1. CAPI Infrastructure Provider for Alibaba Cloud   (CRDs + controller)
#   2. Alibaba Cloud CSI Operator                       (direct deploy, bypasses OLM)
#   3. AlibabaCloudCSIDriver CR                         (triggers Disk driver deployment)
#   4. (Optional) OADP backup stack                     (--with-oadp flag)
#
# Designed for TESTING — bypasses OLM/OperatorHub so no bundle/catalog is needed.
# For production, switch to the OLM path via 04-csi-{operatorgroup,subscription,catalogsource}.yaml.
#
# REQUIREMENTS
# ============
#   - KUBECONFIG pointing at an installed OpenShift cluster
#   - oc (or kubectl) in PATH
#   - kustomize in PATH (or `oc apply -k`)
#   - The three sibling repositories present alongside this one:
#       alibaba-cloud-csi-operator/
#       openshift-capi-alicloud/
#
# USAGE
# =====
#   ./deploy-post-install.sh                        # apply all defaults
#   ./deploy-post-install.sh --with-oadp            # also install OADP backup
#   ./deploy-post-install.sh --dry-run              # render manifests, don't apply
#   ./deploy-post-install.sh --skip-capi            # skip CAPI provider
#   ./deploy-post-install.sh --skip-csi             # skip CSI operator + driver
#
#   CSI_OPERATOR_IMG=quay.io/myorg/alibaba-cloud-csi-operator:v1.35.3 \
#     ./deploy-post-install.sh
#
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"

CSI_OPERATOR_REPO="${WORKSPACE_ROOT}/alibaba-cloud-csi-operator"
CAPI_REPO="${WORKSPACE_ROOT}/openshift-capi-alicloud"
MANIFEST_DIR="${REPO_ROOT}/custom_manifests"

# Image defaults — override with environment variables.
CSI_OPERATOR_IMG="${CSI_OPERATOR_IMG:-quay.io/samzhu/alibaba-cloud-csi-operator:v1.35.3}"
CAPI_PROVIDER_IMG="${CAPI_PROVIDER_IMG:-quay.io/samzhu/openshift-capi-alicloud:v0.1.12}"   # legacy script; ansible SSOT = ansible/vars/images.yml

WITH_OADP=false
DRY_RUN=false
SKIP_CAPI=false
SKIP_CSI=false

# ── Flag parsing ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --with-oadp)   WITH_OADP=true;  shift ;;
    --dry-run)     DRY_RUN=true;    shift ;;
    --skip-capi)   SKIP_CAPI=true;  shift ;;
    --skip-csi)    SKIP_CSI=true;   shift ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

OC=${OC:-oc}
APPLY=( "$OC" apply )
$DRY_RUN && APPLY+=( --dry-run=client -o yaml )

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required in PATH"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
preflight() {
  log "Preflight checks…"
  need "$OC"
  need kustomize

  [[ -n "${KUBECONFIG:-}" || -f "$HOME/.kube/config" ]] \
    || die "KUBECONFIG is not set and ~/.kube/config does not exist"

  $OC cluster-info >/dev/null 2>&1 \
    || die "Cannot reach the cluster — check KUBECONFIG / network"

  local v
  v=$($OC version -o json 2>/dev/null | jq -r '.openshiftVersion // empty' || true)
  [[ -n "$v" ]] && ok "Connected to OpenShift $v" || ok "Connected to Kubernetes cluster"

  $DRY_RUN && warn "DRY-RUN mode — no resources will be applied"
}

# ── 1. CAPI Provider ──────────────────────────────────────────────────────────
deploy_capi() {
  $SKIP_CAPI && { warn "Skipping CAPI provider"; return; }
  [[ -d "$CAPI_REPO" ]] || die "CAPI repo not found: $CAPI_REPO"

  log "Applying CAPI CRDs…"
  "${APPLY[@]}" -f "${CAPI_REPO}/config/crd/bases/"

  log "Applying CAPI controller manifest (image: $CAPI_PROVIDER_IMG)…"
  # Replace the image tag inline so we don't have to maintain it in two places.
  sed "s|image: quay.io/samzhu/openshift-capi-alicloud:.*|image: ${CAPI_PROVIDER_IMG}|" \
      "${MANIFEST_DIR}/02-capa-controller.yaml" \
    | "${APPLY[@]}" -f -

  $DRY_RUN || ok "CAPI provider deployed"
}

# ── 2. CSI Operator (bypasses OLM) ────────────────────────────────────────────
deploy_csi_operator() {
  $SKIP_CSI && { warn "Skipping CSI operator"; return; }
  [[ -d "$CSI_OPERATOR_REPO" ]] || die "CSI operator repo not found: $CSI_OPERATOR_REPO"

  log "Deploying CSI operator via kustomize (image: $CSI_OPERATOR_IMG)…"
  pushd "$CSI_OPERATOR_REPO/config/manager" >/dev/null
  kustomize edit set image "controller=${CSI_OPERATOR_IMG}"
  popd >/dev/null

  if $DRY_RUN; then
    kustomize build "${CSI_OPERATOR_REPO}/config/default"
  else
    kustomize build "${CSI_OPERATOR_REPO}/config/default" | $OC apply -f -
    ok "CSI operator deployed"
  fi
}

# ── 3. CSI Driver CR ──────────────────────────────────────────────────────────
deploy_csi_driver_cr() {
  $SKIP_CSI && return

  log "Waiting for AlibabaCloudCSIDriver CRD to be Established…"
  if ! $DRY_RUN; then
    $OC wait --for=condition=Established \
      crd/alibabacloudcsidrivers.csi.alibabacloud.com --timeout=60s \
      || die "CRD never became Established"
  fi

  log "Applying AlibabaCloudCSIDriver CR…"
  "${APPLY[@]}" -f "${MANIFEST_DIR}/04-csi-driver-cr.yaml"

  $DRY_RUN || ok "CSI driver CR applied"
}

# ── 4. OADP (optional) ────────────────────────────────────────────────────────
deploy_oadp() {
  $WITH_OADP || return

  log "Installing OADP operator + DataProtectionApplication…"
  "${APPLY[@]}" -f "${MANIFEST_DIR}/05-oadp-subscription.yaml"

  if ! $DRY_RUN; then
    log "Waiting for OADP operator to be ready…"
    sleep 10  # give the Subscription controller time to spawn the CSV
    $OC wait --for=condition=Established \
      crd/dataprotectionapplications.oadp.openshift.io --timeout=300s \
      || die "OADP CRD never became Established"
  fi

  # The credentials Secret and DataProtectionApplication are now ansible
  # templates (05-oadp-oss-credentials.yaml.j2 / 05-oadp-dpa.yaml.j2), rendered
  # from oadp_* vars. This shell path only installs the operator; finish the
  # backup wiring via the ansible flow.
  warn "OADP operator installed. To finish the backup wiring (Secret + DPA):"
  warn "  set oadp_enabled: true + oadp_oss_bucket / oadp_oss_access_key_id / _secret"
  warn "  in group_vars/all.yml, then run: ansible-playbook playbooks/08-deploy-post-install.yml"
  warn "  (the .yaml.j2 templates render the OSS bucket/region/endpoint + AK/SK)"
}

# ── 5. Post-deploy summary ────────────────────────────────────────────────────
summary() {
  $DRY_RUN && return
  echo
  ok "Post-install deployment complete. Verification commands:"
  cat <<EOF

  # CAPI provider
  oc get pods -n capa-system
  oc get crd | grep cluster.x-k8s.io

  # CSI operator + driver
  oc get pods -n alibaba-cloud-csi-operator-system
  oc get alibabacloudcsidriver
  oc get storageclass | grep alicloud

  # Node initialization (External Platform CCM)
  oc get nodes -o wide
  oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.providerID}{"\n"}{end}'

  # Live test (provisions a PV)
  cat <<'TEST' | oc apply -f -
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: test-disk
  spec:
    accessModes: [ReadWriteOnce]
    resources:
      requests:
        storage: 20Gi
    storageClassName: alicloud-disk-essd
TEST

EOF
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  preflight
  deploy_capi
  deploy_csi_operator
  deploy_csi_driver_cr
  deploy_oadp
  summary
}

main "$@"
