#!/usr/bin/env python3
"""rh_lifecycle.py 的单元测试(不联网)。

挡得住 —— **EUS 陷阱**(最老的非 EOL 版本是 4.12,正确答案是 4.18;两个答案长得
          一样合理)、type 取值改版、非 X.Y 的版本名、一次跳太远、写文件时把
          注释吃掉,以及**退役那半边**:EOL 的删、EUS 的留(而 EUS 的比 EOL 的
          还老)、还没 GA 的别当成「不认识」删掉、以及三道批量栏杆。
          退役这半边今天在真实数据上一条都不触发(六个配方全在支持期内),
          所以它的正确性只有这里能证明。
挡不住 —— 红帽换掉整个接口。那种情况靠 exit 2/3 的区分兜着:查不到就沿用已提交的
          floor 照常烤,而不是拿一个残缺列表算出个数字来。

    python3 scripts/rh_lifecycle_test.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "rh_lifecycle", os.path.join(HERE, "rh_lifecycle.py"))
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

print("退役:逐版本判,EOL 的删、EUS 的留 —— 而 EUS 的那个比 EOL 的还老")
with tempfile.TemporaryDirectory() as d:
    def prov(name, ocp, extra=""):
        open(os.path.join(d, name), "w").write(
            f'schemaVersion: 2\nocpMinor: "{ocp}"\nrhcosVersion: "x"\n{extra}')

    prov("a.yaml", "4.22.1")     # Full        -> keep
    prov("b.yaml", "4.18.9")     # Maintenance -> keep
    prov("c.yaml", "4.17.30")    # EOL         -> retire
    prov("e.yaml", "4.16.40")    # Extended(比 4.17 还老)-> keep
    tmap = f.type_map(doc(REAL))
    rows = {b: (act, why) for b, _, act, why in
            f.classify_provenance(d, tmap, "4.18")}
    check("4.17 EOL → retire", rows["c.yaml"][0] == "retire", rows["c.yaml"])
    check("4.16 EUS → keep(比 4.17 老,却要留着)",
          rows["e.yaml"][0] == "keep", rows["e.yaml"])
    check("4.22 / 4.18 在支持期 → keep",
          rows["a.yaml"][0] == "keep" and rows["b.yaml"][0] == "keep", rows)

    print("还没 GA 的配方不能当成「不认识」删掉 —— 同一个「不在表里」,两个方向相反")
    prov("f.yaml", "4.23.0")     # 比表里最新的还新
    prov("g.yaml", "4.2.5")      # 比表里最老的还老(REAL 最老是 4.11)
    rows = {b: (act, why) for b, _, act, why in
            f.classify_provenance(d, tmap, "4.18")}
    check("4.23 未 GA → keep", rows["f.yaml"][0] == "keep", rows["f.yaml"])
    check("4.2 老到不再列出 → retire", rows["g.yaml"][0] == "retire", rows["g.yaml"])

    print("dry-run 不删文件")
    before = sorted(os.listdir(d))
    doomed = f.retire_provenance(d, tmap, "4.18", delete=False)
    check("报出 c/g 两个", sorted(doomed) == ["c.yaml", "g.yaml"], doomed)
    check("文件一个没少", sorted(os.listdir(d)) == before, os.listdir(d))

    print("--delete 才真删,而且只删该删的")
    f.retire_provenance(d, tmap, "4.18", delete=True)
    check("c/g 没了", not os.path.exists(os.path.join(d, "c.yaml"))
          and not os.path.exists(os.path.join(d, "g.yaml")))
    check("a/b/e/f 还在",
          sorted(os.listdir(d)) == ["a.yaml", "b.yaml", "e.yaml", "f.yaml"],
          os.listdir(d))

    print("retain: 钉住的配方不删(还有集群在跑那个版本)")
    prov("h.yaml", "4.17.30", extra='retain: "ste2 上还跑着"\n')
    rows = {b: (act, why) for b, _, act, why in
            f.classify_provenance(d, tmap, "4.18")}
    check("EOL 但被钉住 → keep", rows["h.yaml"][0] == "keep", rows["h.yaml"])
    check("理由写明是钉住的", "retain" in rows["h.yaml"][1], rows["h.yaml"])
    os.remove(os.path.join(d, "h.yaml"))

    print("example.yaml 不参与(它是模板,不是菜单项)")
    prov("example.yaml", "4.2.0")
    rows = [b for b, _, _, _ in f.classify_provenance(d, tmap, "4.18")]
    check("没被列进来", "example.yaml" not in rows, rows)
    os.remove(os.path.join(d, "example.yaml"))

print("schemaVersion 1 的旧条目仍能分类(回落到 ocpVersion)")
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "old.yaml"), "w").write(
        'schemaVersion: 1\nocpVersion: "4.17.30"\n')
    open(os.path.join(d, "new.yaml"), "w").write(
        'schemaVersion: 2\nocpMinor: "4.22"\n')
    rows = {b: (a, w) for b, _, a, w in
            f.classify_provenance(d, f.type_map(doc(REAL)), "4.18")}
    check("旧字段照样判成 EOL", rows["old.yaml"][0] == "retire", rows["old.yaml"])
    check("新字段正常", rows["new.yaml"][0] == "keep", rows["new.yaml"])

print("批量栏杆:三种都得一个文件都不动")
with tempfile.TemporaryDirectory() as d:
    def prov2(name, ocp):
        open(os.path.join(d, name), "w").write(f'ocpMinor: "{ocp}"\n')
    tmap = f.type_map(doc(REAL))

    prov2("x.yaml", "4.17.1"); prov2("y.yaml", "4.15.1")
    try:                                     # 全删 = 菜单会空
        f.retire_provenance(d, tmap, "4.18", delete=True)
        check("删光菜单 → 拒绝", False, "居然删了")
    except f.FloorUndecidable as e:
        check("删光菜单 → 拒绝", True)
        check("文件都还在", sorted(os.listdir(d)) == ["x.yaml", "y.yaml"], os.listdir(d))

    for n in ("a", "b"):                     # 加两个活的,让「过半」这条先响
        prov2(f"{n}.yaml", "4.22.1")
    prov2("z.yaml", "4.13.1")
    try:
        f.retire_provenance(d, tmap, "4.18", delete=True)
        check("一次删过半 → 拒绝", False, "居然删了")
    except f.FloorUndecidable as e:
        check("一次删过半 → 拒绝", True)
        check("文件都还在", len(os.listdir(d)) == 5, os.listdir(d))

with tempfile.TemporaryDirectory() as d:
    # 生命周期数据自相矛盾:下沿那个版本自己是 EOL
    open(os.path.join(d, "k.yaml"), "w").write('ocpMinor: "4.20"\n')
    open(os.path.join(d, "l.yaml"), "w").write('ocpMinor: "4.22"\n')
    bad_tmap = f.type_map(doc([("4.22", "Full Support"), ("4.20", "End of life")]))
    try:
        f.retire_provenance(d, bad_tmap, "4.20", delete=True)
        check("要删的不比下沿老 → 拒绝", False, "居然删了")
    except f.FloorUndecidable:
        check("要删的不比下沿老 → 拒绝", True)
        check("文件都还在", len(os.listdir(d)) == 2, os.listdir(d))

print("退役失败时退出码是 3,而且 floor 那半边已经写好了")
with tempfile.TemporaryDirectory() as d:
    real = os.path.join(d, "real.json"); json.dump(doc(REAL), open(real, "w"))
    pd = os.path.join(d, "prov"); os.makedirs(pd)
    open(os.path.join(pd, "x.yaml"), "w").write('ocpMinor: "4.17"\n')
    check("→ 3", f.main(["--from-file", real, "--current", "4.18",
                         "--retire", pd, "--delete"]) == 3)
    check("配方没动", os.listdir(pd) == ["x.yaml"], os.listdir(pd))

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
