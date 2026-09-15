#!/usr/bin/env python3
"""rh_oldest_supported_minor.py 的单元测试(不联网)。

挡得住 —— **EUS 陷阱**(最老的非 EOL 版本是 4.12,正确答案是 4.18;两个答案长得
          一样合理)、type 取值改版、非 X.Y 的版本名、一次跳太远、写文件时把
          注释吃掉。
挡不住 —— 红帽换掉整个接口。那种情况靠 exit 2/3 的区分兜着:查不到就沿用已提交的
          floor 照常烤,而不是拿一个残缺列表算出个数字来。

    python3 scripts/rh_oldest_supported_minor_test.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "rh_oldest_supported_minor", os.path.join(HERE, "rh_oldest_supported_minor.py"))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def doc(pairs, product=f.PRODUCT):
    return {"data": [{"name": product,
                      "versions": [{"name": n, "type": t} for n, t in pairs]}]}


# 2026-09-15 实际数据的形状:EOL 和 Extended Support 在版本序列里是交错的。
REAL = [("4.22", "Full Support"), ("4.21", "Full Support"),
        ("4.20", "Maintenance Support"), ("4.19", "Maintenance Support"),
        ("4.18", "Maintenance Support"), ("4.17", "End of life"),
        ("4.16", "Extended Support"), ("4.15", "End of life"),
        ("4.14", "Extended Support"), ("4.13", "End of life"),
        ("4.12", "Extended Support"), ("4.11", "End of life"),
        ("4.6 EUS", "End of life"), ("3", "End of life")]

print("EUS 陷阱:最老的非 EOL 是 4.12,正常支持的最老是 4.18")
floor, supported, skipped = f.oldest_normally_supported(doc(REAL))
check("floor 是 4.18 而不是 4.12", floor == "4.18", floor)
check("Extended Support 不算正常支持",
      "4.16" not in supported and "4.12" not in supported, supported)
check("正常支持的集合正好是 4.18..4.22",
      supported == ["4.18", "4.19", "4.20", "4.21", "4.22"], supported)
check("非 X.Y 的名字被跳过并报出来", skipped == ["4.6 EUS", "3"], skipped)

print("type 的取值变了:一个 Full Support 都没有")
try:
    f.oldest_normally_supported(doc([("4.22", "Supported"), ("4.21", "Supported")]))
    check("拒绝下结论", False, "居然给了答案")
except f.FloorUndecidable as e:
    check("拒绝下结论", True)
    check("错误信息带上实际见到的取值", "supported" in str(e), str(e))

print("大小写/空格不敏感(措辞微调不该让 job 变哑)")
floor, _, _ = f.oldest_normally_supported(
    doc([("4.22", "FULL  SUPPORT"), ("4.20", "Maintenance support")]))
check("仍能算出 4.20", floor == "4.20", floor)

print("产品改名")
try:
    f.oldest_normally_supported(doc(REAL, product="OpenShift"))
    check("拒绝下结论", False, "居然给了答案")
except f.FloorUndecidable:
    check("拒绝下结论", True)

print("退出码:三种情形分得开")
check("网络失败 → 2", f.main(["--url", "http://127.0.0.1:1/nope"]) == 2)

with tempfile.TemporaryDirectory() as d:
    real = os.path.join(d, "real.json")
    json.dump(doc(REAL), open(real, "w"))
    bad = os.path.join(d, "bad.json")
    json.dump(doc([("4.22", "Supported")]), open(bad, "w"))
    check("数据不可信 → 3", f.main(["--from-file", bad]) == 3)
    check("正常 → 0", f.main(["--from-file", real, "--current", "4.20"]) == 0)

    print("跳太远就不采信")
    # 上游给出 4.12(EUS 过滤没生效之类),和当前 4.20 差 8 格
    far = os.path.join(d, "far.json")
    json.dump(doc([("4.22", "Full Support"), ("4.12", "Maintenance Support")]),
              open(far, "w"))
    check("差 8 格 → 3", f.main(["--from-file", far, "--current", "4.20"]) == 3)
    check("放宽 --max-move 就允许",
          f.main(["--from-file", far, "--current", "4.20", "--max-move", "9"]) == 0)

    print("--write 只动版本行,注释一个字不改")
    vf = os.path.join(d, "version")
    body = ("# FLOOR: 说明文字\n#\n# 第二段说明\n4.20\n")
    open(vf, "w").write(body)
    rc = f.main(["--from-file", real, "--write", vf])
    got = open(vf).read()
    check("退出 0", rc == 0, rc)
    check("版本行变成 4.18", got == body.replace("4.20\n", "4.18\n"), repr(got))
    check("注释行数没变", got.count("#") == body.count("#"), repr(got))

    print("值没变就不落盘(否则每天一个空 commit)")
    before = os.stat(vf).st_mtime_ns
    rc = f.main(["--from-file", real, "--write", vf])
    check("退出 0", rc == 0, rc)
    check("文件没被重写", os.stat(vf).st_mtime_ns == before)

    print("不可信时,已提交的 floor 原样留着")
    open(vf, "w").write(body)
    f.main(["--from-file", bad, "--write", vf])
    check("文件没动", open(vf).read() == body, repr(open(vf).read()))

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
