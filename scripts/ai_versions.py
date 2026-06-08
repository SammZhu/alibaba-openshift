#!/usr/bin/env python3
"""Fetch the OCP versions Assisted Installer supports (P3-IMG.2). Stdlib-only.

The AI side of the version-matrix AND: a CAPA aliyun boot image is only useful
for an OCP version a cluster can actually be — i.e. one AI supports. This
exchanges the Red Hat offline token for an access token (same flow as
ansible/tasks/assisted_token.yml), GETs <assisted_api>/openshift-versions, and
prints the supported OCP version keys (one per line) for detect --ai-versions.

  ai_versions.py [--offline-token-file ~/.openshift/offline-token]
                 [--assisted-api https://api.openshift.com/api/assisted-install/v2]
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SSO = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API = "https://api.openshift.com/api/assisted-install/v2"


def _grep1(path, key):
    pat = re.compile(r'^\s*' + re.escape(key) + r'\s*:\s*["\']?(.+?)["\']?\s*$')
    for line in open(path, encoding="utf-8", errors="replace"):
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


def _resolve(value):
    """Resolve a group_vars path value: {{ lookup('env','HOME') }} / ~ / $VARS."""
    value = re.sub(r"\{\{\s*lookup\(\s*['\"]env['\"]\s*,\s*['\"]HOME['\"]\s*\)\s*\}\}",
                   os.environ.get("HOME", "~"), value)
    value = re.sub(r"\{\{\s*ansible_env\.HOME\s*\}\}", os.environ.get("HOME", "~"), value)
    return os.path.expanduser(os.path.expandvars(value))


def offline_token_path(group_vars):
    """The offline_token_file from group_vars all.yml (the runner's config)."""
    path = group_vars
    if not os.path.exists(path) and os.path.exists(path + ".example"):
        sys.stderr.write(f"[ai] {path} absent, falling back to {path}.example\n")
        path = path + ".example"
    raw = _grep1(path, "offline_token_file") if os.path.exists(path) else None
    if not raw:
        raw = "{{ lookup('env', 'HOME') }}/.openshift/offline-token"
        sys.stderr.write(f"[ai] offline_token_file not in {path}; using default\n")
    return _resolve(raw)


def access_token(offline, sso_url):
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": "cloud-services",
        "refresh_token": offline,
    }).encode()
    req = urllib.request.Request(
        sso_url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-vars", default="ansible/group_vars/all.yml",
                    help="read offline_token_file (+ assisted_api/sso_url) from here")
    ap.add_argument("--offline-token-file",
                    help="explicit override (else taken from group_vars)")
    ap.add_argument("--sso-url", default=SSO)
    ap.add_argument("--assisted-api", default=API)
    args = ap.parse_args(argv)

    # Prefer the runner's all.yml config (assisted_api / sso_url / offline_token_file).
    gv = args.group_vars if os.path.exists(args.group_vars) else args.group_vars + ".example"
    if os.path.exists(gv):
        args.assisted_api = _grep1(gv, "assisted_api") or args.assisted_api
        args.sso_url = _grep1(gv, "sso_url") or args.sso_url

    token_file = args.offline_token_file or offline_token_path(args.group_vars)
    sys.stderr.write(f"[ai] offline token: {token_file} | api: {args.assisted_api}\n")
    offline = open(token_file).read().strip()
    tok = access_token(offline, args.sso_url)
    req = urllib.request.Request(
        f"{args.assisted_api}/openshift-versions",
        headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        versions = json.loads(r.read())

    for v in sorted(versions):
        print(v)
    sys.stderr.write(f"[ai] {len(versions)} AI-supported OCP versions: {sorted(versions)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
