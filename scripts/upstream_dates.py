#!/usr/bin/env python3
"""两个日期,用来判断一条 branch-head provenance 的 `latestZ` 到底 boot 不 boot 它。

  python3 scripts/upstream_dates.py --branch 4.18 --z 4.18.54 \
      --rhcos 418.94.202608142238-0
  bumpedAt=2026-08-24 latestZBuiltAt=2026-08-21

**为什么需要它**:bootimage 只在离散的 "bump" 提交那一刻改变,而 payload 每个 z
都构建。一个 z 会不会 boot 某张 bootimage,取决于它的 payload 构建时间落在最后一次
bump 之前还是之后 —— 而这两个日期,provenance 文件里一个都没有。

`bakedAt` 顶替不了 `bumpedAt`:它是**我们**第一次烤到这个构建的时间,取决于我们的
下沿,和上游无关。实测 2026-09-15:release-4.18 在 08-24 bump,而我们今天才烤到它
(因为今天下沿才降到 4.18),两个日期差三周。

两个数据源都是 CI 已经在访问的端点,各一次请求:
  bumpedAt        GitHub API,取最近一次改动该 stream 文件的提交日期
  latestZBuiltAt  mirror 的 release.txt 里的 `Created:`

**查不到就不打印那一项** —— 宁可文件里少一个字段,也不能写一个猜的日期进去:
读的人会拿它去比大小。
"""
import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bootimage_detect import _fetch_json, STREAM_FILES, INSTALLER_COREOS_DIR  # noqa: E402

COMMITS = "https://api.github.com/repos/openshift/installer/commits"
RELEASE_TXT = ("https://mirror.openshift.com/pub/openshift-v4/x86_64/clients/ocp/"
               "{z}/release.txt")
UA = {"User-Agent": "alibaba-openshift-bootimage/1.0", "Accept": "application/json"}


def stream_file_for(branch):
    """这个分支实际在用哪个 stream 文件 —— 上游在 4.22 把它拆了名字。

    判据和 fetch_stream_for_minor 一致:按 `stream` 字段和分支号对得上来挑,
    不按文件名顺序。挑错文件 = 查到另一条流的 bump 日期。
    """
    base = INSTALLER_COREOS_DIR.format(branch=branch)
    want = f"rhcos-{branch}"
    fallback = None
    for name in STREAM_FILES:
        doc = _fetch_json(base + name)
        if doc is None:
            continue
        if doc.get("stream") == want:
            return name
        fallback = fallback or name
    return None            # 一个都对不上就别猜:返回 None,上层不打印 bumpedAt


def bumped_at(branch, rhcos, stream_file=None):
    """分支头**变成 rhcos 这个构建**的那次提交的日期(YYYY-MM-DD)。

    不能图省事取「最近一次 bump」:对当前分支头那条没错,但对历史条目就是另一次
    bump 的日期 —— 拿它去和 latestZ 的构建时间比大小,会得出一个看着很合理的
    错结论。所以按提交标题里的构建号精确匹配,匹配不上就返回 None。
    上游的标题是字面的:`Update data/data/coreos/rhcos.json to 9.6.20260818-0`。
    """
    name = stream_file or stream_file_for(branch)
    if not name:
        return None
    q = urllib.parse.urlencode({
        "sha": f"release-{branch}",
        "path": f"data/data/coreos/{name}",
        "per_page": "100"})
    try:
        data = _fetch_json(f"{COMMITS}?{q}", headers=UA)
    except Exception as e:                                       # noqa: BLE001
        sys.stderr.write(f"[dates] 取 bump 日期失败:{e}\n")
        return None
    for c in data or []:
        if rhcos in c["commit"]["message"].splitlines()[0]:
            return c["commit"]["committer"]["date"][:10]
    sys.stderr.write(
        f"[dates] release-{branch} 的提交历史里没有一条标题点名 {rhcos} —— "
        f"不写 bumpedAt(宁可缺字段,也不写一个别次 bump 的日期)\n")
    return None


def payload_created(z):
    """那个 z 的 release payload 的构建日期(YYYY-MM-DD)。"""
    import urllib.request
    try:
        req = urllib.request.Request(RELEASE_TXT.format(z=z), headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode(errors="replace")
    except Exception as e:                                       # noqa: BLE001
        sys.stderr.write(f"[dates] 取 {z} 的 release.txt 失败:{e}\n")
        return None
    m = re.search(r"^Created:\s*(\S+)", txt, re.M)
    return m.group(1)[:10] if m else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", required=True, help="minor,例如 4.18")
    ap.add_argument("--z", required=True, help="latestZ,例如 4.18.54")
    ap.add_argument("--rhcos", required=True, help="这条记的构建,例如 418.94.202608142238-0")
    args = ap.parse_args(argv)
    b = bumped_at(args.branch, args.rhcos)
    c = payload_created(args.z)
    out = []
    if b:
        out.append(f"bumpedAt={b}")
    if c:
        out.append(f"latestZBuiltAt={c}")
    print(" ".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
