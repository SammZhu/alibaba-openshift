#!/usr/bin/env python3
"""upstream_dates.py 的单元测试(不联网)。

挡得住 —— **拿「最近一次 bump」当「设定了这个构建的那次 bump」**。对当前分支头那条
          两者相同,对历史条目就是另一次 bump 的日期,而它一样是个合法日期、一样能
          拿去和 payload 构建时间比大小、一样给出一个看着合理的结论。实测:
          9.6.20260512-0 的 bump 是 05-28,而 release-4.20 最近一次 bump 是 08-24。
          也挡住挑错 stream 文件(4.22 目录里有两个)。
挡不住 —— 上游改掉提交标题的写法。那时匹配不上,返回 None,字段就不写 —— 少一个
          字段,好过写一个别次 bump 的日期。

    python3 scripts/upstream_dates_test.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "upstream_dates", os.path.join(HERE, "upstream_dates.py"))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def commit(date, title):
    return {"commit": {"message": title, "committer": {"date": date + "T00:00:00Z"}}}


# release-4.20 的真实形状:最近一次是 08-24,而我们要找的那条在更早
HISTORY = [
    commit("2026-08-24", "Update data/data/coreos/rhcos.json to 9.6.20260818-0"),
    commit("2026-06-23", "Update data/data/coreos/rhcos.json to 9.6.20260616-0"),
    commit("2026-05-28", "Update data/data/coreos/rhcos.json to 9.6.20260512-0"),
]

u._fetch_json = lambda url, **kw: HISTORY

print("按构建号精确匹配,而不是取最近一次")
check("当前分支头 -> 最近那次",
      u.bumped_at("4.20", "9.6.20260818-0", stream_file="rhcos.json") == "2026-08-24")
check("历史条目 -> 它自己那次(不是 08-24)",
      u.bumped_at("4.20", "9.6.20260512-0", stream_file="rhcos.json") == "2026-05-28",
      u.bumped_at("4.20", "9.6.20260512-0", stream_file="rhcos.json"))
check("中间那条也对",
      u.bumped_at("4.20", "9.6.20260616-0", stream_file="rhcos.json") == "2026-06-23")

print("匹配不上就返回 None,不退而求其次")
check("历史里没有这个构建 -> None",
      u.bumped_at("4.20", "9.6.20991231-9", stream_file="rhcos.json") is None)
u._fetch_json = lambda url, **kw: []
check("提交历史为空 -> None",
      u.bumped_at("4.20", "9.6.20260818-0", stream_file="rhcos.json") is None)

print("挑 stream 文件:按 stream 字段对分支号,不按文件名顺序")
files = {"coreos-rhel-10.json": {"stream": "rhcos-4.22"},
         "coreos-rhel-9.json": {"stream": "rhcos-4.21"}}
u._fetch_json = lambda url, **kw: files.get(url.rsplit("/", 1)[-1])
check("4.22 挑 rhel-10", u.stream_file_for("4.22") == "coreos-rhel-10.json",
      u.stream_file_for("4.22"))
check("4.21 挑 rhel-9(在 STREAM_FILES 里排后面)",
      u.stream_file_for("4.21") == "coreos-rhel-9.json", u.stream_file_for("4.21"))
check("没有一个对得上 -> None(不猜)", u.stream_file_for("4.30") is None)

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
