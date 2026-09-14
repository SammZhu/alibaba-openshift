#!/usr/bin/env python3
"""bootimage_detect.py 里挑 stream 文件那段的单元测试(不联网)。

背景:上游在 4.22 把 `data/data/coreos/rhcos.json` 拆成了
`coreos-rhel-9.json` / `coreos-rhel-10.json`。detect 仍去取老名字,拿到一个货真
价实的 404,把它读成「这个 minor 还没出分支」,于是停止扫描 —— **4.22 以及往上
所有版本从此不可见**,而 job 报 `nothing to bake`,退出码 0,绿的。

说清楚这些测试能挡住什么、挡不住什么:

  挡得住 —— 挑错文件(4.22 的 rhel-9 那个 stream 还是 rhcos-4.21)、
            给没有自己 RHCOS 流的开发分支硬凑一个镜像。
  挡不住 —— 上游**再改一次名字**。那种情况只能靠运行时那条 StreamFileMissing:
            目录在、OWNERS 在、我们认识的文件名一个都不在 → 报错,而不是
            静默当成扫描到头。挡住那一类的是那个异常,不是这个文件。

    python3 scripts/bootimage_detect_test.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
spec = importlib.util.spec_from_file_location(
    "bootimage_detect", os.path.join(HERE, "bootimage_detect.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)


def stream_doc(name, release="9.0.20260101-0"):
    """一份最小但结构完整的 stream 文档。"""
    return {
        "stream": name,
        "architectures": {"x86_64": {"artifacts": {"openstack": {
            "release": release,
            "formats": {"qcow2.gz": {"disk": {
                "location": f"https://example/{release}-openstack.x86_64.qcow2.gz",
                "sha256": "deadbeef"}}}}}}},
    }


def run(branch, files, marker_exists=True):
    """用一组假文件跑 fetch_stream_for_minor。files: {文件名: stream 文档}"""
    b._fetch_json = lambda url, **kw: files.get(url.rsplit("/", 1)[-1])
    b._url_exists = lambda url, **kw: marker_exists
    return b.fetch_stream_for_minor(branch)


print("老布局(4.21 及以前):单文件 rhcos.json")
doc = run("4.21", {"rhcos.json": stream_doc("rhcos-4.21")})
check("取到了", doc is not None and doc["stream"] == "rhcos-4.21", doc)

print("新布局(4.22):按 stream 挑,不按文件名顺序")
files422 = {
    "coreos-rhel-9.json": stream_doc("rhcos-4.21", "9.8.20260715-1"),
    "coreos-rhel-10.json": stream_doc("rhcos-4.22", "10.2.20260715-0"),
}
doc = run("4.22", files422)
check("挑中 stream 是 rhcos-4.22 的那个",
      doc is not None and doc["stream"] == "rhcos-4.22", doc)
check("解出来的是 rhel-10 那份的 RHCOS",
      b.extract(doc)["rhcosVersion"] == "10.2.20260715-0", doc and b.extract(doc))

print("挑选依据是 stream 而不是文件名顺序")
# 造一个反过来的:匹配的那份在 rhel-9 里。STREAM_FILES 把 rhel-10 排在 rhel-9
# 前面,所以按顺序挑会挑错。
flipped = {
    "coreos-rhel-10.json": stream_doc("rhcos-4.30", "10.9.x"),
    "coreos-rhel-9.json": stream_doc("rhcos-4.29", "9.9.x"),
}
doc = run("4.29", flipped)
check("匹配项在后面也能挑中",
      doc is not None and doc["stream"] == "rhcos-4.29", doc)

print("开发分支(实测 4.23):文件在,但没有一个声明自己的流")
# release-4.23 真实情况:两个文件都在,stream 分别是 rhcos-4.22 / rhcos-4.21,
# 而 rhel-10 那份的 RHCOS 比 4.22 自己的还旧。退而求其次会给 4.23 烤一个 4.22
# 时代的镜像,而且两边都报成功。
doc = run("4.23", {
    "coreos-rhel-10.json": stream_doc("rhcos-4.22", "10.2.20260423-0"),
    "coreos-rhel-9.json": stream_doc("rhcos-4.21", "9.8.20260101-0"),
})
check("当成尚未发布,返回 None 而不是退而求其次", doc is None, doc)

print("上游又改名:目录在、认识的文件名一个都不在")
try:
    run("4.40", {}, marker_exists=True)
    check("抛 StreamFileMissing", False, "什么都没抛")
except b.StreamFileMissing as e:
    check("抛 StreamFileMissing", True)
    check("错误信息点名 STREAM_FILES", "STREAM_FILES" in str(e), str(e)[:80])

print("分支确实不存在:文件和 OWNERS 都没有")
doc = run("4.99", {}, marker_exists=False)
check("返回 None,扫描正常结束", doc is None, doc)

print("enumerate_minors 不编造停止理由")
import io                     # noqa: E402
import contextlib             # noqa: E402

_saved = b.fetch_stream_for_minor
b.fetch_stream_for_minor = lambda br, **kw: (
    stream_doc(f"rhcos-{br}") if br in ("4.20", "4.21") else None)
buf = io.StringIO()
try:
    with contextlib.redirect_stderr(buf):
        got = [br for br, _ in b.enumerate_minors("4.20")]
finally:
    b.fetch_stream_for_minor = _saved
check("扫到第一个没有流的 minor 就停", got == ["4.20", "4.21"], got)
# 它分不出是「分支不存在」还是「分支在但没有自己的流」,所以一个字都不能断言。
check("停止那行不声称分支不存在", "分支不存在" not in buf.getvalue(), buf.getvalue())
check("停止那行指向上面的真实理由", "理由见上一行" in buf.getvalue(), buf.getvalue())

print("STREAM_FILES 本身")
check("老名字仍在候选里(4.21 及以前要用)", "rhcos.json" in b.STREAM_FILES)
check("4.22 的两个新名字都在",
      {"coreos-rhel-9.json", "coreos-rhel-10.json"} <= set(b.STREAM_FILES))

print()
if failures:
    print(f"FAILED: {len(failures)} 项 —— {failures}")
    sys.exit(1)
print("all ok")
