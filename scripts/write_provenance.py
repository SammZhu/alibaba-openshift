#!/usr/bin/env python3
"""Write (and optionally commit) the provenance for a freshly baked RHCOS aliyun
boot image (P3-IMG.1). Stdlib-only.

Assembles bootimage/provenance/<rhcosVersion>.yaml — THE recipe + the observed
kargs baseline — so the supply chain has a durable, auditable record and detect
can skip already-baked versions. Run AFTER the offline gate passes.

  write_provenance.py --rhcos 9.6.x --ocp 4.20.22 --ref release-4.20 \
      --ref-kind branch-head --url <U> --sha256 <S> \
      --baseline /path/kargs.baseline --provenance-dir bootimage/provenance \
      [--guestfish 1.44.0] [--qemu-img 6.2.0] [--commit]

--ref-kind says **which of the two RHCOS numbers this file is about**, and that
distinction is the whole reason schemaVersion is 2:

  branch-head  resolved from openshift/installer `release-X.Y` at HEAD — what CI
               bakes.  `--ocp` is then only "the newest z of that minor at bake
               time" and is recorded as `latestZ`.  It is NOT a claim that
               installing that z gets you this image.
  payload      resolved from a running cluster's `coreos-bootimages` — the
               bootimage its own payload ships.  `--ocp` is then a **proven**
               correspondence and is recorded as `bootedBy`.

Why the distinction matters: bootimages move by discrete "bump" commits (roughly
monthly) while release payloads ship every z, so `release-X.Y` HEAD and the
bootimage inside a given z can name different builds.  Measured 2026-09-15:
release-4.18 was bumped to 418.94.202608142238-0 on 08-24, but 4.18.54 was built
08-21 — so a 4.18.54 cluster boots 418.94.202602022246-0, NOT the file CI wrote
while labelling it "4.18.54".  Under schemaVersion 1 that label was `ocpVersion`,
which read like "the version this image is for" and could simply be false.

Neither of these is `machine-os`.  machine-os is the OS the MCO pivots a node to
right after first boot and it moves with every z; nothing in this supply chain
reads it.  4.21 makes the difference impossible to miss: it boots a RHEL 9.6
bootimage and then runs RHEL 10.2.

--commit: git add + commit + push (uses the checkout's credentials; pushes to
$GITHUB_REF_NAME or main). Without it, only the file is written (for testing).
"""
import argparse
import datetime
import os
import socket
import re
import subprocess
import sys

SED = r"'s/ignition\.platform\.id=[a-z0-9]*/ignition.platform.id=aliyun/g'"



def baked_by():
    """谁烤的 —— 问环境,不要写死。

    这个字段以前是常量 "github-actions/rhcos-aliyun-bootimage"。2026-09-20 ste2
    上 site-apsara.yml 的 phase 10 手工烤出一份条目,文件却照样声称自己是流水线
    烤的。provenance 存在的全部意义是来源可信,而它对**自己的**来源说了假话。

    没有任何代码读这个字段,所以它错了也不会有人被绊倒 —— 这正是它能一直错下去
    的原因。
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        wf = os.environ.get("GITHUB_WORKFLOW") or "rhcos-aliyun-bootimage"
        return f"github-actions/{wf}"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    return f"manual/{user}@{socket.gethostname()}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rhcos", required=True)
    ap.add_argument("--ocp", required=True,
                    help="branch-head: newest z of the minor at bake time; "
                         "payload: the cluster version that provably booted this")
    ap.add_argument("--ref-kind", choices=("branch-head", "payload"),
                    default="branch-head",
                    help="where this RHCOS was resolved from — see the module docstring")
    ap.add_argument("--bumped-at", default="",
                    help="branch-head 专用:分支头变成这个构建的那次提交的日期")
    ap.add_argument("--latest-z-built-at", default="",
                    help="branch-head 专用:--ocp 那个 z 的 payload 构建日期")
    ap.add_argument("--ref", default="",
                    help="branch-head: the installer branch (release-4.22); "
                         "payload: the cluster's openshift_version")
    ap.add_argument("--url", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--baseline", required=True, help="file with observed karg keys")
    ap.add_argument("--provenance-dir", required=True)
    ap.add_argument("--guestfish", default="unknown")
    ap.add_argument("--qemu-img", default="unknown")
    # libguestfs 的后端一直是 direct;真正会变的是 settings。用 KVM 烤和用 TCG
    # (纯软件模拟)烤是两条差别很大的路径 —— 2026-09-20 ste2 上 KVM appliance
    # 90 秒起不来,整轮落到 force_tcg。以前两者在 provenance 里长得一模一样。
    ap.add_argument("--libguestfs-settings", default="",
                    help="LIBGUESTFS_BACKEND_SETTINGS actually used (e.g. force_tcg)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)

    baseline = [l.strip() for l in open(args.baseline) if l.strip()] \
        if os.path.exists(args.baseline) else []
    out = os.path.join(args.provenance_dir, f"{args.rhcos}.yaml")
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    m = re.match(r"^(\d+)\.(\d+)", args.ocp.strip().strip('"'))
    if not m:
        sys.exit(f"--ocp {args.ocp!r}: cannot parse an OCP minor from it")
    ocp_minor = f"{m.group(1)}.{m.group(2)}"
    head = args.ref_kind == "branch-head"

    lines = [
        "# Auto-generated by the bootimage supply chain (P3-IMG.1). THE recipe.",
        "schemaVersion: 2",
        f'ocpMinor: "{ocp_minor}"',
        f'rhcosVersion: "{args.rhcos}"',
        # 这一条是本文件唯一和 z 有关的断言,名字必须说清它是哪一种:
        #   latestZ  = 烤的时候该 minor 最新的 z。**不保证**装那个 z 会拿到这张镜像。
        #   bootedBy = 真有一个跑着这个版本的集群 boot 了它。实证。
        (f'latestZ: "{args.ocp}"' if head else f'bootedBy: "{args.ocp}"'),
        # 光有 latestZ 判断不出它到底 boot 不 boot 这张镜像 —— 那取决于两个
        # 日期,而它们原来一个都不在文件里(`bakedAt` 顶不上:那是**我们**第一次
        # 烤到它的时间,取决于我们的下沿。实测 4.18:上游 08-24 bump,我们 09-15
        # 才烤,差三周)。查不到就不写,绝不写一个猜的日期进来。
        *([f'latestZBuiltAt: "{args.latest_z_built_at}"']
          if head and args.latest_z_built_at else []),
        "source:",
        "  kind: bootimage          # 不是 machine-os —— 见本文件生成脚本的模块注释",
        f"  refKind: {args.ref_kind}",
        *([f'  bumpedAt: "{args.bumped_at}"'] if head and args.bumped_at else []),
        *([f'  ref: "{args.ref}"'] if args.ref else []),
        "  format: openstack-qcow2.gz",
        f'  url: "{args.url}"',
        f'  sha256: "{args.sha256}"',
        "transform:",
        "  target: ignition.platform.id=aliyun",
        f"  sed: {SED}",
        '  files: ["/loader/entries/*.conf", "/grub2/grub.cfg"]',
        "tooling:",
        f'  guestfish: "{args.guestfish}"',
        f'  qemu-img: "{args.qemu_img}"',
        "  libguestfsBackend: direct",
        *([f'  libguestfsBackendSettings: "{args.libguestfs_settings}"']
          if args.libguestfs_settings else []),
        "kargsBaseline:",
        *[f"  - {k}" for k in baseline],
        "result:",
        "  gate: passed",
        "  bootSmoke: pending",
        f'  bakedAt: "{now}"',
        f'  bakedBy: "{baked_by()}"',
    ]
    os.makedirs(args.provenance_dir, exist_ok=True)
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[provenance] wrote {out} ({len(baseline)} karg keys)")

    if not args.commit:
        return 0

    branch = os.environ.get("GITHUB_REF_NAME", "main")
    run = lambda *c, **k: subprocess.run(list(c), **k)
    run("git", "config", "user.name", "github-actions[bot]", check=True)
    run("git", "config", "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com", check=True)
    run("git", "add", out, check=True)
    if run("git", "commit", "-m",
           f"chore(bootimage): provenance for RHCOS {args.rhcos} (OCP {args.ocp})").returncode != 0:
        print("[provenance] nothing to commit (already recorded)")
        return 0
    run("git", "pull", "--rebase", "--autostash", "origin", branch)  # best-effort
    run("git", "push", "origin", f"HEAD:{branch}", check=True)
    print(f"[provenance] pushed to {branch}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
