#!/usr/bin/env python3
"""Offline format gate for the re-stamped RHCOS aliyun boot image (P3-IMG.1).

Given a directory of BLS entries (loader/entries/*.conf) + grub.cfg ALREADY
extracted from a baked qcow2 (the guestfish part lives in bootimage-gate.sh),
assert that the ignition.platform.id re-stamp is complete and correct, and —
with a baseline — flag if upstream RHCOS changed its kernel-argument scheme.

This is the cheap, boot-free check that turns "silent broken image discovered at
cluster boot" into "loud CI failure at bake time". Pure stdlib, fully testable.

Exit 0 = pass; non-zero = a specific, named failure.
"""
import argparse
import glob
import os
import re
import sys

PLATFORM_RE = re.compile(r"ignition\.platform\.id=([a-z0-9]+)")
# A karg token: "key" or "key=value" (value may contain anything but whitespace).
KARG_RE = re.compile(r"(?:^|\s)([a-zA-Z0-9_.\-]+)(?:=\S+)?")


def _kargs_lines(directory):
    """Yield (source_label, kernel-args-line) for every BLS entry + grub.cfg."""
    for conf in sorted(glob.glob(os.path.join(directory, "entries", "*.conf"))):
        for line in open(conf, encoding="utf-8", errors="replace"):
            if line.lstrip().startswith("options "):
                yield os.path.basename(conf), line.strip()
    grub = os.path.join(directory, "grub.cfg")
    if os.path.exists(grub):
        for line in open(grub, encoding="utf-8", errors="replace"):
            s = line.strip()
            # GRUB kernel lines carry the same kargs.
            if " ignition.platform.id=" in s or s.startswith(("linux", "linux16", "linuxefi")):
                if "ignition.platform.id=" in s:
                    yield "grub.cfg", s


def verify(directory, expect, forbid, baseline_keys=None):
    """Return (ok, problems[], info{})."""
    problems = []
    platform_ids = []  # (label, value)
    karg_keys = set()

    saw_any_line = False
    for label, line in _kargs_lines(directory):
        saw_any_line = True
        for m in PLATFORM_RE.finditer(line):
            platform_ids.append((label, m.group(1)))
        for km in KARG_RE.finditer(line):
            k = km.group(1)
            if k not in ("options", "linux", "linux16", "linuxefi"):
                karg_keys.add(k)

    if not saw_any_line:
        problems.append("no BLS 'options' line or grub kernel line found — "
                        "partition layout / extraction wrong, or RHCOS changed format")
        return False, problems, {}

    # 1. Every platform id present must be the expected one.
    bad = [(l, v) for (l, v) in platform_ids if v != expect]
    if bad:
        problems.append(f"found non-{expect} ignition.platform.id (incomplete re-stamp): "
                        + ", ".join(f"{l}={v}" for l, v in bad))

    # 2. There must be at least one platform id at all.
    if not platform_ids:
        problems.append("no ignition.platform.id karg found anywhere — "
                        "RHCOS may have changed how the platform is selected (karg gone)")

    # 3. No forbidden (pre-stamp) value may survive.
    survivors = sorted({v for (_, v) in platform_ids if v in set(forbid)})
    if survivors:
        problems.append(f"residual pre-stamp platform id(s) survived the sed: {survivors}")

    # 4. Cross-version karg diff guard (early warning of upstream change).
    if baseline_keys is not None:
        baseline = set(baseline_keys) - {"ignition.platform.id"}
        current = karg_keys - {"ignition.platform.id"}
        added = sorted(current - baseline)
        removed = sorted(baseline - current)
        if added or removed:
            problems.append(
                "kernel-argument KEY set drifted vs baseline (upstream RHCOS may have "
                f"changed its karg scheme) — added={added} removed={removed}")

    info = {
        "platform_ids": platform_ids,
        "entries_with_platform_id": len({l for (l, _) in platform_ids}),
        "karg_keys": sorted(karg_keys),
    }
    return (not problems), problems, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline format gate for the RHCOS aliyun re-stamp")
    ap.add_argument("directory", help="dir with entries/*.conf and grub.cfg extracted from the qcow2")
    ap.add_argument("--expect", default="aliyun", help="required ignition.platform.id (default: aliyun)")
    ap.add_argument("--forbid", default="metal,openstack,qemu,qemu-secex,gcp,aws,azure",
                    help="comma-separated pre-stamp ids that must NOT survive")
    ap.add_argument("--baseline-keys",
                    help="path to a newline-separated list of expected karg KEYS (diff guard)")
    ap.add_argument("--emit-baseline",
                    help="write THIS image's observed karg KEY set here (for provenance)")
    args = ap.parse_args(argv)

    baseline = None
    if args.baseline_keys and os.path.exists(args.baseline_keys):
        baseline = [l.strip() for l in open(args.baseline_keys) if l.strip()]

    ok, problems, info = verify(
        args.directory, args.expect, [f for f in args.forbid.split(",") if f], baseline)

    # Record the observed karg KEY set so the provenance write-back can pin it as
    # the next version's diff-guard baseline.
    if args.emit_baseline and info.get("karg_keys"):
        with open(args.emit_baseline, "w") as f:
            f.write("\n".join(info["karg_keys"]) + "\n")

    if info:
        print(f"[gate] platform ids: {info['platform_ids']}")
        print(f"[gate] BLS entries carrying a platform id: {info['entries_with_platform_id']}")
    if ok:
        print(f"[gate] PASS — all ignition.platform.id == {args.expect}, no residuals")
        return 0
    print("[gate] FAIL:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
