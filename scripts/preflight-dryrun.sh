#!/usr/bin/env bash
# Pre-live env-free preflight — run on the operator host BEFORE site-agent.yml /
# site.yml to catch the issues that otherwise only surface deep in a (slow, paid)
# live run: bad ROS templates, an unusable control_plane_type (too few cores /
# NVMe-required vs the virtio RHCOS image / sold-out in a zone), or missing
# prereq files.  Builds NOTHING — only ValidateTemplate / DescribeInstanceTypes /
# DescribeAvailableResource (all read-only) + local file checks.
#
# Usage:  scripts/preflight-dryrun.sh
# Reads ansible/group_vars/all.yml for the active config.  Exit 0 = all PASS.
#
# NOTE: zone availability via DescribeAvailableResource is best-effort (it can
# report Available for a type that a full CreateStack still rejects in that zone);
# the authoritative zone check is the cluster-stack PreviewStack, which needs the
# mirror's real VSwitch and so only runs in-flight (06).  cores + NVMe here ARE
# authoritative.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
GV="ansible/group_vars/all.yml"
[ -f "$GV" ] || { echo "FATAL: $GV not found (run from repo root)"; exit 2; }

# ── read config from group_vars (awk — no pyyaml dependency) ─────────────────
# Matches `key: value  # comment`, strips key/leading-space/trailing-comment and
# surrounding quotes.  group_vars values here have no embedded ':'.
read_gv() { awk -v k="^$1:" '$0 ~ k {sub(/^[^:]*:[[:space:]]*/,""); sub(/[[:space:]]*#.*$/,""); gsub(/^["'\'']+|["'\'']+$/,""); print; exit}' "$GV"; }
REGION=$(read_gv region);            PROFILE=$(read_gv aliyun_profile)
TOPO=$(read_gv cluster_topology);    [ -z "$TOPO" ] && TOPO=ha
METHOD=$(read_gv installation_method); CPTYPE=$(read_gv control_plane_type)
Z1=$(read_gv zone); Z2=$(read_gv zone2); Z3=$(read_gv zone3)
ABI=false; case "$(echo "$METHOD" | tr A-Z a-z)" in agent|agent-based) ABI=true;; esac

fails=0; warns=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fails=$((fails+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; warns=$((warns+1)); }

echo "== config: topology=$TOPO method=$METHOD control_plane_type=$CPTYPE region=$REGION =="

# ── 1. ROS templates ValidateTemplate ────────────────────────────────────────
echo "== ROS templates =="
tmpls=(ros-templates/mirror-stack.yaml)
if [ "$TOPO" = sno ]; then tmpls+=(ros-templates/cluster-stack-sno.yaml); else tmpls+=(ros-templates/cluster-stack.yaml); fi
for t in "${tmpls[@]}"; do
  out=$(aliyun ros ValidateTemplate --region "$REGION" --TemplateBody "$(cat "$t")" --profile "$PROFILE" 2>&1)
  if echo "$out" | grep -qiE "ErrorCode|InvalidTemplate"; then
    fail "$t — $(echo "$out" | grep -iE Message | head -1 | cut -c1-120)"
  else pass "$t ValidateTemplate"; fi
done

# ── 2. control_plane_type: cores + NVMe (authoritative) ───────────────────────
echo "== control_plane_type ($CPTYPE) =="
need_cores=4; [ "$TOPO" = sno ] && need_cores=8
it=$(aliyun ecs DescribeInstanceTypes --RegionId "$REGION" --InstanceTypes.1 "$CPTYPE" --profile "$PROFILE" 2>/dev/null \
     | python3 -c "import sys,json
try:
 t=json.load(sys.stdin)['InstanceTypes']['InstanceType'][0]
 print(t['CpuCoreCount'], t['MemorySize'], t.get('NvmeSupport','?'))
except: print('ERR')")
if [ "$it" = ERR ] || [ -z "$it" ]; then fail "DescribeInstanceTypes $CPTYPE — not found"; else
  set -- $it; cores=$1; mem=$2; nvme=$3
  if [ "${cores:-0}" -ge "$need_cores" ]; then pass "cores=$cores (need >=$need_cores for $TOPO), mem=${mem}G"
  else fail "cores=$cores < $need_cores required for $TOPO — pick a bigger type"; fi
  if [ "$ABI" = true ]; then
    if [ "$nvme" = required ]; then fail "NvmeSupport=required — RHCOS agent image is virtio-only (06b reimage will fail). Use a gen-6/7 type."
    else pass "NvmeSupport=$nvme (RHCOS-compatible)"; fi
  fi
fi

# ── 3. control_plane_type sellable in each master zone (best-effort) ──────────
echo "== zone availability (best-effort; authoritative check is in-flight PreviewStack) =="
if [ "$ABI" = true ] && [ "$TOPO" != sno ]; then zones="$Z1 $Z2 $Z3"; else zones="$([ "$ABI" = true ] && echo "$Z1" || echo "$Z2")"; fi
for z in $zones; do
  [ -z "$z" ] && continue
  st=$(aliyun ecs DescribeAvailableResource --RegionId "$REGION" --ZoneId "$z" --DestinationResource InstanceType \
        --InstanceType "$CPTYPE" --InstanceChargeType PostPaid --profile "$PROFILE" 2>/dev/null \
       | python3 -c "import sys,json
try:
 d=json.load(sys.stdin); s=set()
 for az in d['AvailableZones']['AvailableZone']:
  for r in az.get('AvailableResources',{}).get('AvailableResource',[]):
   for x in r.get('SupportedResources',{}).get('SupportedResource',[]):
    if x.get('Value')=='$CPTYPE': s.add(x.get('Status'))
 print(','.join(s) or 'none')
except: print('?')")
  case "$st" in
    *Available*) pass "$z: $CPTYPE Available";;
    none|*SoldOut*|*soldout*) warn "$z: $CPTYPE = $st (may fail at CreateStack — verify or pick another type/zone)";;
    *) warn "$z: $CPTYPE status=$st (could not determine; verify)";;
  esac
done

# ── 4. prereq files + aliyun profile ─────────────────────────────────────────
echo "== prereqs =="
PS=$(read_gv pull_secret_file); SK=$(read_gv ssh_priv_key_file); PK=$(read_gv ssh_pub_key_file)
PS="${PS/\{\{ lookup(\'env\',\'HOME\') \}\}/$HOME}"; PS="${PS/#\~/$HOME}"
for pair in "pull-secret:$HOME/.openshift/pull-secret.json" "ssh-pub:$HOME/.ssh/openshift_ed25519.pub" "ssh-priv:$HOME/.ssh/openshift_ed25519"; do
  lbl="${pair%%:*}"; p="${pair#*:}"
  if [ -f "$p" ]; then pass "$lbl present ($p)"; else warn "$lbl not at default $p (ok if group_vars overrides)"; fi
done
command -v aliyun >/dev/null && pass "aliyun CLI present" || fail "aliyun CLI not found"
aliyun sts GetCallerIdentity --profile "$PROFILE" >/dev/null 2>&1 && pass "aliyun profile '$PROFILE' valid" || fail "aliyun profile '$PROFILE' invalid/expired"
if [ "$ABI" = true ]; then command -v openshift-install >/dev/null && pass "openshift-install present" || warn "openshift-install not on PATH (ABI 06a builds on the mirror ECS, so OK if staged there)"; fi

echo
if [ "$fails" -gt 0 ]; then echo "RESULT: $fails FAIL, $warns WARN — fix FAILs before the live run."; exit 1
else echo "RESULT: 0 FAIL, $warns WARN — env-free preflight clean."; exit 0; fi
