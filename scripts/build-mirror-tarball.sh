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
# Usage:
#   OSS_BUCKET=openshift-iso-samzhu-test \
#   REGION=cn-wulanchabu \
#   CLUSTER_NAME=aliocp1 \
#   OPENSHIFT_VERSION=4.17 \
#   AGENT_IMAGE_DIGEST=008935c33fb03bb246c22f8873da7599ec30aa2c \
#       ./build-mirror-tarball.sh

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
OSS_BUCKET="${OSS_BUCKET:?OSS_BUCKET is required}"
REGION="${REGION:-cn-wulanchabu}"
CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
OPENSHIFT_VERSION="${OPENSHIFT_VERSION:-4.17}"
AGENT_IMAGE_DIGEST="${AGENT_IMAGE_DIGEST:-}"   # optional: pin a specific agent image
PULL_SECRET="${PULL_SECRET:-$HOME/.docker/config.json}"
WORK_DIR="${WORK_DIR:-$(pwd)/mirror-build}"
TARBALL_NAME="${CLUSTER_NAME}-${OPENSHIFT_VERSION}.tar"

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
        minVersion: ${OPENSHIFT_VERSION}.0
EOF

if [[ -n "$AGENT_IMAGE_DIGEST" ]]; then
  cat >> imageset-config.yaml <<EOF
  additionalImages:
    - name: registry.redhat.io/rhai/assisted-installer-agent-rhel9:${AGENT_IMAGE_DIGEST}
EOF
fi

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
    --part-size=100m --parallel=10 --force

aliyun oss cp "${TARBALL_PATH}.sha256" "oss://${OSS_BUCKET}/${OSS_OBJECT}.sha256" \
    --endpoint="$OSS_ENDPOINT" --access-key-id="$AK" --access-key-secret="$SK" \
    --force

cat <<EOF

═══════════════════════════════════════════════════════════════════════
✓ Mirror tarball ready in OSS

  Bucket : $OSS_BUCKET
  Object : $OSS_OBJECT
  Size   : $TARBALL_SIZE
  SHA256 : $(cat "${TARBALL_PATH}.sha256")

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
