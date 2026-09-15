#!/usr/bin/env python3
"""红帽产品生命周期 —— 供应链据此决定**烤哪些版本**、**留哪些配方**。

两件事都是同一份数据的不同切面,所以放在一个模块里:判据只有一处,取数只有一次。

  --write-floor FILE   把「正常支持的最老 minor」写进 FILE(bootimage/oldest-supported-minor)
  --retire DIR         把已经彻底不受支持的 provenance 配方退役掉(默认只报,加 --delete 才删)

**为什么下沿要推导**:floor 决定每天扫哪些 minor,也就决定用户装机时菜单上有哪些
版本。它以前是手写的一行,没有参照系 —— 写 4.20 不是因为 4.20 是支持窗口的下沿,
只是因为当时手边是 4.20。红帽每 4 个月发一个 minor,手写的数字必然过期。

**「正常支持」是什么** —— 这里是全部的坑所在。接口里的 `type` 有四种:

    Full Support / Maintenance Support / Extended Support / End of life

`Extended Support` 是 EUS,要单独订阅,而且**它和 End of life 在版本序列里是交错的**。
2026-09-15 实际数据:

    4.22 Full        4.21 Full        4.20 Maint      4.19 Maint      4.18 Maint
    4.17 EOL         4.16 Extended    4.15 EOL        4.14 Extended   4.13 EOL
    4.12 Extended    4.11 EOL ...

于是「最老的那个还没 EOL 的版本」会给出 **4.12** —— 一个看上去完全合理的答案,
结果是从 4.12 开始扫,烤十个 minor,菜单里塞满没人该装的版本。正确答案是 4.18。
所以 NORMAL_SUPPORT 只认前两种,EUS 明确排除:它是加钱买的延长线,不是「正常支持」。

**退役用的是另一条判据,而且是逐版本判的,不是一条线**。烤与不烤看的是「正常支持」,
删与不删要等到 **EOL 和 EUS 都走完**,也就是 `type == End of life`。两者之间那段
(今天的 4.12 / 4.14 / 4.16)是「不再烤新的,但配方留着」—— 注意它们比已经 EOL 的
4.13 / 4.15 / 4.17 **还老**,所以退役不可能用一条下沿线表达,只能一个一个版本问。

退役删的是 `bootimage/provenance/*.yaml`。CI 烤完不留任何云上资源(bake-one.sh 在
mktemp 里干活、`skip_upload_until_gate=true`),真正的 ECS 自定义镜像是部署时 phase 10
建的、随集群 teardown 回收 —— 所以会随时间堆积的只有这些配方文件。它们不占什么空间,
代价是**菜单噪音**:site-apsara 就拿这个列表校验 openshift_version,留着 EOL 条目
等于让新手挑到一个不再有补丁的版本,而且装得一路绿灯。删掉是可逆的(在 git 里),
真要找回来就 `git checkout <sha> -- bootimage/provenance/<file>`。

**不确定的时候什么都不改**。三种情形各自区分,谁也不许冒充「查到了」:

    exit 0  查到了(写了/删了,或者本来就无事可做)
    exit 2  没查到 —— 网络不通/接口挂了。floor 和配方都不动,照常烤。
    exit 3  查到了但数据不可信 —— schema 变了、一个 Full Support 都没有、
            下沿跳得太远、或者退役集合大到不像话。同样什么都不动,但这是要人看的告警。

  python3 scripts/rh_lifecycle.py                       # 只打印分类表
  python3 scripts/rh_lifecycle.py --write-floor bootimage/oldest-supported-minor \
                                  --retire bootimage/provenance --delete
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bootimage_detect import _fetch_json, _grep1, minor  # noqa: E402

LIFECYCLE_URL = ("https://access.redhat.com/product-life-cycles/api/v1/products"
                 "?name=Openshift%20Container%20Platform")

PRODUCT = "Red Hat OpenShift Container Platform"

# access.redhat.com 在 Akamai 后面,对 urllib 的默认 UA 回 403(同一个 URL 用 curl
# 是 200)。必须带一个像样的 UA —— 这个坑只在真跑的时候才现形,用 fixture 测不出来。
HEADERS = {"User-Agent": "alibaba-openshift-bootimage/1.0 (+rhcos aliyun supply chain)",
           "Accept": "application/json"}

# 只有这两种算「正常支持」。Extended Support(EUS)是另买的延长线,End of life 不用说。
# 见上面的模块注释:漏掉这条过滤,答案会从 4.18 变成 4.12,而且看起来很像对的。
NORMAL_SUPPORT = ("full support", "maintenance support")

# 退役的判据 —— 和上面那条是两回事,别合并。EOL 意味着连 EUS 也走完了;
# `extended support` 的版本虽然早已不烤,配方却必须留着,因为还有人买着延长线在跑。
DEAD = "end of life"

MINOR_RE = re.compile(r"^(\d+)\.(\d+)$")


class FloorUndecidable(Exception):
    """数据到手了,但不足以下结论。exit 3 —— 要人看。"""


def _mkey(m):
    a, b = m.split(".")
    return (int(a), int(b))


def parse_versions(doc):
    """(minor, 支持类型) 列表,只保留 X.Y 形式的名字。

    接口里还有 `3` 和 `4.6 EUS` 这种名字。它们都早已 EOL,所以丢掉不影响答案 ——
    但要丢得明明白白,不能让一个没预料到的名字格式把整个列表静默截断。
    """
    prods = doc.get("data") or []
    hit = [p for p in prods if p.get("name") == PRODUCT]
    if not hit:
        raise FloorUndecidable(
            f"生命周期接口里没有 {PRODUCT!r}(拿到 {[p.get('name') for p in prods]}) "
            f"—— 产品改名或接口改版了")
    out, skipped = [], []
    for v in hit[0].get("versions") or []:
        name = str(v.get("name", "")).strip()
        typ = re.sub(r"\s+", " ", str(v.get("type", ""))).strip().lower()
        if MINOR_RE.match(name):
            out.append((name, typ))
        else:
            skipped.append(name)
    if not out:
        raise FloorUndecidable("生命周期接口里一个 X.Y 形式的版本都没有 —— schema 变了")
    return out, skipped


def oldest_normally_supported(doc):
    """正常支持(Full + Maintenance)里最老的那个 minor。"""
    versions, skipped = parse_versions(doc)
    supported = sorted((n for n, t in versions if t in NORMAL_SUPPORT), key=_mkey)
    full = [n for n, t in versions if t == "full support"]
    # 任何时候都至少有一个 Full Support 的 minor。一个都没有,说明 type 的取值
    # 变了(大小写、措辞、或者换字段),而不是红帽真的停止支持 OpenShift 了。
    # 这种时候 supported 很可能是空的或者残缺的 —— 不能拿它当答案。
    if not full:
        raise FloorUndecidable(
            "没有任何一个 minor 是 Full Support —— `type` 的取值多半变了,"
            f"实际见到:{sorted({t for _, t in versions})}")
    if not supported:
        raise FloorUndecidable("正常支持的版本集合为空")
    return supported[0], supported, skipped


def type_map(doc):
    """minor -> 支持类型(小写)。"""
    versions, _ = parse_versions(doc)
    return {n: t for n, t in versions}


def classify_provenance(prov_dir, tmap, floor):
    """每个配方一条 (文件名, ocp minor, 处置, 理由)。处置 ∈ {retire, keep}。

    「查不到」在这里有**两个方向,而且结论相反**,所以绝不能合成一句「不认识,删了」:

      比列表里最老的还老 → 老到红帽的生命周期页都不再列它,确实该退役。
      比列表里最新的还新 → 还没 GA(例如 4.23 的预览配方)。删它就是把还没上市的
                           版本从菜单上抹掉 —— 正好搞反。

    这一条是「探不到≠不存在」的直接应用:同一个「不在列表里」,两个方向的答案相反。
    """
    listed = sorted(tmap, key=_mkey)
    oldest, newest = listed[0], listed[-1]
    out = []
    for path in sorted(glob.glob(os.path.join(prov_dir, "*.yaml"))):
        base = os.path.basename(path)
        if base == "example.yaml":
            continue
        # schemaVersion 2 起是 ocpMinor。旧的 ocpVersion 是个 z,而且那个 z 的
        # 含义只是「烤的时候该 minor 最新的 z」—— 退役只关心 minor 的支持状态,
        # 两者取 minor 之后等价,所以读不到新字段时回落到旧字段仍然正确。
        ocp = _grep1(path, "ocpMinor") or _grep1(path, "ocpVersion")
        if not ocp:
            out.append((base, None, "keep", "读不出 ocpMinor —— 不认识的东西不删"))
            continue
        m = minor(ocp)
        pin = _grep1(path, "retain")
        if pin:
            out.append((base, m, "keep", f"retain: {pin}"))
            continue
        typ = tmap.get(m)
        if typ is None:
            if _mkey(m) < _mkey(oldest):
                out.append((base, m, "retire", f"比生命周期表里最老的 {oldest} 还老"))
            else:
                out.append((base, m, "keep", f"比表里最新的 {newest} 还新 —— 尚未 GA"))
            continue
        if typ == DEAD:
            out.append((base, m, "retire", "End of life —— EOL 和 EUS 都走完了"))
        else:
            out.append((base, m, "keep", typ))
    return out


def retire_provenance(prov_dir, tmap, floor, delete, max_retire_ratio=0.5):
    """按生命周期退役配方。返回要退役的文件名列表;delete 为真才真删。

    三道栏杆,任何一道响了就一个文件都不动 —— 退役是批量动作,错了是整张菜单。
    """
    rows = classify_provenance(prov_dir, tmap, floor)
    for base, m, act, why in rows:
        sys.stderr.write(f"[retire] {act:6} {base:28} OCP {m or '?':6} {why}\n")
    doomed = [b for b, _, act, _ in rows if act == "retire"]
    kept = [b for b, _, act, _ in rows if act == "keep"]
    if not doomed:
        return []

    # 栏杆 1:退役的版本必须严格老于烤的下沿。下沿在 Maintenance 里,按定义不可能
    # 是 EOL —— 真撞上了,说明生命周期数据自相矛盾,不是我们该顺着删的时候。
    bad = [(b, m) for b, m, act, _ in rows if act == "retire" and _mkey(m) >= _mkey(floor)]
    if bad:
        raise FloorUndecidable(
            f"要退役的版本不比下沿 {floor} 老:{bad} —— 生命周期数据自相矛盾,一个都不删")

    # 栏杆 2:不许把菜单清空。一个版本都不剩,装机时每个 openshift_version 都会被拦下。
    if not kept:
        raise FloorUndecidable(
            f"这样会删光所有 {len(doomed)} 个配方,菜单会空 —— 一个都不删")

    # 栏杆 3:一次删掉一半以上,不像是「又老了一个版本」,像是数据出了问题。
    if len(doomed) > max_retire_ratio * len(rows):
        raise FloorUndecidable(
            f"一次要删 {len(doomed)}/{len(rows)} 个配方,超过一半 —— 更像数据出问题"
            f"而不是版本正常老去,一个都不删")

    if delete:
        for b in doomed:
            os.remove(os.path.join(prov_dir, b))
        sys.stderr.write(f"[retire] 已删除 {len(doomed)} 个配方(在 git 里可恢复)\n")
    else:
        sys.stderr.write(f"[retire] dry-run:{len(doomed)} 个配方会被删(加 --delete 才真删)\n")
    return doomed


def read_floor(path):
    """已提交的 floor:第一行非注释非空行。和 bootimage_detect.version_from_file 同义。"""
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    raise FloorUndecidable(f"{path} 里没有版本行")


def write_floor(path, new):
    """只替换那一行,注释原样留着 —— 那些注释是解释这个文件为什么存在的,不能被机器吃掉。"""
    lines = open(path, encoding="utf-8").readlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#"):
            lines[i] = new + "\n"
            open(path, "w", encoding="utf-8").writelines(lines)
            return
    raise FloorUndecidable(f"{path} 里没有版本行")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-floor", "--write", metavar="FILE", dest="write_floor",
                    help="就地更新这个文件的版本行(只在值变了时才写)")
    ap.add_argument("--retire", metavar="DIR",
                    help="按生命周期退役 DIR 下已彻底不受支持的 provenance 配方")
    ap.add_argument("--delete", action="store_true",
                    help="--retire 真的删文件(默认只报不删)")
    ap.add_argument("--current", help="当前 floor(不写文件时也想做距离校验就给这个)")
    ap.add_argument("--max-move", type=int, default=3, metavar="N",
                    help="和当前 floor 相差超过 N 个 minor 就拒绝采信(默认 3)。"
                         "支持窗口一共才五个 minor,而且这个 job 每天跑 —— "
                         "一次跳三格以上只能是数据出了问题")
    ap.add_argument("--url", default=LIFECYCLE_URL)
    ap.add_argument("--from-file", help="调试用:从本地 JSON 读,不联网")
    args = ap.parse_args(argv)

    try:
        if args.from_file:
            doc = json.load(open(args.from_file, encoding="utf-8"))
        else:
            doc = _fetch_json(args.url, headers=HEADERS)
            if doc is None:               # 确认过的 404
                raise RuntimeError(f"{args.url} 404 —— 接口路径变了")
    except FloorUndecidable:
        raise
    except Exception as e:                                       # noqa: BLE001
        sys.stderr.write(f"[floor] 取生命周期数据失败:{e}\n")
        return 2

    try:
        floor, supported, skipped = oldest_normally_supported(doc)
    except FloorUndecidable as e:
        sys.stderr.write(f"[floor] {e}\n")
        return 3

    sys.stderr.write(f"[floor] 正常支持(Full+Maintenance):{' '.join(supported)}\n")
    if skipped:
        sys.stderr.write(f"[floor] 跳过非 X.Y 的版本名:{' '.join(skipped)}\n")

    current = args.current or (read_floor(args.write_floor) if args.write_floor else None)
    if current:
        cur_m = re.match(r"^(\d+\.\d+)", current)
        if not cur_m:
            sys.stderr.write(f"[floor] 当前 floor {current!r} 解析不出 minor\n")
            return 3
        cur = cur_m.group(1)
        a, b = _mkey(cur), _mkey(floor)
        dist = abs(b[1] - a[1]) if a[0] == b[0] else 99
        if dist > args.max_move:
            sys.stderr.write(
                f"[floor] 算出来是 {floor},当前是 {cur},差 {dist} 个 minor —— "
                f"超过 --max-move={args.max_move},不采信,沿用 {cur}\n")
            return 3
        if cur == floor:
            sys.stderr.write(f"[floor] 已经是 {floor},不动\n")
        else:
            direction = "下调" if b < a else "上调"
            sys.stderr.write(f"[floor] {direction}:{cur} → {floor}\n")

    # floor 没变也要继续往下走 —— 退役跟 floor 动没动没关系,是跟日历有关系的。
    # 早先这里直接 return 0,那样退役一年也执行不了几次。
    if args.write_floor and (current is None or current != floor):
        write_floor(args.write_floor, floor)
        sys.stderr.write(f"[floor] 已写入 {args.write_floor}\n")

    if args.retire:
        try:
            retire_provenance(args.retire, type_map(doc), floor, args.delete)
        except FloorUndecidable as e:
            sys.stderr.write(f"[retire] {e}\n")
            print(floor)
            return 3

    print(floor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
