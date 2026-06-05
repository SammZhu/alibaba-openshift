#!/usr/bin/env python3
"""
Detect single-quote-count imbalance inside ansible.builtin.shell: |
heredoc blocks.

We hit this bug FOUR times in a row writing 03b-mirror-prepare.yml:
plain English comments like "that's", "can't", "Quay's pages" each
unbalance the count of single quotes in the heredoc, and ansible
counts them globally without understanding shell <<'EOF' semantics.
The play then refuses to start with:

  ERROR! failed at splitting arguments, either an unbalanced jinja2
  block or quotes

This script catches those at commit time.

Usage:
  scripts/lint-ansible-quotes.py ansible/playbooks/*.yml

Returns non-zero (and prints offending blocks) on failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


SHELL_BLOCK_RE = re.compile(
    # `ansible.builtin.shell: |` or `shell: |` on its own line,
    # followed by indented body.  We capture the indented body.
    r"^([ \t]+)(?:ansible\.builtin\.shell|shell):\s*\|\s*\n"
    r"((?:\1[ \t].*\n|\n)+)",
    re.MULTILINE,
)


def find_imbalanced_shell_blocks(text: str) -> list[tuple[int, str]]:
    """Return a list of (start_line, snippet) for each shell block whose
    single-quote count is odd."""
    bad = []
    for m in SHELL_BLOCK_RE.finditer(text):
        body = m.group(2)
        n = body.count("'")
        if n % 2 == 1:
            start_line = text[: m.start()].count("\n") + 1
            # Trim to first 12 body lines for the diagnostic.
            preview = "\n".join(body.splitlines()[:12])
            bad.append((start_line, f"{n} single quotes (odd)\n{preview}"))
    return bad


def find_jinja_comment_traps(text: str) -> list[tuple[int, str]]:
    """Return (line, snippet) for shell blocks containing a literal `{#`.

    ansible templates the whole module-arg string through Jinja2, which
    treats `{#` as the start of a comment block ({# ... #}).  A bash
    construct like `${#TOKEN}` (string length) or `${#arr[@]}` (array
    length) therefore aborts playbook load with
      ERROR! failed at splitting arguments, either an unbalanced jinja2
      block or quotes
    Bit us 2026-06-05 (#46 probe) and earlier in 05-verify-mirror.
    Use `wc -c` / explicit counters instead.  (A `{#` inside a YAML
    comment line is fine — but it's in the shell body here, so we only
    scan the captured shell-block body and skip pure-comment lines.)"""
    bad = []
    for m in SHELL_BLOCK_RE.finditer(text):
        body = m.group(2)
        for i, ln in enumerate(body.splitlines()):
            stripped = ln.lstrip()
            if stripped.startswith("#"):       # bash/YAML comment line — benign
                continue
            if "{#" in ln:
                start_line = text[: m.start()].count("\n") + 1 + i
                bad.append((start_line,
                            f"literal '{{#' (Jinja comment-block trap):\n{ln.strip()}"))
    return bad


def _expand_args(args: list[str]) -> list[Path]:
    """Each arg is either a file or a directory.  Directories recurse for
    *.yml / *.yaml so callers can pass a single `ansible` arg from the
    GitHub Actions workflow or pre-commit hook — no shell globstar
    required (GHA bash has globstar off by default, which bit us once)."""
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.yml")))
            out.extend(sorted(p.rglob("*.yaml")))
        elif p.is_file():
            out.append(p)
        else:
            print(f"warning: skipping non-existent path: {a}", file=sys.stderr)
    return out


def main(args: list[str]) -> int:
    paths = _expand_args(args)
    if not paths:
        print("error: no files to scan (after expansion)", file=sys.stderr)
        return 2
    rc = 0
    for p in paths:
        text = p.read_text()
        for line, snippet in find_imbalanced_shell_blocks(text):
            print(f"::error file={p},line={line}:: shell heredoc has odd "
                  f"number of single quotes — ansible arg parser will refuse")
            print(f"  {p}:{line}")
            for s in snippet.splitlines():
                print(f"    {s}")
            print()
            rc = 1
        for line, snippet in find_jinja_comment_traps(text):
            print(f"::error file={p},line={line}:: shell block contains '{{#' "
                  f"— ansible's Jinja2 templar treats it as a comment block "
                  f"and refuses to load the playbook")
            print(f"  {p}:{line}")
            for s in snippet.splitlines():
                print(f"    {s}")
            print()
            rc = 1
    if rc == 0:
        print(f"OK: scanned {len(paths)} file(s), all shell heredocs "
              f"have balanced single-quote counts + no '{{#' Jinja traps.")
    return rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: lint-ansible-quotes.py PATH [PATH...]\n"
              "  PATH can be a file or a directory (dirs recurse for .yml/.yaml).",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
