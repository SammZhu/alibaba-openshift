#!/usr/bin/env python3
"""Detect whether a new RHCOS version needs an aliyun boot image (P3-IMG.1).

Cluster-independent: the OCP version comes from `openshift_version` in
ansible/group_vars/all.yml (the operator's source of truth), and the RHCOS
openstack qcow2 is resolved from the openshift/installer `release-X.Y` stream
metadata — no running cluster / `oc` needed. Compares the resolved RHCOS release
to the provenance files in git and prints a small result the workflow consumes.

Resolution order:
  --stream <file|->        explicit stream JSON (manual override)
  --openshift-version X.Y  explicit version -> installer rhcos.json
  --group-vars <file>      read openshift_version from group_vars (default;
                           falls back to <file>.example when the file is absent,
                           e.g. on a fresh runner checkout)

Exits 0 always; the workflow branches on the printed needs_bake.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

import yaml

INSTALLER_RHCOS = ("https://raw.githubusercontent.com/openshift/installer/"
                   "release-{branch}/data/data/coreos/rhcos.json")


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


def fetch_stream_for_version(version):
    url = INSTALLER_RHCOS.format(branch=minor(version))
    sys.stderr.write(f"[detect] resolving RHCOS for OCP {version} from {url}\n")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def version_from_group_vars(path):
    if not os.path.exists(path):
        alt = path + ".example"
        sys.stderr.write(f"[detect] {path} absent, falling back to {alt}\n")
        path = alt
    data = yaml.safe_load(open(path)) or {}
    v = data.get("openshift_version")
    if not v:
        raise ValueError(f"openshift_version not found in {path}")
    return str(v)


def known_versions(provenance_dir):
    out = set()
    for p in glob.glob(os.path.join(provenance_dir, "*.yaml")):
        if os.path.basename(p) == "example.yaml":
            continue
        d = yaml.safe_load(open(p)) or {}
        if d.get("rhcosVersion"):
            out.add(str(d["rhcosVersion"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", help="explicit stream JSON file, or - for stdin")
    ap.add_argument("--openshift-version", help="explicit OCP version, e.g. 4.20.22")
    ap.add_argument("--group-vars", default="ansible/group_vars/all.yml",
                    help="read openshift_version from here (default; .example fallback)")
    ap.add_argument("--provenance-dir", required=True)
    args = ap.parse_args(argv)

    if args.stream:
        raw = sys.stdin.read() if args.stream == "-" else open(args.stream).read()
        stream = json.loads(raw)
    else:
        version = args.openshift_version or version_from_group_vars(args.group_vars)
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
