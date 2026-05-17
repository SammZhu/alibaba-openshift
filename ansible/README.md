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
├── inventory.yml                  # localhost; jump host 动态加入
├── group_vars/
│   ├── all.yml.example            # 配置模板
│   └── all.yml                    # 你的配置（gitignored）
├── tasks/
│   ├── load_state.yml             # 读 state.yml
│   ├── save_state.yml             # 合并写 state.yml
│   └── assisted_token.yml         # 刷新 Red Hat access token
├── playbooks/
│   ├── 00-preflight.yml           # CLI + 凭证 + 权限自检
│   ├── 01-prepare-iso.yml         # Assisted API → Discovery ISO
│   ├── 02-import-image.yml        # OSS 上传 + RAM 角色 + ImportImage + 等就绪
│   ├── 03-create-stack.yml        # ROS 栈 + 等就绪 + 输出
│   ├── 04-install-cluster.yml     # 分配角色 + 上传 manifest + 装集群 + 拉 kubeconfig
│   ├── 05-deploy-post-install.yml # 跳板上跑，部署 CAPI/CSI
│   ├── 99-teardown.yml            # 应用层清理 + 删栈 + 孤儿扫描
│   └── site.yml                   # 跑 00-04 一条龙
├── state.yml                      # 流水线状态（gitignored，自动生成）
└── README.md
```

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

## 跑

```sh
cd ansible

# 端到端（约 90 分钟）
ansible-playbook playbooks/site.yml

# 分步跑（调试时）
ansible-playbook playbooks/00-preflight.yml
ansible-playbook playbooks/01-prepare-iso.yml
ansible-playbook playbooks/02-import-image.yml
ansible-playbook playbooks/03-create-stack.yml
ansible-playbook playbooks/04-install-cluster.yml

# 04 完成后会把 kubeconfig scp 到跳板。SSH 进跳板跑 Phase 05：
ssh -i ~/.ssh/openshift_ed25519 root@$(yq '.jump_host_ip' state.yml)
cd /root/openshift-alibaba/alibaba-openshift
ansible-playbook ansible/playbooks/05-deploy-post-install.yml

# 销毁
ansible-playbook playbooks/99-teardown.yml
# 或不交互：
ansible-playbook playbooks/99-teardown.yml -e teardown_confirmed=true
```

## 失败恢复

每个 playbook 都做了幂等：

- `01`：cluster + infra-env 按 name+domain 查现有，没有再建；ISO 按大小校验已下载就跳过
- `02`：image 已 Available 就跳过；OSS 对象按大小校验
- `03`：stack 按名字查现有，状态对就 adopt
- `04`：manifest POST 接受 409（已存在），install 接受 409（已在装）

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
| Hosts 一直不上线 | ECS 实例没启动？登 ECS VNC 看是否进了 Discovery 界面 |
| 04 卡 ready | `ai_curl GET /clusters/<id>/host-requirements` 看 validation 哪条没过 |
