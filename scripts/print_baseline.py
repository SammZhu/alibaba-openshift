#!/usr/bin/env python3
"""Print the kargsBaseline (one key per line) from a provenance YAML file.

Stdlib-only (no PyYAML — runner pythons may lack it). Parses the simple
  kargsBaseline:
    - key1
    - key2
block. Used to feed the previous version's baseline into the offline gate's
cross-version diff guard.

  print_baseline.py bootimage/provenance/<rhcos>.yaml
"""
import re
import sys

if len(sys.argv) != 2:
    sys.exit("usage: print_baseline.py <provenance.yaml>")

in_block = False
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if re.match(r"^\s*kargsBaseline\s*:\s*$", line):
        in_block = True
        continue
    if in_block:
        m = re.match(r"^\s*-\s*(\S+)", line)
        if m:
            print(m.group(1))
        elif line.strip() and not line[0].isspace():
            break  # next top-level key ends the list
