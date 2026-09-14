#!/usr/bin/env python3
"""跨 playbook 的「事实」不能和别处的 play `vars:` 同名 —— `register:` 和 `set_fact:` 都算。

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

**2026-09-14 扩容**:这条门禁最初只查 `register:`,**因此拦不住同一天晚些时候那个**
—— `tasks/install_agent.yml`(07 用)`set_fact: _ssh_proxy_args` 且**没有守卫**,在
没有跳板机的专有云上拼出 `ProxyCommand='ssh … root@'`(主机名为空)。那是主机级事实,
压过 08a 自己那份**带守卫**的同名 play var,于是 08a 拿着 07 留下的坏值去连 mirror,
报 `Could not resolve hostname :` —— 既不指向 07,也不指向 `jump_host_ip`。

**故意跨 playbook 共享的名字写在 SHARED 里**,等于一句声明:
「它确实会穿过 playbook,所以**每一处定义都必须语义一致**」。
上面那个 bug 就是违反了这一条 —— iso_agent 的同名变量有守卫,install_agent 的没有。

    python3 scripts/lint-ansible-var-collisions.py
"""

import os
import re
import sys
import glob

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
PLAYBOOKS = os.path.join(REPO, "ansible", "playbooks", "*.yml")
TASKS = os.path.join(REPO, "ansible", "tasks", "*.yml")

# 故意跨 playbook 流动的名字。放进来不是豁免,是**声明**:
# 它会穿过 playbook,所以每一处定义都必须语义一致 —— 差一个守卫就是一次事故。
SHARED = {
    "_ssh_keepalive":    "ABI 的 ssh 选项,跨 playbook 流动",
    "_ssh_common_args":  "同上",
    "_ssh_proxy_args":   "同上",
    "_ssh_root_mirror":  "同上(由上面三个拼出)",
    "_effective_control_plane_count": "06/06b 算出实际拓扑,07 沿用",
    "_effective_compute_count":       "同上",
    "iso_path":          "path_defaults 算出,01 沿用;07 会在 state 清空时重新推导",
}

# SHARED 里**还要求每一处定义逐字一致**的那些。声明共享只说明「它会穿过 playbook」,
# 不保证各处写法相同 —— 而 2026-09-14 那个 bug 恰恰是:iso_agent 的 _ssh_proxy_args
# 带守卫、install_agent 的不带,两份都是事实,后者赢。所以这几个名字上加一条硬约束:
# 定义分歧 = CI 失败。
STRICT = {"_ssh_keepalive", "_ssh_common_args", "_ssh_proxy_args", "_ssh_root_mirror"}


def definitions(path, names):
    """取出 `key: value` 的 value(含续行),用于 STRICT 的逐字比对。"""
    out = {}
    lines = open(path, encoding="utf-8").readlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):(.*)$", line)
        if not m or m.group(2) not in names:
            continue
        indent, name, rest = len(m.group(1)), m.group(2), m.group(3)
        body = [rest]
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip():
                break
            ni = len(nxt) - len(nxt.lstrip(" "))
            if ni <= indent:
                break
            body.append(nxt)
        # 归一化:压掉所有空白,只比 token 串
        out.setdefault(name, []).append(
            (os.path.basename(path), i + 1, re.sub(r"\s+", " ", " ".join(body)).strip()))
    return out


def scan(path):
    """(事实名 -> 行号列表, play-var 名 -> 行号列表)。事实 = register + set_fact。"""
    regs, pvars, in_vars = {}, {}, False
    lines = open(path, encoding="utf-8").readlines()
    for idx, line in enumerate(lines):
        lineno = idx + 1
        m = re.match(r"\s*register:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", line)
        if m:
            regs.setdefault(m.group(1), []).append(lineno)
        # set_fact: 下面比它更深缩进的键,都是这个任务设的事实
        if re.search(r"set_fact:\s*$", line):
            base = len(line) - len(line.lstrip(" "))
            for j in range(idx + 1, len(lines)):
                m2 = re.match(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*):", lines[j])
                if not m2 or len(m2.group(1)) <= base:
                    break
                regs.setdefault(m2.group(2), []).append(j + 1)
    for lineno, line in enumerate(lines, 1):

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
    files = sorted(glob.glob(PLAYBOOKS)) + sorted(glob.glob(TASKS))
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
        if name in SHARED:
            continue
        if reg_files - var_files or var_files - reg_files:
            problems.append((name, all_regs[name], all_pvars[name]))

    # STRICT:声明共享的那几个,各处定义必须逐字一致
    defs = {}
    for f in files:
        for n, lst in definitions(f, STRICT).items():
            defs.setdefault(n, []).extend(lst)
    for name in sorted(defs):
        variants = {}
        for fn, ln, val in defs[name]:
            variants.setdefault(val, []).append(f"{fn}:{ln}")
        if len(variants) > 1:
            print(f"  ✗ {name} —— 声明为跨 playbook 共享,但各处定义**不一致**:")
            for val, where in variants.items():
                print(f"      {', '.join(where)}")
                print(f"        {val[:150]}")
            print("      共享的名字必须每一处写法相同,否则谁先设谁赢,而赢的那个可能少一个守卫。")
            problems.append((name, [], []))

    if problems:
        print("事实名(register/set_fact)和别处的 play var 同名 —— 跨 play 会静默覆盖:\n")
        for name, regs, pvars in problems:
            print(f"  ✗ {name}")
            print(f"      事实设于   : {', '.join(f'{f}:{l}' for f, l in regs)}")
            print(f"      play var 于 : {', '.join(f'{f}:{l}' for f, l in pvars)}")
        print()
        print("  registered 变量是主机级事实,活到 run 结束且优先级高于 play vars。")
        print("  改 register 的名字(加阶段前缀,例如 _csi_ns_create),不要改 play var —— ")
        print("  play var 是这个 playbook 的对外契约,register 只是它自己的中间结果。")
        return 1

    print(f"ok: 扫了 {len(files)} 个文件,事实名(register/set_fact)与 play var "
          f"无跨文件冲突({len(SHARED)} 个名字已声明为故意共享)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
