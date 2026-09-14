#!/usr/bin/env python3
"""ansible/vars/images.yml 和 build-mirror-tarball.sh 的组件镜像 tag 必须一致。

`vars/images.yml` 是组件镜像的唯一事实来源,但 `build-mirror-tarball.sh` 是个
独立的 bash 脚本,读不到 ansible 变量,所以它带着自己的一份默认值。两处都写着
"keep in sync" 的注释 —— 然后它们还是漂了:images.yml 到了 CAPA v0.1.24 /
CSI v0.1.13,脚本的默认值还停在 v0.1.12 / v0.1.0。

漂了不会报错。mirror 里会多出三个没人用的旧镜像,然后 04 发现要的 tag 不在,
再从上游拉一遍 —— 在一条需要一两个小时才能建完 mirror 的链路上,这是实打实的
代价,而且没有任何一步会说「你拉错了」。

注释管不住漂移,所以改成门禁。

    python3 scripts/lint-image-tags.py
"""

import re
import sys
import os

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(HERE)
IMAGES = os.path.join(REPO, "ansible", "vars", "images.yml")
SCRIPT = os.path.join(REPO, "scripts", "build-mirror-tarball.sh")


def yaml_scalar(text, key):
    m = re.search(rf'^{re.escape(key)}:\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', text, re.M)
    return m.group(1).strip() if m else None


def sh_default(text, var):
    """取 VAR="${VAR:-默认值}" 里的默认值。"""
    m = re.search(rf'^{re.escape(var)}="\$\{{{re.escape(var)}:-(.*?)\}}"', text, re.M)
    return m.group(1).strip() if m else None


def main():
    for p in (IMAGES, SCRIPT):
        if not os.path.exists(p):
            sys.exit(f"lint-image-tags: 找不到 {p}")
    images = open(IMAGES).read()
    script = open(SCRIPT).read()

    capa_repo = yaml_scalar(images, "capa_image_repo")
    capa_tag = yaml_scalar(images, "capa_image_tag")
    csi_tag = yaml_scalar(images, "csi_operator_image_tag")
    ccm_tag = yaml_scalar(images, "ccm_image_tag")

    problems = []

    # CAPA:脚本里是完整引用
    want = f"{capa_repo}:{capa_tag}"
    got = sh_default(script, "OPENSHIFT_CAPI_IMAGE")
    if got != want:
        problems.append(f"OPENSHIFT_CAPI_IMAGE 默认值是 {got},images.yml 说应该是 {want}")

    # CSI:脚本里只有版本号
    got = sh_default(script, "CSI_OPERATOR_VERSION")
    if got != csi_tag:
        problems.append(f"CSI_OPERATOR_VERSION 默认值是 {got},"
                        f"images.yml 的 csi_operator_image_tag 是 {csi_tag}")

    # CCM:脚本里是完整引用,仓库路径是上游的固定地址,只比 tag
    got = sh_default(script, "ALIBABA_CCM_IMAGE") or ""
    got_tag = got.rsplit(":", 1)[-1] if ":" in got else None
    if got_tag != ccm_tag:
        problems.append(f"ALIBABA_CCM_IMAGE 的 tag 是 {got_tag},"
                        f"images.yml 的 ccm_image_tag 是 {ccm_tag}")

    if problems:
        print("镜像 tag 在两处不一致 —— 改一处就要改另一处:")
        print(f"  事实来源 : {os.path.relpath(IMAGES, REPO)}")
        print(f"  另一处   : {os.path.relpath(SCRIPT, REPO)}")
        for p in problems:
            print(f"  ✗ {p}")
        print()
        print("  注意 mirror-build.yml 走 ansible 时会用 images.yml 覆盖脚本默认值,")
        print("  所以这个不一致只在**单独跑脚本**时生效 —— 正因为如此它才会一直漂着没人发现。")
        return 1

    print(f"ok: CAPA {capa_tag} / CSI {csi_tag} / CCM {ccm_tag} 两处一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
