#!/usr/bin/env python3
"""Every parameter ansible sends must be defined by every cluster template.

tasks/cluster_stack_params.yml builds ONE parameter list and sends it to
whichever cluster template the topology selects.  ROS rejects a parameter the
template does not declare -- the whole call, not just that parameter:

    ErrorCode: UnknownUserParameter
    Message: The Parameter (EnableSplitWorkers) was not defined in template.

So adding a parameter to cluster-stack.yaml and forgetting cluster-stack-sno.yaml
breaks every SNO deploy on BOTH platforms, and it breaks them at CreateStack --
after the mirror stack, after the ISO, minutes into a paid run.  Nothing else
checks this: the templates are valid on their own, the ansible is valid on its
own, and only the pairing is wrong.  Found the hard way on 2026-09-10.

Usage:  scripts/lint-ros-params.py [ansible/tasks/cluster_stack_params.yml] [ros-templates/]
Exit 0 = every sent parameter is defined everywhere it could be sent.
"""
import re
import sys
from pathlib import Path

# Templates the params list can be sent to (06 picks one by cluster_topology).
CLUSTER_TEMPLATES = ["cluster-stack.yaml", "cluster-stack-sno.yaml"]


def sent_parameters(path):
    return set(re.findall(r"ParameterKey:\s*([A-Za-z0-9_]+)", path.read_text()))


def declared_parameters(path):
    """Top-level keys under Parameters:, without a YAML parse.

    The templates use ROS short tags (!Ref, !Sub, !If) that SafeLoader refuses
    and a custom loader would have to mirror; the block is regular enough that
    reading indentation is both simpler and harder to get subtly wrong.
    """
    out, in_params = set(), False
    for line in path.read_text().splitlines():
        if re.match(r"^Parameters:\s*$", line):
            in_params = True
            continue
        if in_params:
            if re.match(r"^[A-Za-z]", line):        # next top-level section
                break
            m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
            if m:
                out.add(m.group(1))
    return out


def main():
    params_file = Path(sys.argv[1] if len(sys.argv) > 1
                       else "ansible/tasks/cluster_stack_params.yml")
    tpl_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "ros-templates")

    if not params_file.exists():
        sys.exit(f"missing {params_file}")

    sent = sent_parameters(params_file)
    print(f"{params_file}: sends {len(sent)} parameter(s)")

    failures = 0
    for name in CLUSTER_TEMPLATES:
        tpl = tpl_dir / name
        if not tpl.exists():
            print(f"  SKIP {name} (not present)")
            continue
        declared = declared_parameters(tpl)
        missing = sorted(sent - declared)
        if missing:
            failures += 1
            print(f"  FAIL {name}: does not declare {', '.join(missing)}")
        else:
            print(f"  ok   {name}: declares all {len(sent)}")

    if failures:
        print("\nROS rejects the entire call when a sent parameter is undeclared.")
        print("Add the parameter to the template above (inert is fine -- give it")
        print("a Default and say so in the Description), or stop sending it.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
