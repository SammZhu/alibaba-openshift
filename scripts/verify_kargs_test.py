#!/usr/bin/env python3
"""Self-contained tests for verify_kargs.verify (no external fixtures)."""
import os
import tempfile

import verify_kargs as vk

FORBID = ["metal", "openstack", "qemu"]


def _mk(d, entries, grub):
    os.makedirs(os.path.join(d, "entries"), exist_ok=True)
    for i, body in enumerate(entries):
        with open(os.path.join(d, "entries", f"ostree-{i}.conf"), "w") as f:
            f.write(body)
    if grub is not None:
        with open(os.path.join(d, "grub.cfg"), "w") as f:
            f.write(grub)


def case(name, entries, grub, want_ok, baseline=None):
    with tempfile.TemporaryDirectory() as d:
        _mk(d, entries, grub)
        ok, problems, _ = vk.verify(d, "aliyun", FORBID, baseline)
        assert ok == want_ok, f"{name}: ok={ok} want={want_ok} problems={problems}"
        print(f"  ok  {name}")


def main():
    aliyun = "options ignition.platform.id=aliyun console=tty0 root=UUID=x rw\n"
    metal = "options ignition.platform.id=metal console=tty0 root=UUID=x rw\n"
    none = "options console=tty0 root=UUID=x rw\n"
    grub_ok = "linux /vmlinuz ignition.platform.id=aliyun root=UUID=x rw\n"

    case("all-aliyun passes", [aliyun], grub_ok, True)
    case("residual metal fails", [aliyun, metal], grub_ok, False)
    case("grub residual fails", [aliyun], "linux /vmlinuz ignition.platform.id=metal rw\n", False)
    case("no platform id fails", [none], None, False)
    case("empty dir fails", [], None, False)
    case("baseline match passes",
         ["options ignition.platform.id=aliyun console=tty0 root=UUID=x rw\n"], None, True,
         baseline=["ignition.platform.id", "console", "root", "rw"])
    case("baseline drift fails",
         ["options ignition.platform.id=aliyun console=tty0 root=UUID=x rw NEW.k=1\n"], None, False,
         baseline=["ignition.platform.id", "console", "root", "rw"])
    print("verify_kargs_test: all passed")


if __name__ == "__main__":
    main()
