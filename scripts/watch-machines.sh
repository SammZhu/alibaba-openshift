#!/usr/bin/env bash
# Catch the transient "extra ECS" (CAPA surge / MachineHealthCheck remediation /
# MachineSet rollout) in the act.  Observed 2026-06-21: a while after site-post,
# an unexpected ECS appeared then was gone before it could be inspected.  Run this
# during + for a while after site-post to capture the CAPI machine churn (root
# signal) AND the ECS count (cloud signal), timestamped, to a log.
#
# Runs on the OPERATOR host (has aliyun CLI); reaches the cluster via the jump
# host (oc + /root/kubeconfig).  Reads jump_host_ip from ansible/state.yml.
#
# Usage:  scripts/watch-machines.sh [INTERVAL_SECONDS]   # default 30
#         tail -f /tmp/watch-machines-<cluster>.log       # in another terminal
# Stop with Ctrl-C.  A "machines/ECS count > expected workers" line, or any
# remediation/rollout event, pinpoints the cause.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
INTERVAL="${1:-30}"
GV="ansible/group_vars/all.yml"; ST="ansible/state.yml"

# awk reader (no pyyaml dependency): `key: value  # comment` -> value
_yread() { awk -v k="^$2:" '$0 ~ k {sub(/^[^:]*:[[:space:]]*/,""); sub(/[[:space:]]*#.*$/,""); gsub(/^["'\'']+|["'\'']+$/,""); print; exit}' "$1"; }
gv()  { _yread "$GV" "$1"; }
stt() { _yread "$ST" "$1"; }

CLUSTER=$(gv cluster_name); REGION=$(gv region); PROFILE=$(gv aliyun_profile)
KEY=$(gv ssh_priv_key_file); KEY="${KEY/#\~/$HOME}"; [ -f "$KEY" ] || KEY="$HOME/.ssh/openshift_ed25519"
JUMP=$(stt jump_host_ip)
[ -z "$JUMP" ] && { echo "FATAL: jump_host_ip not in $ST (is the cluster up?)"; exit 2; }
LOG="/tmp/watch-machines-${CLUSTER}.log"
J="ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 root@$JUMP"

echo "watching cluster=$CLUSTER via jump=$JUMP every ${INTERVAL}s -> $LOG (Ctrl-C to stop)"
echo "=== watch-machines start $(date -u +%FT%TZ) cluster=$CLUSTER ===" >> "$LOG"

while true; do
  TS=$(date -u +%FT%TZ)
  {
    echo "----- $TS -----"
    # CAPI machines (root signal: a replacement/extra Machine = the extra ECS)
    echo "[machines]"; $J "export KUBECONFIG=/root/kubeconfig; oc get machines -A -o wide --no-headers 2>/dev/null | awk '{print \$2, \$4, \$6, \$7}'" 2>/dev/null
    echo "[machinedeployment/machineset replicas]"; $J "export KUBECONFIG=/root/kubeconfig; oc get machinedeployment,machineset -A --no-headers 2>/dev/null | awk '{print \$1, \$2, \$3, \$4, \$5, \$6}'" 2>/dev/null
    echo "[machinehealthcheck]"; $J "export KUBECONFIG=/root/kubeconfig; oc get machinehealthcheck -A --no-headers 2>/dev/null" 2>/dev/null
    echo "[recent machine/remediation/scale events]"; $J "export KUBECONFIG=/root/kubeconfig; oc get events -A --sort-by=.lastTimestamp 2>/dev/null | grep -iE 'machine|remediat|scal|unhealthy|rollout|delet' | tail -6" 2>/dev/null
    # cloud signal: ALL ECS, categorised by tag (NOT filtered to one tag). The
    # original mystery-ECS hunt failed because CAPA worker / smoke ECS do NOT carry
    # kubernetes.io/cluster/<ocp-cluster> — they're tagged with the CAPI cluster name
    # (caworkers / smoke-test) + openshift-worker-pool, so a single-tag filter on the
    # OCP cluster missed them and a transient surge looked like it appeared from
    # nowhere. Listing all instances and bucketing by tag surfaces the transient.
    echo "[ECS — all, by tag bucket]"; aliyun ecs DescribeInstances --RegionId "$REGION" --profile "$PROFILE" --PageSize 100 2>/dev/null \
      | python3 -c "import sys,json
try:
 d=json.load(sys.stdin); ins=d.get('Instances',{}).get('Instance',[])
 run=[i for i in ins if i.get('Status')=='Running']
 def tags(i): return {t['TagKey']:t['TagValue'] for t in i.get('Tags',{}).get('Tag',[])}
 def bucket(i):
  t=tags(i)
  if 'kubernetes.io/cluster/${CLUSTER}' in t or t.get('openshift-cluster')=='${CLUSTER}': return 'ocp'
  if t.get('openshift-worker-pool') or 'kubernetes.io/cluster/caworkers' in t: return 'capa-worker'
  if 'kubernetes.io/cluster/smoke-test' in t: return 'smoke'
  return 'other'
 from collections import Counter
 c=Counter(bucket(i) for i in run)
 print(' running ECS:', len(run), 'by-bucket:', dict(c))
 for i in run:
  print('   ', i.get('InstanceId'), bucket(i), i.get('ZoneId'), (tags(i).get('k8s.io/cluster-api-machine') or i.get('InstanceName')))
except Exception as e: print(' (ecs query failed:', e, ')')" 2>/dev/null
  } >> "$LOG" 2>&1
  sleep "$INTERVAL"
done
