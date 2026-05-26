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


def main(paths: list[str]) -> int:
    rc = 0
    for p in paths:
        text = Path(p).read_text()
        for line, snippet in find_imbalanced_shell_blocks(text):
            print(f"::error file={p},line={line}:: shell heredoc has odd "
                  f"number of single quotes — ansible arg parser will refuse")
            print(f"  {p}:{line}")
            for s in snippet.splitlines():
                print(f"    {s}")
            print()
            rc = 1
    if rc == 0:
        print(f"OK: scanned {len(paths)} file(s), all shell heredocs "
              f"have balanced single-quote counts.")
    return rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: lint-ansible-quotes.py FILE [FILE...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1:]))
