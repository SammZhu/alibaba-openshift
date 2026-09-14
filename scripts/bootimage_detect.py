#!/usr/bin/env python3
"""Detect whether a new RHCOS version needs an aliyun boot image (P3-IMG.1).

Cluster-independent and stdlib-only (no PyYAML — runner pythons may lack it).
The OCP version comes from `openshift_version` in ansible/group_vars/all.yml
(the operator's source of truth); the RHCOS openstack qcow2 is resolved from the
openshift/installer release-X.Y stream metadata — no running cluster / oc.

Resolution order (first that applies):
  --stream <file|->        explicit stream JSON (manual override)
  --openshift-version X.Y  explicit version -> installer rhcos.json
  --group-vars <file>      read openshift_version from group_vars (operator-local)
  --version-file <file>    committed authoritative version (default: bootimage/version)
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

INSTALLER_COREOS_DIR = ("https://raw.githubusercontent.com/openshift/installer/"
                        "release-{branch}/data/data/coreos/")

# 上游改过这个文件的名字。4.21 及以前是单文件 rhcos.json;4.22 起按 RHEL 基座拆成
# coreos-rhel-9.json / coreos-rhel-10.json,而 4.22 的两个文件里,rhel-9 那个的
# stream 仍是 rhcos-4.21(升级路径),rhel-10 那个才是 rhcos-4.22。
# 所以不能按文件名顺序挑,要按 stream 字段和分支号对得上来挑 —— 这条规则不写死
# RHEL 代次,4.23 之后照样成立。
STREAM_FILES = ["rhcos.json", "coreos-rhel-10.json", "coreos-rhel-9.json"]

# 同目录里跨版本都存在的文件,用来区分「分支不存在」和「文件名又变了」。
# 这个区别是这段代码的全部要点,见 fetch_stream_for_minor。
BRANCH_MARKER = "OWNERS"


def extract(stream):
    ostk = stream["architectures"]["x86_64"]["artifacts"]["openstack"]
    disk = ostk["formats"]["qcow2.gz"]["disk"]
    return {"rhcosVersion": ostk["release"], "url": disk["location"],
            "sha256": disk.get("sha256", "")}


def minor(version):
    m = re.match(r"^(\d+)\.(\d+)", version.strip().strip('"'))
    if not m:
        raise ValueError(f"cannot parse OCP minor from {version!r}")
    return f"{m.group(1)}.{m.group(2)}"


def is_ga(version):
    """False for -ec/-rc/-fc pre-releases (e.g. 4.22.0-ec.0[-multi]). -multi is
    a multi-arch GA variant, not a pre-release."""
    return not re.search(r"-(ec|rc|fc)\.", version)


def vkey(version):
    """Numeric sort key so 4.21.10 > 4.21.5 (string sort gets this wrong). The
    -multi/-ec/... suffix is dropped before comparing."""
    base = version.split("-", 1)[0]
    return tuple(int(n) for n in re.findall(r"\d+", base))


def load_ai_versions(path, include_prereleases):
    """Parse an AI/ supported-version file -> (set of minors, {minor: highest GA
    z-stream without the -multi suffix}). The z map turns a bare minor (4.21) into
    a precise, deployable version (4.21.x) for provenance."""
    minors, max_z = set(), {}
    for line in open(path, encoding="utf-8", errors="replace"):
        v = line.strip()
        if not v or v.startswith("#"):
            continue
        if not (include_prereleases or is_ga(v)):
            continue
        m = minor(v)
        z = v.split("-", 1)[0]            # 4.21.5-multi -> 4.21.5
        minors.add(m)
        if m not in max_z or vkey(z) > vkey(max_z[m]):
            max_z[m] = z
    return minors, max_z


def _confirm_404(url, times, delay):
    """Re-request `url` and report whether it 404s every time.

    A 404 is the signal that STOPS the scan, so unlike every other response it
    is acted on rather than retried — which makes it the one answer a single
    hiccup can turn into a silent truncation.  Anything that is not another 404
    (a 200, a 5xx, a dropped connection) means "not confirmed": the caller
    should keep retrying instead of believing the branch is absent.
    """
    for _ in range(times):
        time.sleep(delay)
        try:
            with urllib.request.urlopen(url, timeout=30):
                return False
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return False
    return True


class StreamFileMissing(Exception):
    """分支在,但我们认识的 stream 文件名一个都不在 —— 上游又改名了。

    这必须是个错误,不能当成「扫描到头了」。2026-09 就是这么丢掉 4.22 的:
    上游把 rhcos.json 拆成 coreos-rhel-{9,10}.json,detect 拿到一个货真价实的
    404,把它读成「这个 minor 还没出分支」,于是 break —— 4.22 **以及往上所有
    版本**从此不可见,而 job 报 `nothing to bake`,退出码 0,绿的。
    """


def _fetch_json(url, retries=4, backoff=2.0, confirm_404=1, confirm_delay=3.0):
    """Fetch one JSON URL; None on a confirmed 404.

    Transient network failures (connection reset / TLS handshake drop / timeout /
    429 / 5xx) are retried with exponential backoff — a flaky runner network
    must not fail the whole detection. Other 4xx still raise immediately.

    A 404 is the one response where a single bad reply is indistinguishable
    from the real answer, and getting it wrong is invisible: the scan
    truncates and the job still reports "nothing to bake".  So a 404 is
    confirmed by re-requesting before it is believed.  The cost is one extra
    request per run (every scan ends on a 404 by construction).

    What a confirmed 404 *means* is decided by the caller, not here — that
    distinction is the whole point of fetch_stream_for_minor below.
    """
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if _confirm_404(url, confirm_404, confirm_delay):
                    return None
                sys.stderr.write(
                    f"[detect] {url} 404 not reproducible — treating it as a "
                    f"transient error, not as an absent branch\n")
                last = e
            elif e.code not in (429, 500, 502, 503, 504):
                raise
            else:
                last = e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last = e
        if attempt < retries - 1:
            delay = backoff * (2 ** attempt)
            sys.stderr.write(
                f"[detect] {url} transient error ({last}); retry in {delay:.0f}s\n")
            time.sleep(delay)
    raise RuntimeError(f"failed to fetch {url} after {retries} attempts: {last}")


def _url_exists(url, confirm_404=1, confirm_delay=3.0):
    """这个 URL 在不在(不解析内容)。仅用于分支存在性判断。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return not _confirm_404(url, confirm_404, confirm_delay)
        return True          # 别的 HTTP 错不代表不存在
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return True          # 网络问题不该被读成「分支没了」


def fetch_stream_for_minor(branch, **kw):
    """release-X.Y 的 RHCOS stream;分支确实不存在时返回 None。

    候选文件名有好几个(上游改过名),存在的可能不止一个:4.22 同时有
    coreos-rhel-9.json(stream rhcos-4.21,升级路径)和 coreos-rhel-10.json
    (stream rhcos-4.22)。**按 stream 字段和分支号对得上来挑**,不按文件名顺序
    ——挑错的后果是给 4.22 烤了 4.21 的镜像,而两边都「成功」。

    一个都没取到时要分清两种情况:
      分支本身不在   → None,扫描正常结束
      分支在但文件名不认识 → StreamFileMissing,必须响,不能静默停
    """
    base = INSTALLER_COREOS_DIR.format(branch=branch)
    found = []
    want = f"rhcos-{branch}"
    for name in STREAM_FILES:
        doc = _fetch_json(base + name, **kw)
        if doc is None:
            continue
        found.append((name, doc))
        # 已经拿到对得上的,不必再试剩下的候选(每个不存在的候选都要复验 404,
        # 一次多两个请求)。
        if doc.get("stream") == want:
            break

    if not found:
        if _url_exists(base + BRANCH_MARKER):
            raise StreamFileMissing(
                f"release-{branch}: {base} 在(还有 {BRANCH_MARKER}),但 "
                f"{STREAM_FILES} 一个都不在。上游多半又改了文件名 —— "
                f"把新名字加进 STREAM_FILES。**不要**把这种情况当成扫描到头,"
                f"那会让这个版本以及往上所有版本静默消失。")
        return None

    for name, doc in found:
        if doc.get("stream") == want:
            sys.stderr.write(f"[detect] release-{branch}: 用 {name} (stream {want})\n")
            return doc

    # 分支在、文件也在,但没有一个文件声明 rhcos-<branch> —— 这个 minor 还没有
    # 自己的 RHCOS 流,是个尚未 GA 的开发分支。实测 release-4.23 就是这样:
    # 它带着 coreos-rhel-{9,10}.json,两个的 stream 都还是 rhcos-4.22,而
    # rhel-10 那个的 RHCOS(10.2.20260423-0)**比 4.22 自己的还旧**。
    # 所以绝不能「退而用第一个」:那会给 4.23 烤一个 4.22 时代的镜像,而且两边
    # 都报成功。没有自己的流 = 还没准备好,和分支不存在同等对待。
    sys.stderr.write(
        f"[detect] release-{branch}: 存在 {[n for n, _ in found]},但没有一个的 "
        f"stream 是 {want}(看到的是 {[d.get('stream') for _, d in found]})—— "
        f"这个 minor 还没有自己的 RHCOS 流,当作尚未发布,扫描到此为止\n")
    return None


def fetch_stream_for_version(version):
    b = minor(version)
    sys.stderr.write(f"[detect] resolving RHCOS for OCP {version} from release-{b}\n")
    s = fetch_stream_for_minor(b)
    if s is None:
        raise ValueError(f"no installer rhcos.json for release-{b}")
    return s


def enumerate_minors(floor_minor, cap=40):
    """Yield (branch, stream) for release-<floor>.. 直到第一个「还没到」的 minor。

    停下来的**理由**由 fetch_stream_for_minor 打印,这里不复述。它现在有两种
    停法(分支不存在 / 分支在但还没有自己的 RHCOS 流),而这里分不出是哪一种 ——
    以前在这里硬写「分支不存在(404 已复验)」,于是 4.23 那种情况下日志会紧跟着
    一句与事实相反的断言。这份日志存在的全部意义,就是让人分清「还没发布」和
    「我们坏了」;在里面写一句听起来很确定、其实没根据的话,是最坏的一种噪音。
    """
    major, mn = floor_minor.split(".")
    m = int(mn)
    while m <= int(mn) + cap:
        branch = f"{major}.{m}"
        stream = fetch_stream_for_minor(branch)     # 文件名不认识时会抛
        if stream is None:
            sys.stderr.write(f"[detect] 扫描到 release-{branch} 为止(理由见上一行)\n")
            break
        yield branch, stream
        m += 1


def _grep1(path, key):
    """First `key: value` scalar from a simple YAML file (no PyYAML)."""
    pat = re.compile(r'^\s*' + re.escape(key) + r'\s*:\s*["\']?([^"\'#\s]+)')
    for line in open(path, encoding="utf-8", errors="replace"):
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


def version_from_group_vars(path):
    if not os.path.exists(path):
        alt = path + ".example"
        sys.stderr.write(f"[detect] {path} absent, falling back to {alt}\n")
        path = alt
    v = _grep1(path, "openshift_version")
    if not v:
        raise ValueError(f"openshift_version not found in {path}")
    return v


def version_from_file(path):
    """First non-comment, non-blank line of the committed version file (the FLOOR)."""
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    raise ValueError(f"no version line in {path}")


def known_versions(provenance_dir):
    out = set()
    for p in glob.glob(os.path.join(provenance_dir, "*.yaml")):
        if os.path.basename(p) == "example.yaml":
            continue
        v = _grep1(p, "rhcosVersion")
        if v:
            out.add(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", help="explicit stream JSON file, or - for stdin")
    ap.add_argument("--openshift-version", help="explicit OCP version, e.g. 4.20.22")
    ap.add_argument("--group-vars", help="operator-local group_vars override (reads openshift_version)")
    ap.add_argument("--version-file", default="bootimage/version",
                    help="committed FLOOR version file (the minimum supported OCP)")
    ap.add_argument("--all-from", action="store_true",
                    help="MATRIX: enumerate every OCP minor from the floor (version "
                         "file / --openshift-version) up to the latest; emit TSV lines "
                         "(rhcos<TAB>ocp_minor<TAB>url<TAB>sha256) for the ones not yet "
                         "in provenance")
    ap.add_argument("--ai-versions",
                    help="INTERSECT (#84): file of OCP versions a cluster can actually "
                         "be (from ai_versions.py / a connected assisted-service). When "
                         "given, only bake minors also in this set — so every image "
                         "matches a version AI can install.")
    ap.add_argument("--include-prereleases", action="store_true",
                    help="keep -ec/-rc/-fc pre-release versions (default: GA only)")
    ap.add_argument("--provenance-dir", required=True)
    args = ap.parse_args(argv)

    # ── MATRIX mode: floor minor .. latest, skip already-baked, emit a list ──────
    # bootimage/version is the FLOOR (minimum supported OCP). The actual set to bake
    # = enumerate floor..latest, minus what provenance already has. Optionally AND
    # with the AI-supported set (#84) so every image matches a version a cluster can
    # actually be. GA-only unless --include-prereleases.
    if args.all_from:
        floor = args.openshift_version or version_from_file(args.version_file)
        fminor = minor(floor)
        # The AI-supported set (#84) doubles as the z-stream resolver: ocpVersion
        # recorded in provenance is the highest GA z of the minor (precise,
        # deployable), not a bare minor. Without --ai-versions we can only record
        # the minor.
        ai_minors, ai_max_z = None, {}
        if args.ai_versions and os.path.exists(args.ai_versions):
            ai_minors, ai_max_z = load_ai_versions(
                args.ai_versions, args.include_prereleases)
        have = known_versions(args.provenance_dir)
        seen = set(have)   # also dedups minors that pin the same RHCOS build
        to_bake, scanned, skipped_not_ai = [], [], []
        for branch, stream in enumerate_minors(fminor):
            scanned.append(branch)
            if ai_minors is not None and branch not in ai_minors:
                skipped_not_ai.append(branch)
                continue
            info = extract(stream)
            if info["rhcosVersion"] not in seen:
                ocp = ai_max_z.get(branch, branch)   # precise z, else bare minor
                to_bake.append((info, ocp))
                seen.add(info["rhcosVersion"])
        for info, ocp in to_bake:
            print(f"{info['rhcosVersion']}\t{ocp}\t{info['url']}\t{info['sha256']}")
        sys.stderr.write(
            f"[detect] floor={fminor} scanned={scanned} "
            f"ai_filtered_out={skipped_not_ai} already_baked={sorted(have)} "
            f"to_bake={[(i['rhcosVersion'], o) for i, o in to_bake]}\n")
        return 0

    # ── SINGLE mode (default): one version, GITHUB_OUTPUT key=value ──────────────
    if args.stream:
        raw = sys.stdin.read() if args.stream == "-" else open(args.stream).read()
        stream = json.loads(raw)
    else:
        if args.openshift_version:
            version = args.openshift_version
        elif args.group_vars:
            version = version_from_group_vars(args.group_vars)
        else:
            version = version_from_file(args.version_file)
        stream = fetch_stream_for_version(version)

    info = extract(stream)
    have = known_versions(args.provenance_dir)
    needs = info["rhcosVersion"] not in have

    print(f"needs_bake={'true' if needs else 'false'}")
    print(f"rhcos_version={info['rhcosVersion']}")
    print(f"source_url={info['url']}")
    print(f"source_sha256={info['sha256']}")
    sys.stderr.write(
        f"[detect] rhcos={info['rhcosVersion']} known={sorted(have)} -> "
        f"{'NEW, bake needed' if needs else 'already baked'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
