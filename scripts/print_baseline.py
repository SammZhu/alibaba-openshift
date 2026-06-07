#!/usr/bin/env python3
"""Print the kargsBaseline (one key per line) from a provenance YAML file.

Used by the bake workflow to feed the previous version's baseline into the
offline gate's cross-version diff guard, without embedding YAML-in-YAML.

  print_baseline.py bootimage/provenance/<rhcos>.yaml
"""
import sys
import yaml

if len(sys.argv) != 2:
    sys.exit("usage: print_baseline.py <provenance.yaml>")
data = yaml.safe_load(open(sys.argv[1])) or {}
print("\n".join(data.get("kargsBaseline", [])))
