#!/usr/bin/env bash
# build-mirror-tarball.sh
#
# Run on a host with FAST quay.io connectivity (e.g. your US/EU server, or
# RHEL 8 with stable transpacific link).  Generates a self-contained OpenShift
# mirror tarball and uploads it to Aliyun OSS for the cn-wulanchabu cluster
# to consume.
#
# Prerequisites on the build host:
#   - Red Hat pull-secret at ~/.docker/config.json (or path passed as $PULL_SECRET)
#   - aliyun CLI configured with credentials that can write to the OSS bucket
#   - 60 GB free disk (30 GB tarball + working space)
#   - 5-10 GB free /tmp
#
# Outputs (in OSS):
#   oss://<bucket>/mirror-tarballs/<cluster>-<version>.tar
#   oss://<bucket>/mirror-tarballs/<cluster>-<version>.tar.sha256
#
# Usage (recommended — auto-discovers all AI component images):
#   OSS_BUCKET=openshift-iso-samzhu-test \
#   REGION=cn-wulanchabu \
#   CLUSTER_NAME=aliocp1 \
#   OPENSHIFT_VERSION=4.20 \
#   OFFLINE_TOKEN_FILE=/full/path/to/offline-token \
#       ./build-mirror-tarball.sh
#
# OFFLINE_TOKEN_FILE has no default — pass your actual token path explicitly.
# Get a token at https://console.redhat.com/openshift/token
#
# Usage (manual — pin specific images):
#   OSS_BUCKET=... CLUSTER_NAME=... OPENSHIFT_VERSION=4.20 \
#   AI_AGENT_IMAGE=registry.redhat.io/rhai/assisted-installer-agent-rhel9:008935... \
#   AI_INSTALLER_IMAGE=registry.redhat.io/rhai/assisted-installer-rhel9:a9bfccc... \
#   AI_CONTROLLER_IMAGE=registry.redhat.io/rhai/assisted-installer-controller-rhel9:a9bfccc... \
#       ./build-mirror-tarball.sh
#
# What gets mirrored:
#   - OpenShift release (platform.channels → ~25 GB of release images)
#   - discovery-agent (boots from ISO, first image masters pull)
#   - assisted-installer + assisted-installer-controller (run during install)
#
# Why all three AI images?  The discovery agent is just the first hurdle —
# the install phase pulls assisted-installer + controller too.  Missing any
# of them stalls the install.
#
# Discover digests manually:
#   TOKEN=$(curl -s --data-urlencode 'grant_type=refresh_token' \
#     --data-urlencode 'client_id=cloud-services' \
#     --data-urlencode "refresh_token=$(cat ~/.openshift/offline-token)" \
#     https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token \
#     | jq -r .access_token)
#   curl -sH "Authorization: Bearer $TOKEN" \
#     https://api.openshift.com/api/assisted-install/v2/component-versions \
#     | jq '.versions'

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
OSS_BUCKET="${OSS_BUCKET:?OSS_BUCKET is required}"
REGION="${REGION:-cn-wulanchabu}"
CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
OPENSHIFT_VERSION="${OPENSHIFT_VERSION:-4.20}"
OFFLINE_TOKEN_FILE="${OFFLINE_TOKEN_FILE:-}"   # no default — must be passed explicitly

# Auto-discover AI component images if OFFLINE_TOKEN_FILE exists and AI_* not set.
if [[ -z "${AI_AGENT_IMAGE:-}" && -n "$OFFLINE_TOKEN_FILE" ]]; then
  [[ -r "$OFFLINE_TOKEN_FILE" ]] || {
    echo "ERROR: OFFLINE_TOKEN_FILE='$OFFLINE_TOKEN_FILE' not readable"; exit 1
  }
  echo "[0/6] Auto-discovering AI component images from openshift.com (token: $OFFLINE_TOKEN_FILE)..."
  _TOKEN=$(curl -s --data-urlencode 'grant_type=refresh_token' \
    --data-urlencode 'client_id=cloud-services' \
    --data-urlencode "refresh_token=$(cat "$OFFLINE_TOKEN_FILE")" \
    https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token \
    | jq -r .access_token)
  if [[ -z "$_TOKEN" || "$_TOKEN" == "null" ]]; then
    echo "ERROR: failed to get access token from $OFFLINE_TOKEN_FILE"; exit 1
  fi
  _VERSIONS=$(curl -sH "Authorization: Bearer $_TOKEN" \
    https://api.openshift.com/api/assisted-install/v2/component-versions)
  AI_AGENT_IMAGE="${AI_AGENT_IMAGE:-$(echo "$_VERSIONS" | jq -r '.versions["discovery-agent"]')}"
  AI_INSTALLER_IMAGE="${AI_INSTALLER_IMAGE:-$(echo "$_VERSIONS" | jq -r '.versions["assisted-installer"]')}"
  AI_CONTROLLER_IMAGE="${AI_CONTROLLER_IMAGE:-$(echo "$_VERSIONS" | jq -r '.versions["assisted-installer-controller"]')}"
  echo "    discovery-agent              : $AI_AGENT_IMAGE"
  echo "    assisted-installer           : $AI_INSTALLER_IMAGE"
  echo "    assisted-installer-controller: $AI_CONTROLLER_IMAGE"
fi

# Validate we have all three AI images (either auto-discovered or env-provided)
for v in AI_AGENT_IMAGE AI_INSTALLER_IMAGE AI_CONTROLLER_IMAGE; do
  [[ -n "${!v:-}" && "${!v}" != "null" ]] || {
    echo "ERROR: $v is unset. Provide OFFLINE_TOKEN_FILE for auto-discovery, or set AI_AGENT_IMAGE / AI_INSTALLER_IMAGE / AI_CONTROLLER_IMAGE explicitly."
    exit 1
  }
done
PULL_SECRET="${PULL_SECRET:-$HOME/.docker/config.json}"
WORK_DIR="${WORK_DIR:-$(pwd)/mirror-build}"
TARBALL_NAME="${CLUSTER_NAME}-${OPENSHIFT_VERSION}.tar"

# Auto-discover the latest patch version in the stable channel if not pinned.
# Setting min=max to a SPECIFIC version makes oc-mirror download just that one
# release (~25 GB) instead of every release in the upgrade path (250+ GB).
OPENSHIFT_PATCH_VERSION="${OPENSHIFT_PATCH_VERSION:-}"
if [[ -z "$OPENSHIFT_PATCH_VERSION" ]]; then
  echo "[?] Looking up latest stable-${OPENSHIFT_VERSION} release from Cincinnati..."
  OPENSHIFT_PATCH_VERSION=$(curl -fsSL \
    "https://api.openshift.com/api/upgrades_info/v1/graph?channel=stable-${OPENSHIFT_VERSION}" \
    -H 'Accept: application/json' \
    | jq -r '.nodes[].version' | sort -V | tail -1)
  [[ -n "$OPENSHIFT_PATCH_VERSION" && "$OPENSHIFT_PATCH_VERSION" != "null" ]] || {
    echo "ERROR: stable-${OPENSHIFT_VERSION} channel returned no releases. Try OPENSHIFT_VERSION=4.19 or pin OPENSHIFT_PATCH_VERSION=X.Y.Z explicitly."
    exit 1
  }
  echo "    → will mirror $OPENSHIFT_PATCH_VERSION"
fi

OSS_ENDPOINT="oss-${REGION}.aliyuncs.com"
OSS_PREFIX="mirror-tarballs"
OSS_OBJECT="${OSS_PREFIX}/${TARBALL_NAME}"

# ── Sanity ────────────────────────────────────────────────────────────────────
[[ -f "$PULL_SECRET" ]] || { echo "ERROR: pull secret not found at $PULL_SECRET"; exit 1; }
command -v aliyun >/dev/null || { echo "ERROR: aliyun CLI missing"; exit 1; }

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

# ── Install oc + oc-mirror if missing ─────────────────────────────────────────
if ! command -v oc-mirror >/dev/null; then
  echo "[1/6] Installing oc + oc-mirror..."
  curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/oc/latest/openshift-client-linux.tar.gz | tar -xz oc
  curl -sLO https://mirror.openshift.com/pub/openshift-v4/clients/oc-mirror/latest/oc-mirror.tar.gz
  tar -xzf oc-mirror.tar.gz
  chmod +x oc oc-mirror
  sudo mv oc oc-mirror /usr/local/bin/
fi

# Make pull-secret discoverable by oc-mirror
export DOCKER_CONFIG="$(dirname "$PULL_SECRET")"

# ── Generate ImageSetConfiguration ────────────────────────────────────────────
echo "[2/6] Writing ImageSetConfiguration..."
cat > imageset-config.yaml <<EOF
apiVersion: mirror.openshift.io/v1alpha2
kind: ImageSetConfiguration
archiveSize: 10                                   # split into 10 GB chunks
storageConfig:
  local:
    path: ./mirror-data
mirror:
  platform:
    channels:
      - name: stable-${OPENSHIFT_VERSION}
        type: ocp
        minVersion: ${OPENSHIFT_PATCH_VERSION}
        maxVersion: ${OPENSHIFT_PATCH_VERSION}
EOF

cat >> imageset-config.yaml <<EOF
  additionalImages:
    - name: ${AI_AGENT_IMAGE}
    - name: ${AI_INSTALLER_IMAGE}
    - name: ${AI_CONTROLLER_IMAGE}
EOF

# ── Run oc-mirror (the slow step — pulls ~25-30 GB from quay.io) ──────────────
echo "[3/6] Running oc-mirror (will take 15-60 min depending on link speed)..."
oc-mirror --config=imageset-config.yaml file://./openshift-mirror

# Collect all generated tarballs into one combined archive
echo "[4/6] Packaging mirror data into single tarball..."
TARBALL_PATH="$WORK_DIR/$TARBALL_NAME"
tar -cf "$TARBALL_PATH" -C ./openshift-mirror .

# Compute checksum
sha256sum "$TARBALL_PATH" | awk '{print $1}' > "${TARBALL_PATH}.sha256"
TARBALL_SIZE=$(du -h "$TARBALL_PATH" | cut -f1)
echo "Tarball: $TARBALL_PATH ($TARBALL_SIZE)"

# ── Read AK/SK from aliyun config for ossutil ────────────────────────────────
PROFILE="${ALIYUN_PROFILE:-openshift-test}"
read -r AK SK < <(jq -r ".profiles[] | select(.name==\"$PROFILE\") | .access_key_id + \" \" + .access_key_secret" ~/.aliyun/config.json)
[[ -n "$AK" && -n "$SK" ]] || { echo "ERROR: could not read AK/SK for profile '$PROFILE'"; exit 1; }

# ── Upload to OSS ─────────────────────────────────────────────────────────────
echo "[5/6] Ensuring OSS bucket exists..."
aliyun oss ls "oss://${OSS_BUCKET}/" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" \
    >/dev/null 2>&1 || \
  aliyun oss mb "oss://${OSS_BUCKET}" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK"

echo "[6/6] Uploading tarball + checksum to OSS (this will take a while)..."
aliyun oss cp "$TARBALL_PATH" "oss://${OSS_BUCKET}/${OSS_OBJECT}" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" \
    --part-size=104857600 --parallel=10 --force

aliyun oss cp "${TARBALL_PATH}.sha256" "oss://${OSS_BUCKET}/${OSS_OBJECT}.sha256" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" \
    --force

# ── Also fetch + upload mirror-registry installer to OSS ─────────────────────
# Why: mirror.openshift.com 307-redirects to access.cdn.redhat.com with a
# signed query string.  Cloud-init on the in-VPC mirror ECS can't reliably
# follow this from cn-* regions (cross-border, no Red Hat token).  By
# staging the installer in OSS alongside the image tarball, cloud-init pulls
# everything from one place (free VPC-internal traffic).
echo "[7/8] Fetching mirror-registry installer (~700 MB) from Red Hat..."
MR_VERSION="${MIRROR_REGISTRY_VERSION:-}"
if [[ -z "$MR_VERSION" ]]; then
  MR_VERSION=$(curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/mirror-registry/ \
    | grep -oE 'href="[^"]+/"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | sort -V | tail -1)
  [[ -n "$MR_VERSION" ]] || { echo "ERROR: could not discover mirror-registry latest version"; exit 1; }
  echo "    → using mirror-registry $MR_VERSION (auto-detected; override with MIRROR_REGISTRY_VERSION=)"
fi

MR_TARBALL="$WORK_DIR/mirror-registry-${MR_VERSION}.tar.gz"
if [[ ! -s "$MR_TARBALL" ]]; then
  # developers.redhat.com is the redirect target; anonymous works, no Bearer token
  curl -fL --retry 5 --retry-delay 10 -o "$MR_TARBALL" \
    "https://developers.redhat.com/content-gateway/file/pub/openshift-v4/clients/mirror-registry/${MR_VERSION}/mirror-registry.tar.gz"
fi
ls -lh "$MR_TARBALL"

echo "[8/8] Uploading mirror-registry installer to OSS..."
aliyun oss cp "$MR_TARBALL" "oss://${OSS_BUCKET}/${OSS_PREFIX}/mirror-registry-${MR_VERSION}.tar.gz" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" \
    --part-size=104857600 --parallel=10 --force

# Mark the "current" version so cloud-init can find it without knowing the version
echo -n "$MR_VERSION" > "$WORK_DIR/mirror-registry-version.txt"
aliyun oss cp "$WORK_DIR/mirror-registry-version.txt" \
    "oss://${OSS_BUCKET}/${OSS_PREFIX}/mirror-registry-version.txt" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" --force

cat <<EOF

═══════════════════════════════════════════════════════════════════════
✓ Mirror artefacts ready in OSS

  Image tarball     : oss://$OSS_BUCKET/$OSS_OBJECT  ($TARBALL_SIZE)
  SHA256            : $(cat "${TARBALL_PATH}.sha256")
  mirror-registry   : oss://$OSS_BUCKET/$OSS_PREFIX/mirror-registry-${MR_VERSION}.tar.gz
  Version marker    : oss://$OSS_BUCKET/$OSS_PREFIX/mirror-registry-version.txt → $MR_VERSION

Next step — on your local ansible host:

  # Add to ansible/group_vars/all.yml:
  mirror_oss_object: $OSS_OBJECT

  # Spin up mirror ECS + import tarball:
  ansible-playbook ansible/playbooks/mirror-prepare.yml

  # Then proceed with the normal cluster install:
  ansible-playbook ansible/playbooks/01-prepare-iso.yml
  ansible-playbook ansible/playbooks/02-import-image.yml
  ansible-playbook ansible/playbooks/03-create-stack.yml
  ansible-playbook ansible/playbooks/04-install-cluster.yml
═══════════════════════════════════════════════════════════════════════
EOF
