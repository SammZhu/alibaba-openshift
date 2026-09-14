#!/usr/bin/env python3
"""discover.py —— 探测一套新的 Apsara Stack 环境,产出可直接用的 group_vars/all.yml。

专有云上几乎没有一个配置值能从别的环境抄过来:端点没有命名规律、镜像和实例规格
各环境不同、盘型不同、有没有 NAS 不一定、NAS 的盘类里还有「列出来了但卖不出去」
的。这些值一个个手查要半小时,查错一个的代价可能是一小时的无效装机。

这个脚本在 **目标环境的 helper 上** 跑,把能确定的都确定下来:

  AK=.. SK=.. ORG_ID=.. RG_ID=.. python3 scripts/apsara/discover.py
  AK=.. SK=.. ORG_ID=.. RG_ID=.. python3 scripts/apsara/discover.py \\
      -o ansible/group_vars/all.yml

不带 -o 时只打印结果(AK/SK 会被打码);带 -o 时以
group_vars/all.yml.apsara.example 为底稿写出一份填好的配置。已存在的文件不会被
覆盖,除非加 --force。

和 adapt-all-yml.sh 的区别:那个需要「一份已经跑通环境的 all.yml」当参考,因此
只能替换参考文件里已有的键——目标环境比参考环境多出来的能力(比如参考环境没有
NAS)它看不见。这个脚本不需要参考环境,直接问这套环境自己。

查不出来的值一律写成 CHANGEME 并说明原因。**不猜**:猜出来的值会一路跑到很深的
地方才暴露,而那时症状通常指向别的东西。
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CLOUDCLI = os.path.join(HERE, "cloudcli")
TEMPLATE = os.path.join(REPO, "ansible", "group_vars", "all.yml.apsara.example")
META = "http://100.100.100.200/latest/meta-data"

CHANGEME = "CHANGEME"

# 端点形状。没有规律可循——同一套环境里 ros/ecs/nas 可以是三种完全不同的形状,
# 所以只能把见过的形状全试一遍。带 {region} 的那几条是 ste2 的 NAS 教出来的
# (nas-pub.cn-wulan-ste2-d01.cloud.ste2.com),不加的话那套环境的 NAS 会被误判
# 成「不存在」。
PATTERNS = [
    "{svc}.{domain}",
    "{svc}-internal.{domain}",
    "{svc}-vpc.{domain}",
    "{svc}-pub.{domain}",
    "{svc}-pop.{domain}",
    "{svc}.pop.{domain}",
    "{svc}.{region}.{domain}",
    "{svc}-pub.{region}.{domain}",
    "{svc}-vpc.{region}.{domain}",
    "{svc}-internal.{region}.{domain}",
]

# 产品 -> (cloudcli 的产品键, 只读探测动作, 该动作要不要 RegionId)
# 探测动作必须是只读的:这个脚本会对每一个候选端点真的发一次请求。
PROBES = {
    "ROS": ("ros", "DescribeRegions", False),
    "ECS": ("ecs", "DescribeRegions", False),
    "VPC": ("vpc", "DescribeVpcs", True),
    "RAM": ("ram", "ListRoles", False),
    "NAS": ("nas", "DescribeZones", True),
    "SLB": ("slb", "DescribeLoadBalancers", True),
    "NLB": ("nlb", "ListLoadBalancers", True),
    "CLOUDDNS": ("clouddns", "DescribePrivateZones", True),
}
# 探测顺序:先 ECS/VPC/ROS(后面的查询都靠它们),再其余。
PROBE_ORDER = ["ECS", "VPC", "ROS", "RAM", "NAS", "SLB", "NLB", "CLOUDDNS"]

# 端点探不到时,这几个是「没有就装不了」,其余是「没有也能跑,只是少个功能」。
REQUIRED_ENDPOINTS = {"ROS", "ECS", "VPC", "RAM", "CLOUDDNS"}


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def mask(v):
    if not v or len(v) < 8:
        return "****"
    return v[:4] + "…" + v[-4:]


# ── 元数据 ────────────────────────────────────────────────────────────────────
def meta(path, timeout=5):
    try:
        with urllib.request.urlopen(f"{META}/{path}", timeout=timeout) as r:
            return r.read().decode().strip()
    except Exception:
        return ""


# ── 调用 ─────────────────────────────────────────────────────────────────────
def cloudcli(product_key, action, env_extra, **params):
    """跑一次 cloudcli,返回 (rc, stdout, stderr)。永远不抛异常。"""
    argv = [sys.executable, CLOUDCLI, product_key, action]
    for k, v in params.items():
        argv += [f"--{k}", str(v)]
    env = dict(os.environ)
    env["CLOUD_PLATFORM"] = "apsara"
    env.update(env_extra)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:  # noqa: BLE001 - 探测工具不该因为任何一次失败而中断
        return 1, "", str(e)


def classify(rc, out, err):
    """把一次调用的结果分成:端点对 / 端点对但调用有问题 / 端点不对。"""
    blob = (out or "") + (err or "")
    if '"asapiSuccess":true' in blob.replace(" ", "") or (rc == 0 and blob.strip().startswith("{")):
        return "ok"
    for marker in ("InvalidAction.NotFound", "InvalidVersion", "NeedSsl",
                   "SignatureDoesNotMatch", "InvalidAccessKeyId",
                   "Forbidden", "NoPermission"):
        if marker in blob:
            return "reachable"          # 端点通,是调用本身的问题
    return "bad"


def resolves(host):
    try:
        socket.getaddrinfo(host, None, socket.AF_INET)
        return True
    except OSError:
        return False


def discover_endpoints(region, domain, found):
    """逐产品试形状。found 会被就地填充,后续调用直接用它做环境。"""
    results = {}
    for prod in PROBE_ORDER:
        key, action, needs_region = PROBES[prod]
        params = {"RegionId": region} if needs_region else {}
        hit, note = "", ""
        candidates = [p.format(svc=key, domain=domain, region=region) for p in PATTERNS]
        # 去重但保持顺序
        seen, ordered = set(), []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                ordered.append(c)
        resolving = [h for h in ordered if resolves(h)]
        if not resolving:
            note = "所有候选域名都不解析 —— 这套环境很可能没有部署这个产品"
        for host in resolving:
            env = dict(found)
            env[f"ENDPOINT_{prod}"] = host
            rc, out, err = cloudcli(key, action, env, **params)
            verdict = classify(rc, out, err)
            if verdict == "ok":
                hit, note = host, "调用成功"
                break
            if verdict == "reachable" and not hit:
                hit, note = host, f"端点可达,但调用返回:{(err or out).strip()[:80]}"
        if hit:
            found[f"ENDPOINT_{prod}"] = hit
        results[prod] = (hit, note, len(resolving))
        mark = "✓" if hit else ("·" if prod not in REQUIRED_ENDPOINTS else "✗")
        log(f"  {mark} {prod:<9} {hit or '(未找到)':<45} {note}")
    return results


# ── 各类事实 ──────────────────────────────────────────────────────────────────
def jget(out):
    try:
        return json.loads(out)
    except Exception:
        return {}


def dig(d, *path, default=None):
    """按路径取值,任何一层缺失都返回 default —— 网关的返回结构各环境略有出入。"""
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def discover_zones(region, env):
    rc, out, err = cloudcli("ecs", "DescribeZones", env, RegionId=region)
    zones = dig(jget(out), "Zones", "Zone", default=[]) or []
    ids = [z.get("ZoneId") for z in zones if z.get("ZoneId")]
    return ids


def discover_image(region, env):
    """挑一个 x86_64 的系统镜像。偏好 Alibaba Cloud Linux 3:mirror ECS 的
    cloud-init 要用它的源装 podman,而更老的镜像在专有云里常常连不上源。"""
    rc, out, err = cloudcli("ecs", "DescribeImages", env,
                            RegionId=region, Status="Available", PageSize=100)
    imgs = dig(jget(out), "Images", "Image", default=[]) or []
    x86 = [i for i in imgs if (i.get("Architecture") or "x86_64") == "x86_64"]
    if not x86:
        return "", f"DescribeImages 没有返回 x86_64 镜像({(err or out).strip()[:60]})"

    def score(i):
        name = (i.get("ImageId") or "") + " " + (i.get("OSName") or "")
        return (0 if "aliyun_3" in name else 1 if "aliyun_4" in name else 2, name)

    best = sorted(x86, key=score)[0]
    return best.get("ImageId", ""), f"共 {len(x86)} 个 x86_64 镜像,选了这个"


def available_resources(region, zone, env, dest):
    """DestinationResource 的可用清单(InstanceType / SystemDisk)。"""
    rc, out, err = cloudcli("ecs", "DescribeAvailableResource", env,
                            RegionId=region, ZoneId=zone,
                            DestinationResource=dest, InstanceChargeType="PostPaid")
    vals = []
    for az in dig(jget(out), "AvailableZones", "AvailableZone", default=[]) or []:
        for ar in dig(az, "AvailableResources", "AvailableResource", default=[]) or []:
            for sr in dig(ar, "SupportedResources", "SupportedResource", default=[]) or []:
                if sr.get("Status") in (None, "Available") and sr.get("Value"):
                    vals.append(sr["Value"])
    return sorted(set(vals))


def instance_type_specs(env):
    rc, out, err = cloudcli("ecs", "DescribeInstanceTypes", env)
    specs = {}
    for t in dig(jget(out), "InstanceTypes", "InstanceType", default=[]) or []:
        tid = t.get("InstanceTypeId")
        if tid:
            specs[tid] = (t.get("CpuCoreCount") or 0, float(t.get("MemorySize") or 0))
    return specs


def image_supported_types(image_id, env):
    rc, out, err = cloudcli("ecs", "DescribeImageSupportInstanceTypes", env, ImageId=image_id)
    return {t.get("InstanceTypeId") for t in
            dig(jget(out), "InstanceTypes", "InstanceType", default=[]) or []
            if t.get("InstanceTypeId")}


def pick_type(candidates, specs, min_cpu, min_mem):
    """在候选里挑满足下限的最小的一个 —— 够用就行,不挑大的(要花钱)。"""
    ok = [(specs[c][0], specs[c][1], c) for c in candidates
          if c in specs and specs[c][0] >= min_cpu and specs[c][1] >= min_mem]
    if not ok:
        return ""
    ok.sort()
    return ok[0][2]


def discover_nas_storage_type(region, env):
    """哪个盘类真的能开 NFS。DescribeZones 会把两种都列出来,但 Protocol 列表
    为空的那个虽然列着却卖不出去 —— ste2 的 Performance 就是空的。"""
    rc, out, err = cloudcli("nas", "DescribeZones", env, RegionId=region)
    zones = dig(jget(out), "Zones", "Zone", default=[]) or []
    usable = []
    for z in zones:
        for kind in ("Performance", "Capacity"):
            block = z.get(kind) or {}
            protocols = dig(block, "Protocol", default=[]) or []
            if isinstance(protocols, dict):
                protocols = protocols.get("Protocol", []) or []
            if any(str(p).upper() == "NFS" for p in protocols):
                usable.append(kind)
    if not usable:
        return "", f"DescribeZones 里没有任何盘类带 NFS({(err or out).strip()[:60]})"
    # Performance 更快,有就用;没有才退 Capacity。
    choice = "Performance" if "Performance" in usable else usable[0]
    return choice, f"可用盘类:{sorted(set(usable))}"


def discover_oss_endpoint(region, domain):
    """OSS 是数据面,没有只读探测动作可打 —— 只能按域名解析挑候选。"""
    cands = [f"oss-{region}-{s}.{domain}" for s in ("a", "b")] + [f"oss-{region}.{domain}"]
    live = [h for h in cands if resolves(h)]
    if not live:
        return "", f"这些候选都不解析:{cands}"
    return "https://" + live[0], f"解析成功的候选:{live}"


def cpu_needs_tcg():
    """phase 10 的 libguestfs appliance 在海光 C86 上起不来,必须软件模拟。
    分界是 CPU 厂商,不是发行版或内核。"""
    try:
        info = open("/proc/cpuinfo").read()
    except OSError:
        return None, "读不到 /proc/cpuinfo"
    vendor = ""
    family = ""
    for line in info.splitlines():
        if line.startswith("vendor_id") and not vendor:
            vendor = line.split(":", 1)[1].strip()
        if line.startswith("cpu family") and not family:
            family = line.split(":", 1)[1].strip()
    model = ""
    m = re.search(r"^model name\s*:\s*(.+)$", info, re.M)
    if m:
        model = m.group(1).strip()
    hygon = "hygon" in model.lower() or (vendor == "AuthenticAMD" and family == "24")
    return hygon, f"vendor_id={vendor} family={family} model={model or '?'}"


def cidr_overlaps(a, b):
    """两个 CIDR 有没有重叠 —— 对等连接两边网段重了路由会打架。"""
    def parse(c):
        net, bits = c.split("/")
        octets = [int(x) for x in net.split(".")]
        val = (octets[0] << 24) + (octets[1] << 16) + (octets[2] << 8) + octets[3]
        mask_bits = (0xFFFFFFFF << (32 - int(bits))) & 0xFFFFFFFF
        return val & mask_bits, mask_bits
    try:
        na, ma = parse(a)
        nb, mb = parse(b)
    except Exception:
        return None
    m = ma & mb
    return (na & m) == (nb & m)


# ── 写配置 ────────────────────────────────────────────────────────────────────
def set_key(lines, key, value, indent=0):
    """就地改一个键的值,保留行尾注释;键被注释掉的话顺带解开注释。"""
    pad = " " * indent
    pat = re.compile(rf"^{pad}#?\s*({re.escape(key)}):(\s*)(.*?)(\s*#.*)?$")
    for i, line in enumerate(lines):
        m = pat.match(line.rstrip("\n"))
        if not m:
            continue
        # 缩进要精确匹配,否则 cloud_env 里的 REGION 会命中顶层的同名键
        if len(line) - len(line.lstrip(" ")) != indent and not line.lstrip().startswith("#"):
            continue
        comment = m.group(4) or ""
        # 空值必须写成 ""。裸的空值 YAML 解析成 None,而 `None | length` 会直接
        # 抛异常 —— ENDPOINT_NAS 留空本来是「这套环境没有 NAS」的正常表达。
        sval = str(value)
        quoted = f'"{sval}"' if (sval == "" or re.search(r"[:\s#]", sval)) else sval
        lines[i] = f"{pad}{key}: {quoted}{comment}\n"
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="探测一套 Apsara 环境并产出 all.yml")
    ap.add_argument("-o", "--out", help="写出 all.yml 的路径(不给则只打印)")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    ap.add_argument("--domain", default=os.environ.get("DOMAIN", ""),
                    help="环境域名后缀,例 cloud.ste2.com(默认从 region 推)")
    args = ap.parse_args()

    for v in ("AK", "SK"):
        if not os.environ.get(v):
            sys.exit(f"discover: 需要环境变量 {v}(还有 ORG_ID / RG_ID)")
    if not os.path.exists(CLOUDCLI):
        sys.exit(f"discover: 找不到 {CLOUDCLI} —— 先跑 00a-prepare-operator.yml")

    log("── 1. 本机元数据 ───────────────────────────────────────────────")
    region = meta("region-id")
    if not region:
        sys.exit("discover: 取不到 region-id —— 这个脚本要在目标环境的 helper(Apsara ECS)上跑")
    vpc_id = meta("vpc-id")
    vpc_cidr = meta("vpc-cidr-block")
    zone_self = meta("zone-id")
    log(f"  region      {region}")
    log(f"  helper VPC  {vpc_id}  {vpc_cidr}")
    log(f"  helper zone {zone_self}")

    domain = args.domain
    if not domain:
        # cn-wulan-ste2-d01 -> ste2 -> cloud.ste2.com
        m = re.match(r"^cn-[a-z]+-([a-z0-9]+)-", region)
        domain = f"cloud.{m.group(1)}.com" if m else ""
    if not domain:
        sys.exit("discover: 推不出域名后缀,用 --domain cloud.<env>.com 指定")
    log(f"  域名后缀    {domain}  (--domain 可覆盖)")

    base_env = {
        "REGION": region,
        "ORG_ID": os.environ.get("ORG_ID", ""),
        "RG_ID": os.environ.get("RG_ID", ""),
    }
    if os.environ.get("APSARA_PROXY"):
        base_env["APSARA_PROXY"] = os.environ["APSARA_PROXY"]

    log("")
    log("── 2. 端点(逐个形状真实调用)──────────────────────────────────")
    found = dict(base_env)
    ep = discover_endpoints(region, domain, found)

    missing_required = [p for p in REQUIRED_ENDPOINTS if not ep[p][0]]
    has_nas = bool(ep["NAS"][0])

    log("")
    log("── 3. 这套环境卖什么 ──────────────────────────────────────────")
    zones = discover_zones(region, found) if ep["ECS"][0] else []
    zone = zones[0] if zones else (zone_self or CHANGEME)
    log(f"  可用区      {zones or '(查不到,先用 helper 自己的 ' + (zone_self or '?') + ')'}")
    if len(zones) > 1:
        log(f"              注意:这套环境有 {len(zones)} 个可用区,但多 AZ 路径在专有云上还没验证过")

    image_id, image_note = discover_image(region, found) if ep["ECS"][0] else ("", "ECS 端点没找到")
    log(f"  系统镜像    {image_id or CHANGEME}   {image_note}")

    disk = ""
    types_master = types_worker = types_mirror = ""
    if ep["ECS"][0] and zone != CHANGEME:
        disks = available_resources(region, zone, found, "SystemDisk")
        for pref in ("cloud_essd", "cloud_pperf", "cloud_sperf", "cloud_efficiency", "cloud_ssd"):
            if pref in disks:
                disk = pref
                break
        if not disk and disks:
            disk = disks[0]
        log(f"  盘型        {disk or CHANGEME}   该可用区可用:{disks or '(查不到)'}")

        avail = set(available_resources(region, zone, found, "InstanceType"))
        specs = instance_type_specs(found)
        supported = image_supported_types(image_id, found) if image_id else set()
        pool = sorted((avail & supported) if supported else avail)
        if not pool:
            log("  实例规格    查不到可用规格 —— DescribeAvailableResource / "
                "DescribeImageSupportInstanceTypes 都没有返回交集")
        else:
            # master 是 OpenShift 的控制面下限:4 vCPU / 16 GiB。
            types_master = pick_type(pool, specs, 4, 16)
            # mirror 首次构建时 oc-mirror 峰值要十几 GB 内存。
            types_mirror = pick_type(pool, specs, 4, 16)
            types_worker = pick_type(pool, specs, 2, 8) or types_master
            log(f"  实例规格    可用 {len(pool)} 种(镜像+可用区交集)")
            log(f"              master/mirror  {types_master or CHANGEME}")
            log(f"              worker         {types_worker or CHANGEME}")

    nas_type, nas_note = ("", "")
    if has_nas:
        nas_type, nas_note = discover_nas_storage_type(region, found)
        log(f"  NAS         有;盘类 {nas_type or CHANGEME}   {nas_note}")
    else:
        log("  NAS         没有 —— ENDPOINT_NAS 留空即可,整条链会自动跳过 NAS")

    oss_ep, oss_note = discover_oss_endpoint(region, domain)
    log(f"  OSS 端点    {oss_ep or CHANGEME}   {oss_note}")
    if oss_ep:
        log("              ⚠ 只验证了域名解析。bucket 必须属于上面 AK/SK 的账号,"
            "否则端点全对也会 AccessDenied")

    hygon, cpu_note = cpu_needs_tcg()
    log(f"  CPU         {cpu_note}")
    if hygon:
        log("              → libguestfs 在这类 CPU 上起不来,已置 bootimage_force_tcg: true")

    overlap = cidr_overlaps(vpc_cidr, "10.0.0.0/16") if vpc_cidr else None
    if overlap:
        log(f"  ⚠ 网段      helper VPC 是 {vpc_cidr},和默认的 10.0.0.0/16 重叠。"
            "对等路由会打架 —— 改 vpc_cidr / private_subnet_cidr* 换一段")

    # ── 汇总 ──────────────────────────────────────────────────────────────
    values = {
        "region": region,
        "zone": zone,
        "zone2": zone,
        "zone3": zone,
        "apsara_image_id": image_id or CHANGEME,
        "control_plane_type": types_master or CHANGEME,
        "compute_type": types_master or CHANGEME,
        "worker_instance_type": types_worker or CHANGEME,
        "mirror_instance_type": types_mirror or CHANGEME,
        "system_disk_category": disk or CHANGEME,
        "apsara_oss_endpoint": oss_ep or CHANGEME,
        "apsara_peer_operator_vpc_id": vpc_id or CHANGEME,
    }
    env_values = {
        "AK": os.environ["AK"],
        "SK": os.environ["SK"],
        "ORG_ID": os.environ.get("ORG_ID", "") or CHANGEME,
        "RG_ID": os.environ.get("RG_ID", "") or CHANGEME,
    }
    for prod in PROBE_ORDER:
        host = ep[prod][0]
        # NAS 的空值是有意义的(= 这套环境没有 NAS),不要写成 CHANGEME
        if prod == "NAS":
            env_values["ENDPOINT_NAS"] = host
        elif prod == "SLB":
            env_values["ENDPOINT_SLB"] = host
        elif prod == "NLB":
            continue          # 没有任何代码读 ENDPOINT_NLB,只在上面的探测结果里报一句
        else:
            env_values[f"ENDPOINT_{prod}"] = host or CHANGEME
    if os.environ.get("APSARA_PROXY"):
        env_values["APSARA_PROXY"] = os.environ["APSARA_PROXY"]
    if nas_type:
        values["nas_storage_type"] = nas_type
    if hygon:
        values["bootimage_force_tcg"] = "true"

    log("")
    log("── 4. 结果 ────────────────────────────────────────────────────")
    for k, v in values.items():
        log(f"  {k:<28} {v}")
    for k, v in env_values.items():
        shown = mask(v) if k in ("AK", "SK") else (v or "(留空 = 这套环境没有)")
        log(f"  cloud_env.{k:<18} {shown}")

    unresolved = [k for k, v in list(values.items()) + list(env_values.items())
                  if v == CHANGEME]
    log("")
    if missing_required:
        log(f"  ✗ 必需的端点没找到:{missing_required} —— 装不下去。"
            f"用 scripts/apsara/probe-endpoints.sh {domain} --call 手工再看一遍")
    if unresolved:
        log(f"  ⚠ 还有 {len(unresolved)} 项没能确定,写成了 {CHANGEME}:{unresolved}")
    else:
        log("  ✓ 所有项都确定了")

    if not args.out:
        log("")
        log("  (加 -o ansible/group_vars/all.yml 把这些写进配置文件)")
        return 0 if not missing_required else 1

    if os.path.exists(args.out) and not args.force:
        sys.exit(f"discover: {args.out} 已存在 —— 加 --force 覆盖,或换个 -o 路径")
    if not os.path.exists(TEMPLATE):
        sys.exit(f"discover: 找不到底稿 {TEMPLATE}")

    lines = open(TEMPLATE).readlines()
    for k, v in values.items():
        if not set_key(lines, k, v, indent=0):
            log(f"  ! 底稿里没有键 {k},跳过")
    for k, v in env_values.items():
        if not set_key(lines, k, v, indent=2):
            log(f"  ! 底稿的 cloud_env 里没有键 {k},跳过")

    with open(args.out, "w") as f:
        f.writelines(lines)
    os.chmod(args.out, 0o600)          # 里面有 AK/SK
    log("")
    log(f"  已写出 {args.out}(权限 600,内含 AK/SK;该路径已被 .gitignore)")
    log("  还要自己填的:cluster_name / base_domain / openshift_version,以及上面所有 "
        f"{CHANGEME}")
    return 0 if not missing_required else 1


if __name__ == "__main__":
    sys.exit(main())
