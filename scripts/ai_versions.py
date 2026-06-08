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
import sys
import urllib.parse
import urllib.request

SSO = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API = "https://api.openshift.com/api/assisted-install/v2"


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
    ap.add_argument("--offline-token-file",
                    default=os.path.expanduser("~/.openshift/offline-token"))
    ap.add_argument("--sso-url", default=SSO)
    ap.add_argument("--assisted-api", default=API)
    args = ap.parse_args(argv)

    offline = open(args.offline_token_file).read().strip()
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
