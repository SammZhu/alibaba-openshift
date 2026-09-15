#!/usr/bin/env python3
"""挑 karg 漂移比对的基线:同一 RHEL 代次里最新的那条 provenance。

  python3 scripts/pick_baseline.py <provenance-dir> <本次的 rhcosVersion>

打印选中条目的 rhcosVersion(也就是文件名去掉 .yaml);**没有合适的就什么都不打印
并退出 0** —— 「这一代还没有过条目」是正常状态(每个新 RHEL 代次第一次都是),
不是错误。

## 为什么不能用文件名排序

上游换过 RHCOS 的命名方案,分界在 4.19:

    4.17  417.94.202607240132-0     老:OCP minor + RHEL 9.4 + 时间戳
    4.18  418.94.202608142238-0     老(最后一个)
    4.19  9.6.20260818-0            新:RHEL 基座 + 日期
    4.20  9.6.20260818-0            (和 4.19 同一个 RHCOS)
    4.22  10.2.20260715-0           新,RHEL 10

于是目录里字典序是这样的:

    10.2.20260715-0   ← 最新的 RHEL 10,排在最前
    418.94.2026...    ← RHEL 9.4
    9.6.20260818-0    ← 排在最后

`sort | tail -1` 永远挑到 9.6 那一代。烤 10.x 镜像(4.22 起的全部版本)时,
基线会是一条 9.x —— **跨代比 karg**,而这正是同代规则要防的事:两个 RHEL 基座的
karg 集合本就可能不同,跨代比会否掉一个好镜像。4.22 那次烤就是这么过的,碰巧
karg 一致而已。

## 为什么不能用 `${ver%%.*}` 取代次

那样 `418.94.…` 取出来是 **418**(OCP minor),不是 RHEL 代次 9。结果是 4.18 找不到
任何同代条目 —— 而 9.x 的条目明明有六条 —— 然后日志还会打一句「本代尚无 provenance
条目」。那是**假话**,比不比对更糟。

## 归一化

两种方案都能解出 (RHEL 大版本, RHEL 小版本, 日期):

    418.94.202608142238-0  ->  (9, 4, 20260814)
    9.6.20260818-0         ->  (9, 6, 20260818)
    10.2.20260715-0        ->  (10, 2, 20260715)

时间戳长度不一样(12 位 vs 8 位),所以只取前 8 位按日期比 —— 直接当整数比会把
202608142238 排到 20260818 后面去。
"""
import os
import re
import sys

# 老方案:第一段是 OCP minor,一律以 4 开头(4.9 -> "49",4.18 -> "418");
# 第二段两位数是 RHEL 大小版本("94" -> 9.4)。
# 新方案:第一段就是 RHEL 大版本(9 / 10 / …),不会以 4 开头。
# 这个判据的前提是 RHEL 不会出到 4x 代 —— 老方案在 4.19 就停用了,不会再有新形态。
OLD = re.compile(r"^4(\d{1,2})\.(\d)(\d)\.(\d{8})\d*-(\d+)$")
NEW = re.compile(r"^(\d+)\.(\d+)\.(\d{8})\d*-(\d+)$")


def parse(version):
    """-> (rhel_major, rhel_minor, date8, build)。解不出来返回 None。"""
    m = OLD.match(version)
    if m:
        return (int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
    m = NEW.match(version)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return None


def generation(version):
    """RHEL 大版本。解不出来返回 None。"""
    p = parse(version)
    return p[0] if p else None


def pick(prov_dir, self_version):
    """同代里最新的、且不是自己的那条的 rhcosVersion;没有就 None。"""
    gen = generation(self_version)
    if gen is None:
        return None
    best, best_key = None, None
    for name in os.listdir(prov_dir):
        if not name.endswith(".yaml") or name == "example.yaml":
            continue
        ver = name[:-len(".yaml")]
        if ver == self_version:
            continue
        key = parse(ver)
        # 解不出来的条目直接跳过 —— 上游要是又换了命名,宁可没有基线,
        # 也不能把一个看不懂的版本当成同代拿来比。
        if key is None or key[0] != gen:
            continue
        if best_key is None or key > best_key:
            best, best_key = ver, key
    return best


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        sys.stderr.write(__doc__.splitlines()[2].strip() + "\n")
        return 2
    prov_dir, self_version = argv
    if not os.path.isdir(prov_dir):
        return 0                       # 目录还不存在 = 还没有任何基线
    got = pick(prov_dir, self_version)
    if got:
        print(got)
    return 0


if __name__ == "__main__":
    sys.exit(main())
