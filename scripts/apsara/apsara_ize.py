#!/usr/bin/env python3
"""apsara_ize.py — turn a public-cloud Aliyun ROS template into one Apsara
Stack (private cloud) ROS will accept.

Public Aliyun Cloud and Apsara Stack share the ROS template language but differ
in a handful of concrete ways we hit bringing mirror-stack up on the ste3
private cloud (2026-08).  This tool applies those adaptations mechanically so
the SOURCE templates under ros-templates/ stay public-cloud-first and Apsara
support is a pure overlay — no fork, no public-cloud regression.

Transforms
----------
1. Short intrinsic tags -> long form.  Apsara ROS is strict about the
   !Ref / !GetAtt / !Sub ... shorthand; emit {"Ref": ...} /
   {"Fn::GetAtt": [...]} long form instead.
2. DataSource image lookups -> explicit-id parameters.  DATASOURCE::ECS::Images
   resources (auto-discovering the RHEL / Aliyun-Linux image) do not exist on
   Apsara; drop each and add a String parameter carrying the image id
   explicitly (MirrorImageLookup -> MirrorImageId), rewriting the
   !Select [0, !GetAtt XLookup.ImageIds] references to {"Ref": XId}.
3. Strip properties Apsara rejects (PROP_STRIP): e.g. ALIYUN::PVTZ::Zone Tags.
4. Substitute values Apsara requires (VALUE_SUBST): NAT type and disk category
   — see the table for the why of each.  Applied only to the named property
   keys, so descriptions and comments are left untouched (this replaces the
   ad-hoc `sed s/cloud_essd/cloud_pperf/` + NatType seds we used while probing).

Every transform is mechanical: run this on any ros-templates/*.yaml before
feeding the result to apsara-rpc CreateStack.

Usage:  apsara_ize.py <source-template.yaml> <apsara-output.yaml>
"""
import sys, yaml

# Intrinsic functions to expand from short tag to long form.
FN = ["Ref", "GetAtt", "Sub", "If", "Equals", "Not", "And", "Or", "Join",
      "Select", "FindInMap", "Base64", "GetAZs", "Split", "ImportValue", "Cidr",
      "Condition"]

# Intrinsics whose long form is a bare top-level key, not under "Fn::".
BARE_KEY = {"Ref", "Condition"}

# Resource types Apsara lacks — dropped, replaced by an explicit-id parameter.
REMOVE_TYPES = {"DATASOURCE::ECS::Images"}

# Resource types provisioned OUTSIDE ROS on Apsara (dropped here, created by
# ansible after the stack).  All private-DNS (PVTZ) resources: on Apsara
# PrivateZone is served by the CloudDns POP (see project_apsara_clouddns_pvtz),
# which ROS cannot reach — ROS would hit the public pvtz.aliyuncs.com and fail.
# ansible/tasks/apsara_privatezone.yml builds the zone/binding/records via
# `cloudcli clouddns`; 99-teardown deletes it.  DependsOn/Outputs that reference
# these dropped resources are cleaned up automatically below.
DROP_TYPE_PREFIXES = ("ALIYUN::PVTZ::",)

# Properties to strip per resource type (Apsara rejects them).
PROP_STRIP = {"ALIYUN::PVTZ::Zone": ["Tags"]}

# Apsara-specific value substitutions, keyed by the property name they apply to
# so descriptions/comments are never touched.  {prop: {public-value: apsara-value}}.
#   NatType:  ste3 offers only the legacy "Normal" NAT; "Enhanced" (public-cloud
#             default) is absent there — and Enhanced is what pulls in the
#             AliyunServiceRoleForNatgw service-linked role a non-root RAM user
#             cannot create.  Normal needs no SLR.
#   *DiskCategory / Category:  cloud_essd is not offered on the ste3 zones we
#             use; cloud_pperf (and cloud_sperf) are.  DescribeAvailableResource
#             is the per-zone source of truth — adjust these if your Apsara
#             environment reports different disk categories.
VALUE_SUBST = {
    "NatType":            {"Enhanced": "Normal"},
    "SystemDiskCategory": {"cloud_essd": "cloud_pperf"},
    "Category":           {"cloud_essd": "cloud_pperf"},  # DataDisk entries
}


class L(yaml.SafeLoader):
    """SafeLoader that understands the ROS short-tag intrinsics."""


def _mk(t):
    key = t if t in BARE_KEY else "Fn::" + t

    def construct(loader, node):
        if isinstance(node, yaml.ScalarNode):
            v = loader.construct_scalar(node)
            # !GetAtt "Res.Attr" -> ["Res", "Attr"]
            if t == "GetAtt" and isinstance(v, str):
                v = v.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            v = loader.construct_sequence(node, deep=True)
        else:
            v = loader.construct_mapping(node, deep=True)
        return {key: v}

    return construct


for _t in FN:
    L.add_constructor("!" + _t, _mk(_t))


def walk(node, fn):
    """Depth-first rewrite: fn(node) may return a replacement or None."""
    r = fn(node)
    if r is not None:
        return r
    if isinstance(node, dict):
        return {k: walk(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [walk(x, fn) for x in node]
    return node


def apply_value_subst(node):
    """Substitute Apsara-required values in place, matched by property key."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in VALUE_SUBST and isinstance(v, str) and v in VALUE_SUBST[k]:
                node[k] = VALUE_SUBST[k][v]
            else:
                apply_value_subst(v)
    elif isinstance(node, list):
        for x in node:
            apply_value_subst(x)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    d = yaml.load(open(src), Loader=L)
    res = d.get("Resources", {})
    params = d.setdefault("Parameters", {})

    # 2. DataSource image lookups -> explicit-id String parameters.
    removed = {}
    for name in list(res):
        if res[name].get("Type") in REMOVE_TYPES:
            p = name.replace("Lookup", "Id") if "Lookup" in name else name + "Id"
            params[p] = {"Type": "String",
                         "Description": f"Apsara explicit id (was DataSource {name})"}
            removed[name] = p
            del res[name]

    # Rewrite references to the removed lookups to Ref the new parameter.
    def repl(x):
        if not isinstance(x, dict):
            return None
        s = x.get("Fn::Select")
        if isinstance(s, list) and len(s) == 2 and isinstance(s[1], dict):
            ga = s[1].get("Fn::GetAtt")
            if isinstance(ga, list) and ga and ga[0] in removed:
                return {"Ref": removed[ga[0]]}
        ga = x.get("Fn::GetAtt")
        if isinstance(ga, list) and ga and ga[0] in removed:
            return {"Ref": removed[ga[0]]}
        return None

    d = walk(d, repl)

    # 2b. Drop PVTZ resources — provisioned by ansible (CloudDns) after the stack.
    res = d.get("Resources", {})  # re-fetch: walk() above rebuilt d into a new dict
    dropped_pvtz = [n for n in list(res)
                    if any(res[n].get("Type", "").startswith(p) for p in DROP_TYPE_PREFIXES)]
    for n in dropped_pvtz:
        del res[n]
    dropped = set(dropped_pvtz)

    # Clean DependsOn entries pointing at dropped resources.
    for r in res.values():
        dep = r.get("DependsOn")
        if isinstance(dep, list):
            kept = [x for x in dep if x not in dropped]
            if kept:
                r["DependsOn"] = kept
            else:
                r.pop("DependsOn", None)
        elif isinstance(dep, str) and dep in dropped:
            r.pop("DependsOn", None)

    # Drop Outputs whose Value references a dropped resource (Ref / GetAtt).
    def refs_dropped(x):
        if isinstance(x, dict):
            if x.get("Ref") in dropped:
                return True
            ga = x.get("Fn::GetAtt")
            if isinstance(ga, list) and ga and ga[0] in dropped:
                return True
            return any(refs_dropped(v) for v in x.values())
        if isinstance(x, list):
            return any(refs_dropped(v) for v in x)
        return False

    outs = d.get("Outputs", {})
    for oname in list(outs):
        if refs_dropped(outs[oname].get("Value")):
            del outs[oname]

    # 3. Strip properties Apsara rejects.
    for r in d.get("Resources", {}).values():
        st = PROP_STRIP.get(r.get("Type"))
        pr = r.get("Properties")
        if st and isinstance(pr, dict):
            for p in st:
                pr.pop(p, None)

    # 4. Apsara value substitutions (NAT type, disk category).
    apply_value_subst(d)

    yaml.safe_dump(d, open(dst, "w"), default_flow_style=False,
                   sort_keys=False, width=8192, allow_unicode=True)
    yaml.safe_load(open(dst))  # fail loudly if we emitted invalid YAML
    print("OK", src, "->", dst,
          "| removed", list(removed),
          "| dropped-pvtz", dropped_pvtz,
          "| stripped", PROP_STRIP,
          "| subst", {k: list(v.values()) for k, v in VALUE_SUBST.items()})


if __name__ == "__main__":
    main()
