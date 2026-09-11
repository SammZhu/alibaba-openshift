#!/usr/bin/env bash
# 用一份已跑通环境的 all.yml,生成新 Apsara 环境的候选配置。
#
# 为什么不从 all.yml.example 开始:那份是通用模板,而一份跑通过的 all.yml
# 带着几个月踩坑迭代出来的结构和注释——哪些键在专有云下是必需的、每个值受
# 什么约束。照着实际形状改,比照着模板猜准得多。
#
# 为什么不能直接拷:半更新的 all.yml 比空白的危险。它看起来完整,却悄悄指
# 着另一个环境的资源——playbook 会拿着新环境的凭据去操作旧环境的 stack。
# 所以最后一步是:扫描产物,只要还残留参考环境的任何标识就拒绝输出。
#
# 在目标 Helper 上运行(需要读元数据服务和做 DNS 探测)。
#
#   adapt-all-yml.sh <参考 all.yml> [输出路径]
set -uo pipefail

REF="${1:?用法: $0 <参考 all.yml> [输出路径]}"
OUT="${2:-./all.yml.candidate}"
[ -r "$REF" ] || { echo "读不到参考文件: $REF" >&2; exit 1; }

M=http://100.100.100.200/latest/meta-data
meta() { curl -s --max-time 5 "$M/$1"; }

# ── 目标环境:从元数据取,不靠参数 ────────────────────────────────────────
TGT_REGION=$(meta region-id)
[ -n "$TGT_REGION" ] || { echo "取不到 region-id —— 不在 Apsara ECS 上?" >&2; exit 1; }
# cn-wulan-ste2-d01 -> ste2
TGT_ENV=$(echo "$TGT_REGION" | sed -E 's/^cn-[a-z]+-([a-z0-9]+)-.*/\1/')
TGT_VPC=$(meta vpc-id)
TGT_VPC_CIDR=$(meta vpc-cidr-block)

# ── 参考环境:从文件里认出来 ──────────────────────────────────────────────
REF_REGION=$(grep -oE '^region: *\S+' "$REF" | awk '{print $2}')
REF_ENV=$(echo "$REF_REGION" | sed -E 's/^cn-[a-z]+-([a-z0-9]+)-.*/\1/')
[ -n "$REF_ENV" ] || { echo "参考文件里认不出环境名(region: 那行)" >&2; exit 1; }
[ "$REF_ENV" = "$TGT_ENV" ] && { echo "参考和目标是同一个环境($REF_ENV)——没什么可改的" >&2; exit 1; }

echo "参考环境: $REF_ENV  ($REF_REGION)"
echo "目标环境: $TGT_ENV  ($TGT_REGION)"
echo "  VPC:    $TGT_VPC  $TGT_VPC_CIDR"
echo

# 端点探测的思路:拿参考环境实际存在的形状去套目标环境,而不是猜命名规律。
# dyz7 一个环境里就有 5 种形状(ros.cloud.X / ecs-internal.cloud.X /
# ram-vpc.cloud.X / dns-control.pop.cloud.X / oss-<region>-a.cloud.X),
# 没有可推导的规律;但这些形状本身是这家厂商部署里真实存在的,套到新环境
# 上的命中率远高于凭空构造。套完逐个验 DNS,不解析的标成 CHANGEME。

python3 - "$REF" "$OUT" "$REF_ENV" "$TGT_ENV" "$REF_REGION" "$TGT_REGION" "$TGT_VPC" "$TGT_VPC_CIDR" > /tmp/.adapt.$$ <<'PY'
import re, subprocess, sys
ref, out, refenv, tgtenv, refreg, tgtreg, vpc, vpccidr = sys.argv[1:9]
src = open(ref).read().splitlines(keepends=True)
BLANK = {'AK':'CHANGEME-access-key-id','SK':'CHANGEME-access-key-secret',
         'ORG_ID':'CHANGEME-org-id','RG_ID':'CHANGEME-resource-group-id',
         'oss_bucket':'CHANGEME-oss-bucket-must-exist-in-this-account',
         'mirror_init_password':'CHANGEME-mirror-password'}
DEFER = {k:'CHANGEME-需凭据后查' for k in
         ['zone','zone2','zone3','control_plane_type','compute_type','worker_instance_type',
          'mirror_instance_type','mirror_instance_type_fresh','mirror_instance_type_restore',
          'system_disk_category','apsara_image_id']}
DIRECT = {'region': tgtreg, 'apsara_peer_operator_vpc_id': f'"{vpc}"'}
def probe(v):
    h=v.strip('"\'');  h=h.split('//')[-1].split('/')[0]
    return subprocess.run(['getent','hosts',h],capture_output=True).returncode==0
outl, notes = [], []
for line in src:
    m = re.match(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)(\s+#.*)?$', line.rstrip('\n'))
    if not m: outl.append(line); continue
    ind,key,val,com = m.group(1),m.group(2),(m.group(3) or '').strip(),(m.group(4) or '')
    new=None
    if key in BLANK: new=BLANK[key]; notes.append((key,'需人工填'))
    elif key in DEFER: new=DEFER[key]; notes.append((key,'待凭据就绪'))
    elif key in DIRECT: new=DIRECT[key]
    elif val and (refenv in val or refreg in val):
        cand=val.replace(refreg,tgtreg).replace(refenv,tgtenv)
        if probe(cand): new=cand; notes.append((key,f'探测 ok: {cand}'))
        else: new=f'CHANGEME-{key}'; notes.append((key,f'{cand} 无 DNS'))
    outl.append(f'{ind}{key}: {new}{com}\n' if new is not None else line)
open(out,'w').writelines(outl)
for k,v in notes: print(f'  {k:<32} {v}')
PY
echo "== 替换结果 =="; cat /tmp/.adapt.$$; rm -f /tmp/.adapt.$$
echo

# ── 最后一道闸:参考环境的标识绝不能残留 ──────────────────────────────────
echo "== 残留扫描 =="
leak=$(grep -nE "$REF_ENV|$REF_REGION" "$OUT" | grep -vE "^\s*[0-9]+:\s*#" | grep -v CHANGEME)
if [ -n "$leak" ]; then
  echo "  ✗ 仍有 $REF_ENV 的值残留 —— 拒绝交付:"
  echo "$leak" | sed 's/^/      /'
  echo
  echo "  半更新的 all.yml 会让 playbook 拿新环境的凭据去操作旧环境的资源。"
  echo "  产物留在 $OUT.rejected 供检查。"
  mv "$OUT" "$OUT.rejected"; exit 1
fi
echo "  ✓ 无残留"
echo
echo "候选配置: $OUT"
echo "待填项:   grep -n CHANGEME $OUT"
