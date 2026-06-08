#!/usr/bin/env python3
"""Normalize provenance ocpVersion to a precise, deployable z-stream (P3-IMG.2).

Early matrix bakes recorded a bare minor (ocpVersion: "4.21") because
detect --all-from emitted the release branch. The operator picks a version to
deploy by eye from bootimage/provenance/, so every entry should be a precise
version they can copy straight into all.yml's openshift_version.

This rewrites any provenance file whose ocpVersion is a bare minor (X.Y) to the
highest GA z-stream of that minor from the AI-supported set (same source/rule as
detect --ai-versions). Already-precise entries (X.Y.Z) are left untouched.

Run on the runner (it has the AI offline token):
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
from bootimage_detect import load_ai_versions, _grep1  # noqa: E402

BARE_MINOR = re.compile(r"^\d+\.\d+$")


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
        ocp = _grep1(path, "ocpVersion")
        if not ocp or not BARE_MINOR.match(ocp):
            continue                       # already precise (X.Y.Z) or unreadable
        z = ai_max_z.get(ocp)
        if not z:
            sys.stderr.write(f"[normalize] {path}: no AI z for minor {ocp}, skip\n")
            continue
        text = open(path).read()
        new = re.sub(r'(?m)^(ocpVersion:\s*)"?%s"?\s*$' % re.escape(ocp),
                     r'\g<1>"%s"' % z, text)
        if new == text:
            continue
        print(f"[normalize] {os.path.basename(path)}: ocpVersion {ocp} -> {z}")
        if not args.dry_run:
            open(path, "w").write(new)
        changed += 1
    print(f"[normalize] {changed} file(s) {'would change' if args.dry_run else 'updated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
