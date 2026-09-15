#!/usr/bin/env python3
"""pick_baseline.py 的单元测试(不联网、不碰仓库里的 provenance)。

挡得住 —— 跨代挑基线(烤 10.x 拿 9.x 比,4.22 那次就是这么过的)、把老方案的
          `418.94.…` 的代次解成 418、两种命名方案混在同一代里排错序、把 12 位
          时间戳和 8 位日期直接当整数比、挑到自己、挑到 example。
挡不住 —— 上游**再换一次命名方案**。那种情况下 parse 返回 None,条目被跳过,
          结果是「没有基线」:少一道比对,但绝不会拿一个看不懂的版本硬比。

    python3 scripts/pick_baseline_test.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "pick_baseline", os.path.join(HERE, "pick_baseline.py"))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def d(*versions):
    """造一个 provenance 目录,返回路径(调用方负责在 with 里用)。"""
    td = tempfile.mkdtemp()
    for v in versions:
        open(os.path.join(td, v + ".yaml"), "w").write(f'rhcosVersion: "{v}"\n')
    open(os.path.join(td, "example.yaml"), "w").write('rhcosVersion: "x"\n')
    return td


print("两种命名方案都要解对代次")
check("老方案 418.94.* 是 RHEL 9(不是 418)",
      p.generation("418.94.202608142238-0") == 9,
      p.generation("418.94.202608142238-0"))
check("老方案 410.84.* 是 RHEL 8", p.generation("410.84.202201010000-0") == 8)
check("新方案 9.6.* 是 RHEL 9", p.generation("9.6.20260818-0") == 9)
check("新方案 10.2.* 是 RHEL 10", p.generation("10.2.20260715-0") == 10)
check("认不出来的返回 None", p.generation("rhcos-latest") is None)

print("时间戳 12 位 vs 日期 8 位,必须按日期比")
# 直接当整数比的话 202608142238 > 20260818,4.18 会被当成比 8/18 那条还新
check("老方案取前 8 位当日期",
      p.parse("418.94.202608142238-0")[2] == 20260814,
      p.parse("418.94.202608142238-0"))
check("9.4 排在 9.6 之前",
      p.parse("418.94.202608142238-0") < p.parse("9.6.20260818-0"))

print("挑基线:同代、不含自己、不含 example")
td = d("418.94.202608142238-0", "9.6.20260815-0", "9.6.20260818-0", "10.2.20260715-0")
check("4.18(9.4)→ 同代最新的 9.6.20260818-0",
      p.pick(td, "418.94.202608142238-0") == "9.6.20260818-0",
      p.pick(td, "418.94.202608142238-0"))
check("9.6.20260818 → 同代次新的(不是自己)",
      p.pick(td, "9.6.20260818-0") == "9.6.20260815-0",
      p.pick(td, "9.6.20260818-0"))

print("这就是被修掉的那个 bug:烤 10.x 不能拿 9.x 当基线")
check("10.2 这一代只有它自己 → 无基线",
      p.pick(td, "10.2.20260715-0") is None, p.pick(td, "10.2.20260715-0"))
# 字典序会怎么挑:'10.2…' < '418…' < '9.6…',tail -1 永远是 9.6
lex = sorted(v for v in os.listdir(td) if v != "example.yaml")[-1]
check("字典序挑的是 9.6(跨代)—— 这正是原来的行为",
      lex.startswith("9.6."), lex)

print("同代里两种方案混着也要排对")
td2 = d("417.94.202607240132-0", "418.94.202608142238-0")
check("老方案内部按日期排",
      p.pick(td2, "417.94.202607240132-0") == "418.94.202608142238-0",
      p.pick(td2, "417.94.202607240132-0"))

print("上游再换命名:跳过,宁可没基线也不硬比")
td3 = d("9.6.20260818-0", "rhcos-something-new")
check("认不出的条目被跳过",
      p.pick(td3, "418.94.202608142238-0") == "9.6.20260818-0",
      p.pick(td3, "418.94.202608142238-0"))
check("自己认不出来 → 无基线",
      p.pick(td3, "rhcos-something-new") is None)

print("CLI:没有基线时什么都不打印,退出 0")
import io, contextlib                                            # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = p.main([td, "10.2.20260715-0"])
check("退出 0", rc == 0, rc)
check("stdout 为空", buf.getvalue() == "", repr(buf.getvalue()))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    p.main([td, "418.94.202608142238-0"])
check("有基线时只打印那一行",
      buf.getvalue().strip() == "9.6.20260818-0", repr(buf.getvalue()))
check("目录不存在也退出 0", p.main(["/nonexistent/dir", "9.6.20260818-0"]) == 0)

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
