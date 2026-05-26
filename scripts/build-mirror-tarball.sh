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
  echo "[1/6] Installing oc + oc-mirror (versioned URLs — /latest/ is now 404)..."
  curl -sL "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$OPENSHIFT_PATCH_VERSION/openshift-client-linux.tar.gz" | tar -xz oc
  curl -sLO "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$OPENSHIFT_PATCH_VERSION/oc-mirror.tar.gz"
  tar -xzf oc-mirror.tar.gz
  chmod +x oc oc-mirror
  sudo mv oc oc-mirror /usr/local/bin/
fi

# Make pull-secret discoverable by oc-mirror
export DOCKER_CONFIG="$(dirname "$PULL_SECRET")"

# ── Generate ImageSetConfiguration (oc-mirror v2 schema) ─────────────────────
# v2 is mandatory from oc-mirror 4.21 onwards; v1 generates state files
# (publish/.metadata.json) that make every re-push a silent noop unless
# you pass --skip-metadata-check, and even then it's prone to incomplete
# pushes.  v2 has clean state semantics + emits IDMS natively (with
# mirrorSourcePolicy: NeverContactSource) so the cluster enforces
# mirror-only at the kube-level.
echo "[2/6] Writing ImageSetConfiguration (v2)..."
cat > imageset-config.yaml <<EOF
apiVersion: mirror.openshift.io/v2alpha1
kind: ImageSetConfiguration
mirror:
  platform:
    architectures:
      - amd64
    channels:
      - name: stable-${OPENSHIFT_VERSION}
        type: ocp
        minVersion: ${OPENSHIFT_PATCH_VERSION}
        maxVersion: ${OPENSHIFT_PATCH_VERSION}
EOF

# Pin additionalImages to digest form.
#
# oc-mirror v2 has a known foot-gun where tag-form additionalImages
# (e.g. registry.redhat.io/rhai/foo:abc123) get silently skipped for
# some images in the same imageset — we observed only 1 of 3 rhai
# images actually being mirrored, with no error in the log.  Resolving
# the tags to digests up-front sidesteps the whole class.
#
# skopeo inspect needs the registry-redhat-io creds; pull-secret JSON
# has them base64-encoded under .auths["registry.redhat.io"].auth.
_REDHAT_CREDS=$(jq -r '.auths["registry.redhat.io"].auth' "$PULL_SECRET" | base64 -d)
echo "  additionalImages:" >> imageset-config.yaml
for IMG in "$AI_AGENT_IMAGE" "$AI_INSTALLER_IMAGE" "$AI_CONTROLLER_IMAGE"; do
  REPO="${IMG%:*}"   # strip tag
  DIGEST=$(skopeo inspect --no-tags --creds "$_REDHAT_CREDS" "docker://$IMG" 2>/dev/null \
             | jq -r .Digest)
  if [[ -z "$DIGEST" || "$DIGEST" == "null" ]]; then
    echo "ERROR: skopeo inspect failed for $IMG — can't resolve digest." >&2
    exit 1
  fi
  echo "    - name: ${REPO}@${DIGEST}" >> imageset-config.yaml
  echo "    → pinned ${IMG##*:} → ${DIGEST}"
done

# Optional: mirror operator catalogs (post-install OperatorHub) so installing
# any operator from those catalogs also works fully offline.
# Set OPERATOR_CATALOGS=redhat-operators,certified-operators (comma-separated).
# Each one expands to its full image set (~10-50 GB per catalog) — big.
if [[ -n "${OPERATOR_CATALOGS:-}" ]]; then
  echo "[2b/8] Adding operator catalogs: $OPERATOR_CATALOGS"
  echo "  operators:" >> imageset-config.yaml
  IFS=',' read -ra _CATS <<< "$OPERATOR_CATALOGS"
  for c in "${_CATS[@]}"; do
    cat >> imageset-config.yaml <<EOF
    - catalog: registry.redhat.io/redhat/${c}-index:v${OPENSHIFT_VERSION}
EOF
  done
fi

# ── Run oc-mirror v2 (the slow step — pulls ~25-30 GB from quay.io) ──────────
# v2 syntax: oc-mirror -c <cfg> file://<dir> --v2
# Output layout differs from v1:
#   openshift-mirror/working-dir/      — staging, contains cluster-resources/
#   openshift-mirror/mirror_*.tar      — image chunks
#
# IMPORTANT: oc-mirror v2 frequently exits 0 even when some images
# failed to download (partial-success semantics).  set -e alone won't
# catch this.  We tee the log and inspect afterwards to fail loudly
# instead of shipping a half-baked tarball.
# oc-mirror v2 resume notes:
#   - Blob cache lives at $OC_MIRROR_CACHE_DIR (default: $HOME/.oc-mirror).
#     If a run dies mid-download, the next run reuses what's already there —
#     do NOT rm -rf this between runs.  Across runs, expect it to grow to
#     ~50-80 GB for a full OCP release.
#   - Default --retry-times=2 is too low for cross-border links; bump to 10.
#   - The mirror_*.tar packaging only happens at the end of a successful
#     run, so an empty mirror_000001.tar mid-run is normal.
echo "[3/6] Running oc-mirror v2 (will take 15-60 min depending on link speed)..."
echo "      cache dir : ${OC_MIRROR_CACHE_DIR:-$HOME/.oc-mirror}"
echo "      retries   : ${OC_MIRROR_RETRIES:-10}"
OC_MIRROR_LOG="$WORK_DIR/oc-mirror.log"
# Note: --workspace is rejected in mirrorToDisk (file://) mode —
# oc-mirror always uses <destination>/working-dir there.
#
# Do NOT pipe oc-mirror through `tee` — v2 detects a non-TTY stdout
# and silently switches to "non-interactive" mode, suppressing the
# per-image progress lines we used to see in v1.  Let stdout go
# straight to the terminal so progress prints live; v2 already
# writes the same content (and more) to
#   openshift-mirror/working-dir/logs/oc-mirror.log
# which our post-run validation reads from.
oc-mirror \
  -c imageset-config.yaml \
  --cache-dir "${OC_MIRROR_CACHE_DIR:-$HOME/.oc-mirror}" \
  --retry-times "${OC_MIRROR_RETRIES:-10}" \
  --retry-delay "${OC_MIRROR_RETRY_DELAY:-5s}" \
  file://./openshift-mirror --v2
OCM_RC=$?
# Mirror v2's internal log captures everything (with timestamps) for
# the post-run grep checks below.
cp -f openshift-mirror/working-dir/logs/oc-mirror.log "$OC_MIRROR_LOG" 2>/dev/null || true

echo "[3a/6] Validating oc-mirror output..."
# Check 1: explicit exit code
if [[ "$OCM_RC" != 0 ]]; then
  echo "ERROR: oc-mirror exited with rc=$OCM_RC — aborting before upload."
  exit 1
fi
# Check 2: error / failure lines in the log (v2 prints
# "image X failed", "error", "ERRO", etc. even when rc=0)
if grep -nEi '^(ERRO|error|failed|FATAL)|image .* failed|unable to (pull|copy)' \
     "$OC_MIRROR_LOG" \
     | grep -vEi 'no error|0 errors|warning|deprecation' \
     | head -5 \
     | grep -q .; then
  echo "ERROR: oc-mirror reported failures in log — aborting before upload:"
  grep -nEi '^(ERRO|error|failed|FATAL)|image .* failed|unable to (pull|copy)' \
       "$OC_MIRROR_LOG" | grep -vEi 'no error|0 errors|warning|deprecation' | head -20
  echo
  echo "  full log: $OC_MIRROR_LOG"
  echo "  fix the network / pull-secret / image-set config and re-run."
  exit 1
fi
# Check 3: at least one mirror chunk tarball got produced.  In v2's
# mirrorToDisk mode there is no working-dir/cluster-resources/idms-*.yaml
# (those are only emitted during the disk-to-mirror push); the source of
# truth is the mirror_NNNNNN.tar chunk files inside openshift-mirror/.
CHUNK_COUNT=$(ls openshift-mirror/mirror_*.tar 2>/dev/null | wc -l)
if (( CHUNK_COUNT == 0 )); then
  echo "ERROR: oc-mirror produced no mirror_*.tar chunks under openshift-mirror/"
  exit 1
fi
# Check 4: combined chunk size sanity.  oc-mirror v2 reuses blobs from
# the cache between runs, so the *new* tar chunks can be small if most
# blobs were already cached — but a real full mirror still always
# produces several GB of chunks.  Anything under 5 GB suggests the run
# barely did anything.  Override via MIRROR_MIN_GB if needed.
CHUNK_BYTES=$(du -cb openshift-mirror/mirror_*.tar 2>/dev/null | awk 'END{print $1}')
CHUNK_GB=$(( CHUNK_BYTES / 1024 / 1024 / 1024 ))
MIN_GB="${MIRROR_MIN_GB:-5}"
if (( CHUNK_GB < MIN_GB )); then
  echo "ERROR: combined chunk size is only ${CHUNK_GB} GB, below threshold ${MIN_GB} GB."
  echo "  oc-mirror likely produced an incomplete mirror.  Aborting."
  exit 1
fi
echo "    ✓ oc-mirror output looks complete (${CHUNK_COUNT} chunk(s), ${CHUNK_GB} GB, no error lines)"

# Collect everything into one tarball; on the receive side the entire
# directory tree gets handed back to oc-mirror via `--from file://...`.
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

# ── Generate + upload expected-images.txt ────────────────────────────────────
# 03b uses this to verify the mirror is complete after import: it queries
# Quay's catalog and fails fast if any image listed here is missing.
# Without this check, a partial mirror silently lets install start and then
# bootkube grinds for hours retrying upstream pulls that ultimately fail.
echo "[8b/8] Generating expected-images.txt from oc adm release info..."
EXPECTED_LIST="$WORK_DIR/expected-images.txt"
{
  oc adm release info --registry-config="$PULL_SECRET" \
    "quay.io/openshift-release-dev/ocp-release:${OPENSHIFT_PATCH_VERSION}-x86_64" \
    -o jsonpath='{range .references.spec.tags[*]}{.from.name}{"\n"}{end}' \
    2>/dev/null || true
  echo "$AI_AGENT_IMAGE"
  echo "$AI_INSTALLER_IMAGE"
  echo "$AI_CONTROLLER_IMAGE"
} | sort -u > "$EXPECTED_LIST"
echo "    → $(wc -l < "$EXPECTED_LIST") images expected in mirror"
aliyun oss cp "$EXPECTED_LIST" "oss://${OSS_BUCKET}/${OSS_PREFIX}/${CLUSTER_NAME}-${OPENSHIFT_VERSION}-expected-images.txt" \
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
  ansible-playbook ansible/playbooks/03b-mirror-prepare.yml

  # Then proceed with the normal cluster install:
  ansible-playbook ansible/playbooks/01-prepare-iso.yml
  ansible-playbook ansible/playbooks/02-import-image.yml
  ansible-playbook ansible/playbooks/03-create-stack.yml
  ansible-playbook ansible/playbooks/04-install-cluster.yml
═══════════════════════════════════════════════════════════════════════
EOF
