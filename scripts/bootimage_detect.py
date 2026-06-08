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
import urllib.error
import urllib.request

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


def is_ga(version):
    """False for -ec/-rc/-fc pre-releases (e.g. 4.22.0-ec.0[-multi]). -multi is
    a multi-arch GA variant, not a pre-release."""
    return not re.search(r"-(ec|rc|fc)\.", version)


def fetch_stream_for_minor(branch):
    """Fetch the installer rhcos.json for a release-X.Y branch; None on 404."""
    url = INSTALLER_RHCOS.format(branch=branch)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_stream_for_version(version):
    b = minor(version)
    sys.stderr.write(f"[detect] resolving RHCOS for OCP {version} from release-{b}\n")
    s = fetch_stream_for_minor(b)
    if s is None:
        raise ValueError(f"no installer rhcos.json for release-{b}")
    return s


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


def versions_from_file(path):
    """All non-comment, non-blank version lines (the supported/mirrored set)."""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    if not out:
        raise ValueError(f"no version line in {path}")
    return out


def version_from_file(path):
    """First version line (single-version mode)."""
    return versions_from_file(path)[0]


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
                    help="committed authoritative version file (default source)")
    ap.add_argument("--all-from", action="store_true",
                    help="MATRIX: bake every OCP version listed in the version file "
                         "(the supported/mirrored set) that is not yet in provenance; "
                         "emit TSV lines (rhcos<TAB>ocp<TAB>url<TAB>sha256)")
    ap.add_argument("--ai-versions",
                    help="optional cross-check: file of OCP versions a cluster can "
                         "actually be (from ai_versions.py / a connected "
                         "assisted-service). When given, only bake versions whose "
                         "minor is also in this set — catches a version listed in the "
                         "version file that AI can't actually install.")
    ap.add_argument("--include-prereleases", action="store_true",
                    help="keep -ec/-rc/-fc pre-release versions (default: GA only)")
    ap.add_argument("--provenance-dir", required=True)
    args = ap.parse_args(argv)

    # ── MATRIX mode: bake exactly the versions listed in the version file ────────
    # The version file IS the supported/mirrored set (one OCP version per line) —
    # the air-gap-authoritative source of what a cluster can actually be here. We
    # resolve each version's RHCOS, drop pre-releases (unless --include-prereleases),
    # skip anything already in provenance, and emit the rest.
    if args.all_from:
        versions = versions_from_file(args.version_file)
        # optional cross-check against a connected AI list (catch typos / versions
        # the env mirrored but AI can't actually install).
        ai_minors = None
        if args.ai_versions and os.path.exists(args.ai_versions):
            ai_minors = {minor(s.strip()) for s in open(args.ai_versions)
                         if s.strip() and not s.startswith("#")
                         and (args.include_prereleases or is_ga(s.strip()))}
        have = known_versions(args.provenance_dir)
        seen = set(have)   # also dedups versions that pin the same RHCOS build
        to_bake, skipped_pre, skipped_ai, errs = [], [], [], []
        for v in versions:
            if not (args.include_prereleases or is_ga(v)):
                skipped_pre.append(v)
                continue
            if ai_minors is not None and minor(v) not in ai_minors:
                skipped_ai.append(v)
                continue
            try:
                info = extract(fetch_stream_for_version(v))
            except Exception as e:   # noqa: BLE001 — one bad version mustn't sink the run
                errs.append(f"{v}: {e}")
                continue
            if info["rhcosVersion"] not in seen:
                to_bake.append((info, minor(v)))
                seen.add(info["rhcosVersion"])
        for info, mnr in to_bake:
            print(f"{info['rhcosVersion']}\t{mnr}\t{info['url']}\t{info['sha256']}")
        sys.stderr.write(
            f"[detect] versions={versions} prerelease_skipped={skipped_pre} "
            f"ai_filtered_out={skipped_ai} errors={errs} already_baked={sorted(have)} "
            f"to_bake={[i['rhcosVersion'] for i, _ in to_bake]}\n")
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
