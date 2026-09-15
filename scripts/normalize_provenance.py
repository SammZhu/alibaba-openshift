#!/usr/bin/env python3
"""Refresh each provenance entry's `latestZ` to the newest z of its minor (P3-IMG.2).

**这个字段证明什么、不证明什么** —— 下面那道守卫只检查「分支头现在仍指向这条的
rhcosVersion」。它证明的是:这张镜像目前仍是 release-X.Y 的 bootimage。它**不**
证明:装 latestZ 那个 z 会拿到这张镜像 —— 因为 bootimage 是按「bump」提交离散往前
走的,而 payload 每个 z 都发,payload 切出来之后再 bump 一次,两者就分家了。
所以字段名从 `ocpVersion`(读起来像「这张镜像是给这个版本的」)改成 `latestZ`,
而菜单校验改用 `ocpMinor`。实证过的对应关系只有一处来源:真装过的集群,由
phase 10 写成 `bootedBy`。

The operator picks a deploy version by eye from bootimage/provenance/, so every
entry should show the *latest* precise version that this exact image can run. The
same RHCOS boot image serves a whole minor's z-streams, so when a newer GA z of
the minor appears (4.21.12 -> 4.21.13), the recorded ocpVersion should follow —
AND an early matrix bake may have recorded only the bare minor (4.21), which is
not deployable as-is. This does both.

SAFETY GUARD — only bump an entry when the installer release-X.Y stream STILL
points at this entry's rhcosVersion. If the latest z shipped a *new* RHCOS, this
image is historical (the matrix bakes a fresh provenance for the new RHCOS); we
must NOT relabel the old image to a z it can't actually boot (that would drift).

Run on the runner (it has the AI offline token); idempotent, safe to schedule:
  python3 scripts/normalize_provenance.py \
      --ai-versions <(python3 scripts/ai_versions.py --group-vars ansible/group_vars/all.yml) \
      --provenance-dir bootimage/provenance
"""
import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bootimage_detect import (  # noqa: E402
    load_ai_versions, _grep1, minor, vkey, fetch_stream_for_minor, extract)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provenance-dir", default="bootimage/provenance")
    ap.add_argument("--ai-versions", required=True,
                    help="AI-supported version list (one per line)")
    ap.add_argument("--include-prereleases", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    _, ai_max_z = load_ai_versions(args.ai_versions, args.include_prereleases)
    changed = 0
    for path in sorted(glob.glob(os.path.join(args.provenance_dir, "*.yaml"))):
        if os.path.basename(path) == "example.yaml":
            continue
        ocp = _grep1(path, "latestZ")
        rhcos = _grep1(path, "rhcosVersion")
        if not ocp or not rhcos:
            continue
        m = minor(ocp)
        z = ai_max_z.get(m)
        if not z or vkey(z) <= vkey(ocp):
            continue                       # nothing newer (or AI list lags — no downgrade)
        # GUARD: is this image still the live one for minor m?
        stream = fetch_stream_for_minor(m)
        live = extract(stream)["rhcosVersion"] if stream else None
        if live != rhcos:
            sys.stderr.write(
                f"[normalize] {os.path.basename(path)}: minor {m} live RHCOS {live} "
                f"!= {rhcos}; image is historical, leaving latestZ={ocp}\n")
            continue
        text = open(path).read()
        new = re.sub(r'(?m)^(latestZ:\s*)"?%s"?\s*$' % re.escape(ocp),
                     r'\g<1>"%s"' % z, text)
        if new == text:
            continue
        print(f"[normalize] {os.path.basename(path)}: latestZ {ocp} -> {z}")
        if not args.dry_run:
            open(path, "w").write(new)
        changed += 1
    print(f"[normalize] {changed} file(s) {'would change' if args.dry_run else 'updated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
