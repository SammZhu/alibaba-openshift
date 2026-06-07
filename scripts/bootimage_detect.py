#!/usr/bin/env python3
"""Detect whether a new RHCOS version needs an aliyun boot image (P3-IMG.1).

Reads the cluster's coreos-bootimages stream JSON (the openstack artifact) and
compares the RHCOS release against the provenance files already in git. Prints a
small result the GitHub Actions `detect` job consumes; exits 0 always (the job
decides via the printed `needs_bake`).

Usage:
  bootimage_detect.py --stream stream.json --provenance-dir bootimage/provenance
  oc -n openshift-machine-config-operator get cm coreos-bootimages \
     -o jsonpath='{.data.stream}' | bootimage_detect.py --stream - --provenance-dir ...

The stream JSON is the value of the coreos-bootimages cm `stream` key, i.e.
{"architectures": {"x86_64": {"artifacts": {"openstack": {"release": "...",
 "formats": {"qcow2.gz": {"disk": {"location": "...", "sha256": "..."}}}}}}}}
"""
import argparse
import glob
import json
import os
import sys


def extract(stream):
    ostk = stream["architectures"]["x86_64"]["artifacts"]["openstack"]
    disk = ostk["formats"]["qcow2.gz"]["disk"]
    return {
        "rhcosVersion": ostk["release"],
        "url": disk["location"],
        "sha256": disk.get("sha256", ""),
    }


def known_versions(provenance_dir):
    out = set()
    for p in glob.glob(os.path.join(provenance_dir, "*.yaml")):
        if os.path.basename(p) == "example.yaml":
            continue
        # cheap parse: look for "rhcosVersion:" without a yaml dep
        for line in open(p):
            s = line.strip()
            if s.startswith("rhcosVersion:"):
                out.add(s.split(":", 1)[1].strip().strip('"'))
                break
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", required=True, help="stream JSON file, or - for stdin")
    ap.add_argument("--provenance-dir", required=True)
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if args.stream == "-" else open(args.stream).read()
    info = extract(json.loads(raw))
    have = known_versions(args.provenance_dir)
    needs = info["rhcosVersion"] not in have

    # GitHub Actions output form (caller appends to $GITHUB_OUTPUT).
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
