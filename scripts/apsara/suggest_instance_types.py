#!/usr/bin/env python3
"""当配置的 worker 机型没库存时,说出哪些机型有。

为什么值得单独写一个脚本:2026-09-19 ste2 上 `ecs.g6x-hg-k10-c1m2.xlarge` 整族
SoldOut,phase 12 只会报「没货」然后让操作者自己去换一个 —— 而换成什么,得手工
翻 449 行 API 输出。但那 449 行**就在同一个响应里**:

    cloudcli ecs DescribeAvailableResource --RegionId <r> --ZoneId <z> \\
      --DestinationResource InstanceType          # 不带 --InstanceType = 枚举全部

一次调用就能分出 89 个有货 / 360 个无货。让报错自带答案,比让人去翻强。

这个脚本是**建议性**的:任何失败都只打印一行原因并 exit 0。它永远不该成为
装机路上新的失败点 —— 它存在的场合,本来就已经是一次失败了。

用法:

    suggest_instance_types.py --cloud-cli <path> --region <r> --zone <z> \\
        --like ecs.g6x-hg-k10-c1m2.xlarge [--limit 10]

自测(不需要云):

    python3 scripts/apsara/suggest_instance_types.py --self-test
"""

import argparse
import json
import subprocess
import sys


def in_stock_types(payload):
    """从 DescribeAvailableResource 的响应里取出有货的机型。

    两层都要读,而且含义不同:可用区那层的 Status 说的是**可用区**能不能用,
    机型那层的 Status 才是**这个机型**有没有货。2026-09-19 ste2 上可用区是
    Available/WithStock 而底下的机型是 SoldOut/WithoutStock —— 只看外层会报出
    一个不存在的库存。
    """
    out = set()
    zones = (payload or {}).get("AvailableZones") or {}
    if isinstance(zones, dict):
        zones = zones.get("AvailableZone") or []
    for z in zones:
        for res in ((z.get("AvailableResources") or {}).get("AvailableResource") or []):
            for sr in ((res.get("SupportedResources") or {}).get("SupportedResource") or []):
                if sr.get("Status") == "Available" and sr.get("Value"):
                    out.add(sr["Value"])
    return out


def specs(payload):
    """InstanceTypeId -> (vCPU, GiB)。缺字段的条目跳过,不猜。"""
    out = {}
    its = (payload or {}).get("InstanceTypes") or {}
    if isinstance(its, dict):
        its = its.get("InstanceType") or []
    for i in its:
        tid = i.get("InstanceTypeId")
        cpu, mem = i.get("CpuCoreCount"), i.get("MemorySize")
        if tid and cpu is not None and mem is not None:
            try:
                out[tid] = (int(cpu), float(mem))
            except (TypeError, ValueError):
                continue
    return out


def suggest(avail_payload, types_payload, like, limit=10):
    """有货、且不小于 `like` 规格的机型,按规格升序。

    不小于而不是「相近」:操作者是被迫换型的,给一个更小的机型等于把一次容量
    故障换成一次说不清的性能故障。同族优先 —— 同族的盘型/网络能力通常一致,
    换起来意外最少。
    """
    sp = specs(types_payload)
    stock = in_stock_types(avail_payload)
    base = sp.get(like)
    if base is None:
        return None, f"规格表里没有 {like},无法比较"
    if not stock:
        return [], "该可用区没有任何机型报有货"
    fam = like.split(".")[1].split("-")[0] if "." in like else ""
    rows = []
    for tid in stock:
        if tid == like:          # 它自己另说,见 main();混在候选里读着别扭
            continue
        s = sp.get(tid)
        if s and s[0] >= base[0] and s[1] >= base[1]:
            same_family = tid.split(".")[1].split("-")[0] == fam if "." in tid else False
            rows.append((0 if same_family else 1, s[0], s[1], tid))
    rows.sort()
    return [(t, c, m) for _, c, m, t in rows[:limit]], None


def _run(cli, region, zone, *extra):
    argv = [cli, "ecs", *extra, "--RegionId", region]
    if zone:
        argv += ["--ZoneId", zone]
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[0][:160]
                           if (p.stderr or p.stdout).strip() else f"rc={p.returncode}")
    return json.loads(p.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud-cli")
    ap.add_argument("--region")
    ap.add_argument("--zone")
    ap.add_argument("--like")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not all([a.cloud_cli, a.region, a.like]):
        print("suggest: 缺参数,跳过建议")
        return 0
    try:
        avail = _run(a.cloud_cli, a.region, a.zone,
                     "DescribeAvailableResource", "--DestinationResource", "InstanceType")
        types = _run(a.cloud_cli, a.region, None, "DescribeInstanceTypes")
    except Exception as e:                                   # 建议性,绝不阻断
        print(f"suggest: 探测失败,跳过建议({e})")
        return 0
    # 库存是快照不是判决(#103)。这个脚本被调用的前提是 a.like 没货,但从那次
    # 探测到这次可能已经过去几十秒 —— 2026-09-19 ste2 上同一个机型 09:47 无货、
    # 15:06 有货。真回来了,这是最该单独说的一件事,不是候选列表里的一行。
    if a.like in in_stock_types(avail):
        print(f"suggest: {a.like} 现在报有货了 —— 库存是会变的,可以直接重试,不必换型。")

    rows, why = suggest(avail, types, a.like, a.limit)
    if rows is None or why:
        print(f"suggest: {why}")
        return 0
    if not rows:
        print(f"suggest: 没有找到不小于 {a.like} 且有货的机型")
        return 0
    print(f"有货、且规格不小于 {a.like} 的候选(同族优先):")
    for tid, cpu, mem in rows:
        print(f"    {tid:<34} {cpu:>3} vCPU  {mem:>6.1f} GiB")
    print("  换机型族前先确认系统盘类型仍支持:")
    print("    DescribeAvailableResource --DestinationResource SystemDisk --InstanceType <type>")
    return 0


# ── 自测 ────────────────────────────────────────────────────────────────
def _avail(*pairs):
    return {"AvailableZones": {"AvailableZone": [{
        "ZoneId": "z-a", "Status": "Available", "StatusCategory": "WithStock",
        "AvailableResources": {"AvailableResource": [{
            "Type": "InstanceType",
            "SupportedResources": {"SupportedResource": [
                {"Value": v, "Status": s} for v, s in pairs]}}]}}]}}


def _types(*triples):
    return {"InstanceTypes": {"InstanceType": [
        {"InstanceTypeId": t, "CpuCoreCount": c, "MemorySize": m} for t, c, m in triples]}}


def self_test():
    fails = []

    def check(name, ok):
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            fails.append(name)

    # 真实形状:可用区说 WithStock,而机型说 SoldOut —— 外层不能当判据
    a = _avail(("ecs.g6x-hg-k10-c1m2.xlarge", "SoldOut"),
               ("ecs.s7-hg-k-c1m4.xlarge", "Available"))
    check("可用区 WithStock 不代表机型有货", in_stock_types(a) == {"ecs.s7-hg-k-c1m4.xlarge"})

    t = _types(("ecs.g6x-hg-k10-c1m2.xlarge", 4, 8.0),
               ("ecs.s7-hg-k-c1m4.xlarge", 4, 16.0),
               ("ecs.s7-hg-k-c1m1.large", 2, 2.0),
               ("ecs.s6-hg-k-c1m2.xlarge", 4, 8.0))
    a2 = _avail(("ecs.g6x-hg-k10-c1m2.xlarge", "SoldOut"),
                ("ecs.s7-hg-k-c1m4.xlarge", "Available"),
                ("ecs.s7-hg-k-c1m1.large", "Available"),
                ("ecs.s6-hg-k-c1m2.xlarge", "Available"))
    rows, why = suggest(a2, t, "ecs.g6x-hg-k10-c1m2.xlarge")
    ids = [r[0] for r in rows or []]
    check("不推荐比基准小的机型", "ecs.s7-hg-k-c1m1.large" not in ids)
    check("推荐了更大的和同规格的", set(ids) == {"ecs.s7-hg-k-c1m4.xlarge", "ecs.s6-hg-k-c1m2.xlarge"})
    check("SoldOut 的基准自己不会被推荐", "ecs.g6x-hg-k10-c1m2.xlarge" not in ids)

    # 基准机型自己有货时,不混进候选列表(main 会单独说一句"可以直接重试")
    a3 = _avail(("ecs.s6-hg-k-c1m2.xlarge", "Available"),
                ("ecs.s7-hg-k-c1m4.xlarge", "Available"))
    rows_self, _ = suggest(a3, t, "ecs.s6-hg-k-c1m2.xlarge")
    check("基准自己有货时不出现在候选里",
          [r[0] for r in rows_self] == ["ecs.s7-hg-k-c1m4.xlarge"])

    # 同族优先:两个候选规格完全相同,只有族不同 —— 同族的那个必须排前面。
    # (旧写法断言"基准自己排第一",基准被排除后才发现它其实什么都没证明。)
    t2 = _types(("ecs.s6-hg-k-c1m2.xlarge", 4, 8.0),
                ("ecs.s6-hg-k-c1m4.xlarge", 4, 16.0),
                ("ecs.s7-hg-k-c1m4.xlarge", 4, 16.0))
    a4 = _avail(("ecs.s6-hg-k-c1m4.xlarge", "Available"),
                ("ecs.s7-hg-k-c1m4.xlarge", "Available"))
    rows2, _ = suggest(a4, t2, "ecs.s6-hg-k-c1m2.xlarge")
    check("同族优先(同规格时)",
          [r[0] for r in rows2] == ["ecs.s6-hg-k-c1m4.xlarge", "ecs.s7-hg-k-c1m4.xlarge"])

    # 空信封(#102 那种)不能崩,也不能假装有答案
    check("空信封 -> 空集合", in_stock_types({"RequestId": "x"}) == set())
    rows3, why3 = suggest({"RequestId": "x"}, t, "ecs.g6x-hg-k10-c1m2.xlarge")
    check("空信封 -> 说没有报有货,而不是崩", rows3 == [] and why3 is not None)

    # 规格表里没有基准机型时,如实说不知道,不猜
    _, why4 = suggest(a2, _types(("ecs.other.x", 4, 8.0)), "ecs.g6x-hg-k10-c1m2.xlarge")
    check("基准不在规格表 -> 说不知道", why4 is not None)

    # 字段缺失/类型古怪的条目跳过,不整体崩
    check("规格表里的坏条目被跳过",
          specs({"InstanceTypes": {"InstanceType": [
              {"InstanceTypeId": "a"}, {"InstanceTypeId": "b", "CpuCoreCount": "x", "MemorySize": 1},
              {"InstanceTypeId": "c", "CpuCoreCount": 2, "MemorySize": 4}]}}) == {"c": (2, 4.0)})

    print()
    if fails:
        print(f"FAILED: {len(fails)} 项 —— {fails}")
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
