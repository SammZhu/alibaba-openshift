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


def enumerate_minors(floor_minor, cap=40):
    """Yield (branch, stream) for release-<floor>.. up to the first 404."""
    major, mn = floor_minor.split(".")
    m = int(mn)
    while m <= int(mn) + cap:
        branch = f"{major}.{m}"
        stream = fetch_stream_for_minor(branch)
        if stream is None:
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
    """First non-comment, non-blank line of the committed version file."""
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
                    help="committed authoritative version file (default source)")
    ap.add_argument("--all-from", action="store_true",
                    help="MATRIX: enumerate every OCP minor from the floor (version "
                         "source) up to the latest; emit TSV lines for the ones not "
                         "yet in provenance (rhcos<TAB>ocp_minor<TAB>url<TAB>sha256)")
    ap.add_argument("--ai-versions",
                    help="INTERSECT: file of supported OCP versions (from "
                         "ai_versions.py, a local assisted-service, or a committed "
                         "list). Default: bootimage/supported-versions if present. "
                         "Only bake minors in this set — so every image matches a "
                         "version a cluster can actually be.")
    ap.add_argument("--include-prereleases", action="store_true",
                    help="keep -ec/-rc/-fc pre-release versions (default: GA only)")
    ap.add_argument("--provenance-dir", required=True)
    args = ap.parse_args(argv)

    # ── MATRIX mode: floor minor .. latest, skip already-baked, emit a list ──────
    if args.all_from:
        floor = args.openshift_version or version_from_file(args.version_file)
        fminor = minor(floor)
        # The AND with the supported-version matrix (P3-IMG.2): only versions a
        # cluster can actually be installed at are worth a CAPA boot image. Source
        # priority: --ai-versions, else committed bootimage/supported-versions
        # (the env's mirrored/installable set — the air-gap-authoritative source).
        src = args.ai_versions
        if not src and os.path.exists("bootimage/supported-versions"):
            src = "bootimage/supported-versions"
        ai_minors = None
        if src and os.path.exists(src):
            ai_minors = {minor(s.strip()) for s in open(src)
                         if s.strip() and not s.startswith("#")
                         and (args.include_prereleases or is_ga(s.strip()))}
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
                to_bake.append((info, branch))
                seen.add(info["rhcosVersion"])
        for info, branch in to_bake:
            print(f"{info['rhcosVersion']}\t{branch}\t{info['url']}\t{info['sha256']}")
        sys.stderr.write(
            f"[detect] floor={fminor} scanned={scanned} "
            f"ai_filtered_out={skipped_not_ai} already_baked={sorted(have)} "
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
