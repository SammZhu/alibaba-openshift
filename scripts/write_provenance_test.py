#!/usr/bin/env python3
"""write_provenance.py 的单元测试(不联网、不碰 git)。

钉住的是**这个文件到底在断言什么**。schemaVersion 1 只有一个 `ocpVersion`,
读起来像「这张镜像是给这个版本的」,而写进去的其实是「烤的时候该 minor 最新的
z」。两者只要 payload 切出来之后又发生过 bootimage bump 就分家 —— 实测
2026-09-15:release-4.18 在 08-24 bump 到 418.94.202608142238-0,4.18.54 却是
08-21 构建的,于是那条 provenance 标着 "4.18.54",而 4.18.54 实际 boot 的是
418.94.202602022246-0。一个会说假话的字段。

现在分成两个名字,谁写的谁负责:

    branch-head (CI)      -> latestZ   信息,不是断言
    payload     (phase10) -> bootedBy  实证,真有集群 boot 过它

    python3 scripts/write_provenance_test.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "write_provenance", os.path.join(HERE, "write_provenance.py"))
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def run(tmp, *extra):
    base = os.path.join(tmp, "baseline")
    open(base, "w").write("console\nostree\nrw\n")
    prov = os.path.join(tmp, "prov")
    rc = w.main(["--rhcos", "10.2.20260715-0", "--ocp", "4.22.12",
                 "--url", "https://example/x.qcow2.gz", "--sha256", "abc",
                 "--baseline", base, "--provenance-dir", prov, *extra])
    return rc, open(os.path.join(prov, "10.2.20260715-0.yaml")).read()


print("CI(分支头):写 latestZ,绝不写 bootedBy")
with tempfile.TemporaryDirectory() as t:
    rc, out = run(t, "--ref-kind", "branch-head", "--ref", "release-4.22")
    check("退出 0", rc == 0, rc)
    check("schemaVersion 2", "schemaVersion: 2" in out)
    check('ocpMinor 是 "4.22"', 'ocpMinor: "4.22"' in out, out[:200])
    check('latestZ 是 "4.22.12"', 'latestZ: "4.22.12"' in out)
    check("不写 bootedBy(没有实证)", "bootedBy" not in out)
    check("不再有 ocpVersion 这个会说假话的名字", "ocpVersion" not in out)
    check("声明 kind: bootimage(不是 machine-os)", "kind: bootimage" in out)
    check("声明 refKind: branch-head", "refKind: branch-head" in out)
    check("记下是哪个分支", 'ref: "release-4.22"' in out)
    check("karg 基线照写", "  - console" in out and "  - rw" in out)
    check("bootSmoke 仍是 pending", "bootSmoke: pending" in out)

print("phase 10(payload):写 bootedBy,绝不写 latestZ")
with tempfile.TemporaryDirectory() as t:
    rc, out = run(t, "--ref-kind", "payload", "--ref", "4.22.12")
    check("退出 0", rc == 0, rc)
    check('bootedBy 是 "4.22.12"', 'bootedBy: "4.22.12"' in out, out[:200])
    check("不写 latestZ(它不是「最新 z」,是实证)", "latestZ" not in out)
    check("声明 refKind: payload", "refKind: payload" in out)

print("默认是 branch-head —— 忘了传也不会把信息冒充成实证")
with tempfile.TemporaryDirectory() as t:
    _, out = run(t)
    check("默认写 latestZ", "latestZ" in out and "bootedBy" not in out)

print("minor 从 z 解析,解不出就报错而不是瞎写")
with tempfile.TemporaryDirectory() as t:
    base = os.path.join(t, "b"); open(base, "w").write("console\n")
    try:
        w.main(["--rhcos", "x", "--ocp", "stable-4.22", "--url", "u",
                "--sha256", "s", "--baseline", base,
                "--provenance-dir", os.path.join(t, "p")])
        check("拒绝写", False, "居然写了")
    except SystemExit as e:
        check("拒绝写", True)
        check("报错点名 --ocp", "--ocp" in str(e), str(e))

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
