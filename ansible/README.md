# Ansible 自动化（推荐路径）

跟 `scripts/` 等价，但用 Ansible 原生方式实现：

- 重试用 `retries:` + `until:`，不用 bash 循环
- 幂等用模块语义（`stat` / `creates:` / `changed_when:`），不用 `[ -f ]`
- HTTP 用 `ansible.builtin.uri`，自动 JSON 解析，不用 `curl | jq`
- 状态用 YAML `state.yml`，跨 playbook `include_vars` 读取
- 错误处理走 Ansible 自己的 `failed_when:` + 结构化输出

## 文件结构

```
ansible/
├── ansible.cfg
├── inventory.yml                       # localhost; jump host 动态加入
├── group_vars/
│   ├── all.yml.example                 # 配置模板
│   └── all.yml                         # 你的配置（gitignored）
├── tasks/                              # 共享 task include
│   ├── load_state.yml                  # 读 state.yml
│   ├── save_state.yml                  # 合并写 state.yml
│   ├── path_defaults.yml               # repo_root / output_dir 等路径默认值
│   ├── assisted_token.yml              # 刷新 Red Hat access token
│   ├── create_stack.yml                # 通用 ROS 建栈 + adopt + wait
│   ├── mirror_defaults.yml             # mirror_enabled 时填补 mirror_* 默认值
│   ├── mirror_stage_artefacts.yml      # 把 mirror tarball 暂存到本地/OSS
│   └── refresh_tag_mapping.yml         # 从 ImageSetConfig 重算 tag 映射
├── playbooks/
│   ├── 00-preflight.yml                # CLI + 凭证 + 权限自检（含 NLB SLR 检查）
│   ├── 01-prepare-iso.yml              # Assisted API → Discovery ISO（mirror_enabled 时注入 registries.conf）
│   ├── 02-import-image.yml             # OSS 上传 + RAM 角色 + ImportImage + 等就绪
│   │
│   │  ── split 流程（推荐）─────────────────────────────────────
│   ├── 03-create-mirror-stack.yml      # 建持久 mirror-stack（VPC + RAM + 跳板 + mirror ECS）
│   │                                   #   有 snapshot+image 时自动走 fast-path
│   ├── 04-prepare-mirror.yml           # oc-mirror d2m → 推 Quay（幂等，可重跑）
│   ├── 05-verify-mirror.yml            # mirror 健康检查 + smoke-test pull + 拍 vda/vdb 快照
│   ├── 06-create-cluster-stack.yml     # 建短命 cluster-stack（SG + NLB + DNS + masters/workers）
│   ├── 07-install-cluster.yml          # 注册 infra-env → 等节点 → 注入 manifest → 等装完 → 拉 kubeconfig
│   ├── 08-deploy-post-install.yml      # 在跳板上跑：CAPA、CSI、OADP 等 post-install
│   ├── 99-teardown.yml                 # split 流程销毁（teardown_target=cluster|mirror|both）
│   │
│   │  ── legacy 单栈流程 ──────────────────────────────────────
│   ├── 03-create-stack-LEGACY.yml      # 单一 monolithic 栈（VPC+RAM+mirror+masters+NLB+DNS）
│   ├── 99-teardown-LEGACY.yml          # legacy 流程销毁（teardown_from=1..4）
│   │
│   │  ── 辅助 ────────────────────────────────────────────────
│   ├── mirror-rebuild.yml              # 仅刷新 mirror 镜像内容（不动 cluster；与 04 重跑等价）
│   └── site.yml                        # 端到端跑 Phase 00→07（08 需手动在跳板跑）
├── state.yml                           # 流水线状态（gitignored，自动生成）
└── README.md
```

## 阿里云服务开通（Phase 03 前必做）

以下服务默认未激活，ROS 建栈时若未开通会报 `Service.Status.Illegal` 错误：

| 服务 | 开通链接 |
|---|---|
| OSS / ECS / VPC / SLB / ROS / RAM | 各自控制台首页点击开通 |
| **PrivateZone**（最易遗漏）| https://pvtz.console.aliyun.com — 进入后点"立即开通" |
| **NLB service-linked role**（最易遗漏）| `aliyun resourcemanager CreateServiceLinkedRole --ServiceName nlb.aliyuncs.com --endpoint resourcemanager.aliyuncs.com --profile <p>` — 00-preflight 会替你检查 |

## 一次性准备

```sh
# 1. Ansible 本身
pip install --user ansible-core>=2.16  # 或者 dnf install ansible-core

# 2. 配置
cp ansible/group_vars/all.yml.example ansible/group_vars/all.yml
vi ansible/group_vars/all.yml         # 改 oss_bucket / aliyun_profile / 文件路径

# 3. Red Hat offline token
# 打开 https://console.redhat.com/openshift/token → Load token → 复制
mkdir -p ~/.openshift
read -s X && echo "$X" > ~/.openshift/offline-token && chmod 600 ~/.openshift/offline-token && unset X
```

## 跑 — split 流程（推荐）

```sh
cd ansible

# 端到端 Phase 00→07（约 90 分钟）
ansible-playbook playbooks/site.yml

# 分步跑（调试时）
ansible-playbook playbooks/00-preflight.yml
ansible-playbook playbooks/01-prepare-iso.yml
ansible-playbook playbooks/02-import-image.yml
ansible-playbook playbooks/03-create-mirror-stack.yml
ansible-playbook playbooks/04-prepare-mirror.yml
ansible-playbook playbooks/05-verify-mirror.yml
ansible-playbook playbooks/06-create-cluster-stack.yml
ansible-playbook playbooks/07-install-cluster.yml

# 07 完成后会把 kubeconfig scp 到跳板。SSH 进跳板跑 Phase 08：
ssh -i ~/.ssh/openshift_ed25519 root@$(yq '.jump_host_ip' state.yml)
cd /root/openshift-alibaba/alibaba-openshift
ansible-playbook ansible/playbooks/08-deploy-post-install.yml

# 销毁
ansible-playbook playbooks/99-teardown.yml -e teardown_target=cluster -e teardown_confirmed=true   # 仅 cluster，mirror 保留
ansible-playbook playbooks/99-teardown.yml -e teardown_target=mirror  -e teardown_confirmed=true   # 仅 mirror（先确保 cluster 已删）
ansible-playbook playbooks/99-teardown.yml -e teardown_target=both    -e teardown_confirmed=true   # 全部
# 完整 teardown 矩阵见 docs/TEARDOWN.md
```

## 跑 — legacy 单栈流程

只在需要单栈部署时使用（一次性 PoC、RAM 权限受限等）：

```sh
ansible-playbook playbooks/00-preflight.yml
ansible-playbook playbooks/01-prepare-iso.yml
ansible-playbook playbooks/02-import-image.yml
ansible-playbook playbooks/03-create-stack-LEGACY.yml      # 单栈：VPC+RAM+mirror+masters+NLB+DNS
ansible-playbook playbooks/04-prepare-mirror.yml           # mirror_enabled=true 时
ansible-playbook playbooks/07-install-cluster.yml

# 销毁（注意是 LEGACY 后缀的 teardown）
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_from=3 -e teardown_confirmed=true
```

> **不要混跑** split 和 legacy：state.yml 字段不同
> （split: `mirror_stack_id` + `cluster_stack_id` vs legacy: `ros_stack_id`），
> 但 mirror_enabled 公共字段会冲突。切换流程前先清空 `state.yml`。

## Disconnected install via private mirror（cn-* region）

跨境拉 `quay.io` 不稳时，启用 mirror 让节点全部走 VPC 内网拉镜像：

```sh
# 1. 在境外构建主机构建 tarball 上传 OSS（一次性，跑 scripts/build-mirror-tarball.sh）

# 2. 在 group_vars/all.yml 开 mirror
echo 'mirror_enabled: true' >> group_vars/all.yml
echo 'mirror_oss_object: "mirror-tarballs/aliocp1-4.20.tar"' >> group_vars/all.yml

# 3. 正常跑 split 流程，04 跑 oc-mirror d2m → Quay
ansible-playbook playbooks/01-prepare-iso.yml
ansible-playbook playbooks/02-import-image.yml
ansible-playbook playbooks/03-create-mirror-stack.yml      # 含 mirror ECS
ansible-playbook playbooks/04-prepare-mirror.yml           # ~30 min（d2m + push）
ansible-playbook playbooks/05-verify-mirror.yml            # 健康检查 + 拍快照
ansible-playbook playbooks/06-create-cluster-stack.yml
ansible-playbook playbooks/07-install-cluster.yml

# 后续刷新 mirror 镜像（加 operator / 升级版本）：直接重跑 04 即可（幂等）
ansible-playbook playbooks/04-prepare-mirror.yml
# mirror-rebuild.yml 是历史遗留，与 04 重跑等价
```

📘 **完整文档**：[`docs/MIRROR.md`](../docs/MIRROR.md)（架构、成本、配置参考、故障排查、设计要点）

## 失败恢复

每个 playbook 都做了幂等：

- `01`：cluster + infra-env 按 name+domain 查现有，没有再建；ISO 按大小校验已下载就跳过
- `02`：image 已 Available 就跳过；OSS 对象按大小校验
- `03`（mirror-stack 或 LEGACY）：stack 按名字查现有，状态对就 adopt；mirror-stack 有 snapshot+image 时走 fast-path
- `04`：oc-mirror d2m + push 全程幂等，重跑只补差量
- `05`：smoke-test + 拍快照；旧快照会先删后建
- `06`：cluster-stack 按名字查现有；mirror-stack outputs 缺失会 assert fail
- `07`：manifest POST 接受 409（已存在），install 接受 409（已在装）

任何一步失败重跑同一条命令即可。`state.yml` 记录所有中间 ID，跨 playbook 透传。

## 与 `scripts/` 的关系

`scripts/` 里的 bash 等价物保留作快速参考，但不再维护新特性。**生产用 ansible/**。

## 故障排查

| 现象 | 检查 |
|---|---|
| `failed_when` 触发 | playbook 输出会显示具体 task + 模块返回 |
| Token 失效 | offline token 被 revoke，去 https://console.redhat.com/openshift/token 重拿 |
| `ImportImage` 报 Forbidden.RAM | 02 第一次跑会自建 `AliyunECSImageImportDefaultRole`，等 30s 后会自动重试 |
| ROS stack `CREATE_FAILED` | `aliyun ros GetStackResources --StackId ...` 看哪个资源失败 |
| `OperationDenied.ServiceLinkedRoleNotExist`（建 NLB 时）| NLB SLR 未建 — 跑表格顶部那条 `CreateServiceLinkedRole` |
| Hosts 一直不上线 | ECS 实例没启动？登 ECS VNC 看是否进了 Discovery 界面 |
| 07 卡 ready | `ai_curl GET /clusters/<id>/host-requirements` 看 validation 哪条没过 |
| 03 卡 "Wait for mirror registry"（mirror 启用时）| 经 jump host SSH 进 mirror ECS `tail /var/log/mirror-setup.log` 看 cloud-init 进度，或 `05-verify-mirror.yml` 跑健康检查 |
| 07 节点拉 mirror 镜像 401 | `pull_secret` 没有 mirror auth — 重跑 Phase 01 自动注入 |
| 07 节点拉 mirror 镜像 manifest unknown | tarball 缺这个 image — 重 build + 重跑 `04-prepare-mirror.yml` |
