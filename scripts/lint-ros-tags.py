#!/usr/bin/env python3
"""Reject YAML short tags that Aliyun ROS cannot handle.

ROS is not CloudFormation.  Two tags look right, pass every local check, and
fail only when the cloud parses the template:

  !Condition  — not supported at all.  ROS rejects the WHOLE template with
                "could not determine a constructor for the tag '!Condition'",
                so a stack using it cannot be created.  Found 2026-09-03 via
                PreviewStack, after the construct had been merged and used on
                Apsara for weeks — that path rewrites the template through
                apsara_ize.py first, which consumes the short tags, so only the
                public cloud ever saw the raw YAML.
  !Not        — parses, but mis-evaluates (always false) even when the inner
                Fn::Equals is plainly false.  Verified 2026-06-20 via
                PreviewStack; first hit 2026-05-30.  The templates use positive
                boolean parameters instead, computed in the ansible layer.

Write conditions out with Fn::Equals / Fn::And instead.

LEGACY templates are reported but do not fail the run: they are the deprecated
single-stack path, kept for reference and not deployed.
"""
import pathlib
import sys

import yaml

BANNED = {
    "!Condition": "ROS cannot parse it — the whole template is rejected",
    "!Not": "ROS mis-evaluates Fn::Not (always false) — use a positive boolean",
}


def scan(path):
    """Yield (line, tag) for banned tags actually applied to a node.

    Uses the YAML event stream rather than a text search: prose inside a
    Description block legitimately names these tags while explaining why they
    are avoided, and a grep cannot tell that from a real use.  The parser can —
    a tag is only a tag when it is attached to a node.
    """
    try:
        events = yaml.parse(path.read_text())
    except yaml.YAMLError as e:
        print(f"ERROR {path}: cannot parse: {e}")
        return
    for ev in events:
        tag = getattr(ev, "tag", None)
        if tag in BANNED:
            yield ev.start_mark.line + 1, tag


def main(argv):
    roots = [pathlib.Path(a) for a in argv[1:]] or [pathlib.Path("ros-templates")]
    files = []
    for r in roots:
        files.extend(sorted(r.rglob("*.yaml")) if r.is_dir() else [r])

    failures = warnings = 0
    for f in files:
        legacy = "LEGACY" in f.name
        for line, tag in scan(f):
            where = f"{f}:{line}"
            if legacy:
                warnings += 1
                print(f"WARN  {where}: {tag} ({BANNED[tag]}) [legacy, not deployed]")
            else:
                failures += 1
                print(f"ERROR {where}: {tag} — {BANNED[tag]}")

    print(f"\nscanned {len(files)} template(s): {failures} error(s), {warnings} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
