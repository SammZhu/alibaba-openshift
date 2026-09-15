#!/usr/bin/env python3
"""从红帽产品生命周期查出「正常支持的最老 OCP minor」,并用它更新 bootimage/oldest-supported-minor。

**为什么要有这个**:floor 决定了每天烤哪些 RHCOS,也就决定了用户装机时菜单上有哪些
版本(`bootimage/provenance/*.yaml`)。它以前是手写的一行,没有参照系 —— 写 4.20
不是因为 4.20 是支持窗口的下沿,只是因为当时手边是 4.20。红帽每 4 个月发一个 minor,
手写的数字必然过期:要么菜单里躺着早已 EOL 的版本,要么支持窗口内的版本根本没烤。
改成每次跑之前从红帽自己的生命周期数据推出来,这个下沿才有意义。

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

**不确定的时候什么都不改**。三种情形各自区分,谁也不许冒充「查到了」:

    exit 0  查到了(写了,或者本来就一样)
    exit 2  没查到 —— 网络不通/接口挂了。沿用已提交的 floor,照常烤。
    exit 3  查到了但数据不可信 —— schema 变了、一个 Full Support 都没有、
            或者和当前 floor 差得太远。同样沿用旧值,但这是要人看的告警。

  python3 scripts/rh_oldest_supported_minor.py                      # 只打印
  python3 scripts/rh_oldest_supported_minor.py --write bootimage/oldest-supported-minor
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bootimage_detect import _fetch_json  # noqa: E402

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
    ap.add_argument("--write", metavar="FILE",
                    help="就地更新这个文件的版本行(只在值变了时才写)")
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

    current = args.current or (read_floor(args.write) if args.write else None)
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
            print(floor)
            return 0
        direction = "下调" if b < a else "上调"
        sys.stderr.write(f"[floor] {direction}:{cur} → {floor}\n")

    if args.write:
        write_floor(args.write, floor)
        sys.stderr.write(f"[floor] 已写入 {args.write}\n")
    print(floor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
