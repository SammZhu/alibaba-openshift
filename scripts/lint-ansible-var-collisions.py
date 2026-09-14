#!/usr/bin/env python3
"""一个 playbook 的 `register:` 名字,不能和另一个 playbook 的 play `vars:` 同名。

**为什么这是个真问题**:registered 变量是**主机级的事实**,活到整个 run 结束,
而且**优先级高于 play vars**。所以 A playbook 里的 `register: x`,会静默压过
后面 B playbook 里的 `vars: x: ...` —— B 里每一处 `{{ x }}` 都变成 A 那个结果字典。

以前不发生,是因为每个阶段各跑各的 `ansible-playbook`,变量活不过进程。
`site-apsara.yml` 把整条链塞进**同一个进程**之后,这类碰撞才第一次成立 ——
2026-09-14 一次跑里撞了两个,而且两个的症状都不指向变量:

  08 `register: nas_sc` → 13 的 `nas_sc` play var 被盖
     → PVC 报 "cannot unmarshal object into ... storageClassName of type string"
  13 `register: _ns`    → 14/15 的 `_ns` play var 被盖
     → `oc delete ns "{'changed': True, 'stdout': 'namespace/csi-smoke created'...}"`

两个都花了很久才定位,因为**孤立地渲染那个变量永远是对的** —— 碰撞只在整条链
一起跑时才存在。

    python3 scripts/lint-ansible-var-collisions.py
"""

import os
import re
import sys
import glob

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
PLAYBOOKS = os.path.join(REPO, "ansible", "playbooks", "*.yml")


def scan(path):
    """(registered 名 -> 行号列表, play-var 名 -> 行号列表)"""
    regs, pvars, in_vars = {}, {}, False
    for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
        m = re.match(r"\s*register:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            regs.setdefault(m.group(1), []).append(lineno)

        # play 级 vars 块:`  vars:` 顶格两空格,键再缩进两格。
        # task 级 vars(缩进更深)不算——它们的作用域只有那个 task。
        if re.match(r"^  vars:\s*$", line):
            in_vars = True
            continue
        if in_vars:
            if re.match(r"^  \S", line) or line.startswith("- "):
                in_vars = False
            else:
                m2 = re.match(r"^    ([A-Za-z_][A-Za-z0-9_]*):", line)
                if m2:
                    pvars.setdefault(m2.group(1), []).append(lineno)
    return regs, pvars


def main():
    files = sorted(glob.glob(PLAYBOOKS))
    if not files:
        sys.exit(f"lint-ansible-var-collisions: 没找到 playbook({PLAYBOOKS})")

    all_regs, all_pvars = {}, {}
    for f in files:
        base = os.path.basename(f)
        regs, pvars = scan(f)
        for n, lns in regs.items():
            all_regs.setdefault(n, []).extend((base, ln) for ln in lns)
        for n, lns in pvars.items():
            all_pvars.setdefault(n, []).extend((base, ln) for ln in lns)

    problems = []
    for name in sorted(set(all_regs) & set(all_pvars)):
        reg_files = {f for f, _ in all_regs[name]}
        var_files = {f for f, _ in all_pvars[name]}
        # 同一个文件里自己 register 又自己当 play var,不是跨 play 覆盖,不管。
        if reg_files - var_files or var_files - reg_files:
            problems.append((name, all_regs[name], all_pvars[name]))

    if problems:
        print("registered 变量和别处的 play var 同名 —— 跨 play 会静默覆盖:\n")
        for name, regs, pvars in problems:
            print(f"  ✗ {name}")
            print(f"      register 于 : {', '.join(f'{f}:{l}' for f, l in regs)}")
            print(f"      play var 于 : {', '.join(f'{f}:{l}' for f, l in pvars)}")
        print()
        print("  registered 变量是主机级事实,活到 run 结束且优先级高于 play vars。")
        print("  改 register 的名字(加阶段前缀,例如 _csi_ns_create),不要改 play var —— ")
        print("  play var 是这个 playbook 的对外契约,register 只是它自己的中间结果。")
        return 1

    print(f"ok: 扫了 {len(files)} 个 playbook,register 名与 play var 无跨文件冲突")
    return 0


if __name__ == "__main__":
    sys.exit(main())
