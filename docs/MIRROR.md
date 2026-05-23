# Disconnected Mirror Registry — 完整文档

为 OpenShift on Alibaba Cloud 提供**境内 air-gap mirror registry** 的完整方案。
解决 `cn-*` Region 跨境拉 `quay.io` / `registry.redhat.io` 不稳定的问题。

---

## 目录

- [架构](#架构)
- [成本](#成本)
- [组件清单](#组件清单)
- [完整工作流](#完整工作流)
  - [一次性：构建 tarball 上传 OSS](#工作流-1一次性构建-tarball-上传-oss)
  - [全新部署 cluster](#工作流-2全新部署-cluster)
  - [刷新 mirror 镜像（不动 cluster）](#工作流-3刷新-mirror-镜像不动-cluster)
  - [扩展 mirror 内容（operators / CCM）](#工作流-4扩展-mirror-内容operators--ccm)
  - [Teardown + 重建](#工作流-5teardown--重建)
- [配置参考](#配置参考)
  - [group_vars/all.yml](#group_varsallyml)
  - [构建脚本环境变量](#构建脚本环境变量)
  - [state.yml 自动写入字段](#stateyml-自动写入字段)
- [故障排查](#故障排查)
- [设计要点](#设计要点)
- [当前限制](#当前限制)

---

## 架构

```
 ┌────────────────────────────────────────┐
 │  境外构建主机                            │
 │  （RHEL 8 / 海外 ECS / 本地 dev 机）     │
 │                                        │
 │  scripts/build-mirror-tarball.sh:      │
 │    ① 查询 AI API 拿 3 个组件 image      │
 │    ② oc-mirror 拉 OCP release + 3 AI    │
 │       (~25 GB → ~21 GB tar)            │
 │    ③ aliyun oss cp 上传 OSS             │
 └────────┬───────────────────────────────┘
          │ 跨境上传（~1 h，一次性）
          ↓
 ┌────────────────────────────────────────────────────────────────────┐
 │  Aliyun OSS  oss://<bucket>/mirror-tarballs/<cluster>-<version>.tar │
 │  长期保存 ~21 GB / 4 RMB 月                                          │
 └────────┬───────────────────────────────────────────────────────────┘
          │ VPC 内网下载（免费 + 飞快）
          ↓
 ┌────────────────────────────────────────────────────────────────────┐
 │  cluster VPC (10.0.0.0/16)  ─ cn-wulanchabu                         │
 │                                                                     │
 │  Mirror ECS @ 10.0.16.4 (cluster ROS stack 的一部分)                │
 │    cloud-init 自动:                                                  │
 │      ① 挂数据盘 /var/lib/quay-storage                                │
 │      ② RAM Role + 内网 endpoint 拉 OSS tarball                       │
 │      ③ 装 mirror-registry (单容器 Quay)                              │
 │      ④ oc mirror import → 把 tarball 推进 Quay                       │
 │      ⑤ 写 /var/lib/mirror-ready 信号 + CA cert                       │
 │                                                                     │
 │  Cluster nodes (jump host + 3 masters)                              │
 │    discovery agent + bootstrap installer + master kubelet           │
 │      → 全部走 https://10.0.16.4:8443/* 拉镜像                        │
 │      → registries.conf insecure=true (cert 已经 PATCH 到 AI)         │
 │      → pull_secret 含 mirror 的 basic auth                           │
 └────────────────────────────────────────────────────────────────────┘
```

---

## 成本

| 项目 | 金额 | 说明 |
|------|------|------|
| OSS 存储（30 GB） | **~4 RMB / 月** | 长期保存 tarball |
| OSS 公网入站（上传） | **0** | 入站永远免费 |
| Mirror ECS（按量付费 4C8G + 100 GB ESSD） | **~1.6 RMB / 小时** | 只在测试 cycle 期间运行 |
| Mirror ECS 同 region VPC 流量 | **0** | 同 region 内网免费 |
| **典型一次测试 cycle**（部署 + 测试 + teardown 共 6-8 小时） | **~12 RMB** | 主要是 ECS 时间 |

**对比 ACR 企业版**：节省 **~85 RMB / 月**（前提是不长期持有 mirror ECS）。

---

## 组件清单

```
alibaba-openshift/
├── scripts/
│   └── build-mirror-tarball.sh             # ✦ 在境外构建主机跑
├── ros-templates/
│   └── create-cluster.yaml                  # 含 MirrorRegistryInstance 等 6 个资源
│                                            # （Condition: MirrorEnabled）
├── ansible/
│   ├── group_vars/
│   │   └── all.yml.example                  # mirror_* 参数定义
│   ├── tasks/
│   │   ├── load_state.yml                   # 加载含 mirror_* 的 state
│   │   └── save_state.yml                   # 保存 mirror_init_password 等
│   └── playbooks/
│       ├── 01-prepare-iso.yml               # mirror auth + ICS + ignition override
│       ├── 03-create-stack.yml              # 等 cloud-init + scp CA + PATCH AI
│       ├── mirror-rebuild.yml          ✦    # 刷新 mirror 镜像（不动 cluster）
│       ├── mirror-verify.yml           ✦    # 健康检查 + 镜像存在验证
│       └── 99-teardown.yml                  # 含 mirror RAM 资源预清理
└── docs/
    └── MIRROR.md                            # 本文档
```

(✦ = 专门为 mirror 工作流新加的)

---

## 完整工作流

### 工作流 1：一次性，构建 tarball 上传 OSS

**何时跑**：第一次启用 mirror / 升级 OpenShift 版本 / 加新镜像。  
**在哪跑**：境外构建主机（能稳定访问 `quay.io` + 装了 `aliyun` CLI）。

```bash
# 准备 pull-secret
mkdir -p ~/.docker
cp /path/to/pull-secret.json ~/.docker/config.json

# 拉代码（如果还没有）
git clone https://github.com/SammZhu/alibaba-openshift.git
cd alibaba-openshift

# 配 aliyun profile（如果还没配）
aliyun configure --profile openshift-test

# 跑构建脚本
OFFLINE_TOKEN_FILE=/path/to/offline-token \
OSS_BUCKET=openshift-iso-samzhu-test \
REGION=cn-wulanchabu \
CLUSTER_NAME=aliocp1 \
OPENSHIFT_VERSION=4.20 \
ALIYUN_PROFILE=openshift-test \
    ./scripts/build-mirror-tarball.sh
```

脚本会自动：

1. **AI API 查询 3 个组件 image**（discovery-agent / assisted-installer / assisted-installer-controller）—— 用 `OFFLINE_TOKEN_FILE` 换 SSO token，查 `/v2/component-versions`。
2. **Cincinnati graph 查最新 patch**——`stable-4.20` channel 当前最新（如 `4.20.22`），自动设 `minVersion=maxVersion` 只下这一个。
3. **oc-mirror 拉镜像** —— OCP release ~150 个 image + 3 AI 组件 = ~25 GB（30-60 分钟）。
4. **打 tarball + sha256** —— ~21 GB（压缩后）。
5. **上传 OSS** —— 跨境上传 ~1-2 小时（一次性）。

**手动覆盖关键参数**：

```bash
# 不让脚本查 AI API，自己指定 image
AI_AGENT_IMAGE=registry.redhat.io/rhai/assisted-installer-agent-rhel9:008935... \
AI_INSTALLER_IMAGE=registry.redhat.io/rhai/assisted-installer-rhel9:a9bfccc... \
AI_CONTROLLER_IMAGE=registry.redhat.io/rhai/assisted-installer-controller-rhel9:a9bfccc... \
    ./scripts/build-mirror-tarball.sh

# 指定具体 patch 版本而不是 channel 最新
OPENSHIFT_PATCH_VERSION=4.20.17 \
    ./scripts/build-mirror-tarball.sh
```

跑完会输出：

```
═══════════════════════════════════════════════════════════════════════
✓ Mirror tarball ready in OSS
  Bucket : openshift-iso-samzhu-test
  Object : mirror-tarballs/aliocp1-4.20.tar
  Size   : 21G
  SHA256 : <hash>
═══════════════════════════════════════════════════════════════════════
```

---

### 工作流 2：全新部署 cluster

```bash
# 在 ansible 那台机器
cd /path/to/alibaba-openshift

# 1. 改配置开 mirror
vi ansible/group_vars/all.yml
# 加：
#   mirror_enabled: true
#   mirror_oss_object: "mirror-tarballs/aliocp1-4.20.tar"

# 2. 跑完整流程
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-stack.yml    # 多 ~30 min（mirror cloud-init）

# 3. Phase 04 前先验证 mirror
ansible-playbook ansible/playbooks/mirror-verify.yml

# 4. 触发 install
ansible-playbook ansible/playbooks/04-install-cluster.yml
```

**Phase 01 实际改变**（mirror_enabled=true 时）：

- 生成 / 加载 `mirror_init_password`（auto-generate 或用 group_vars 设的），存 state.yml
- `pull_secret` 注入 `{mirror_ip}:8443` 的 basic auth entry
- `cluster body` 加 `install_config_overrides`（含 5 个 `imageDigestSources` 映射）
- `infra-env body` 加 `ignition_config_override`（含 registries.conf 注入到 `/etc/containers/`）

**Phase 03 实际改变**：

- ROS stack 多创建：MirrorRamRole / MirrorRamPolicy / MirrorRamAttach / 2 个 SecurityGroupIngress / MirrorRegistryInstance
- Mirror ECS cloud-init（~30 分钟）自动完成整套 setup
- ansible SSH polling `/var/lib/mirror-ready` 等就绪（最多 60 min）
- `scp` mirror CA cert 到 `output_dir/mirror-ca.crt`
- PATCH AI cluster + infra-env 注入 `additional_trust_bundle`（CA cert）

---

### 工作流 3：刷新 mirror 镜像（不动 cluster）

**何时用**：tarball 在 OSS 上更新了，想让 cluster 立即看到新镜像。

```bash
ansible-playbook ansible/playbooks/mirror-rebuild.yml

# 可选：不重新下载 OSS tarball（用 mirror ECS 上已存在的）
ansible-playbook ansible/playbooks/mirror-rebuild.yml -e mirror_redownload=false
```

做的事：

1. SSH 进 mirror ECS（经 jump host）
2. `aliyun oss cp` 重新下载 tarball（覆盖 `/var/lib/quay-storage/mirror-tarball.tar`）
3. `oc mirror --from=tarball docker://10.0.16.4:8443`（**增量**——只 push 新 layer）

整个过程 ~10-30 分钟，**cluster 完全不动**。

---

### 工作流 4：扩展 mirror 内容（operators / CCM）

OpenShift baseline 没包含 operator catalog 和 Alibaba CCM。要装这些，分两步：

**Step 1：在境外构建主机 imageset-config.yaml 加 packages**

```bash
cd /path/to/alibaba-openshift/mirror-build   # 上次构建留下的目录

# 编辑 imageset-config.yaml
cat > imageset-config.yaml <<EOF
apiVersion: mirror.openshift.io/v1alpha2
kind: ImageSetConfiguration
archiveSize: 10
storageConfig:
  local:
    path: ./mirror-data
mirror:
  platform:
    channels:
      - name: stable-4.20
        type: ocp
        minVersion: 4.20.22
        maxVersion: 4.20.22
  additionalImages:
    - name: registry.redhat.io/rhai/assisted-installer-agent-rhel9:008935...
    - name: registry.redhat.io/rhai/assisted-installer-rhel9:a9bfccc...
    - name: registry.redhat.io/rhai/assisted-installer-controller-rhel9:a9bfccc...
    # Alibaba CCM
    - name: registry.k8s.io/provider-alibaba-cloud/ccm:vX.Y.Z
  operators:
    - catalog: registry.redhat.io/redhat/redhat-operator-index:v4.20
      packages:
        - name: cert-manager
        - name: openshift-pipelines-operator-rh
        - name: serverless-operator
EOF

# 增量 oc-mirror（只下新加的）
oc-mirror --config=imageset-config.yaml file://./openshift-mirror

# 重打 tarball
tar -cf aliocp1-4.20.tar -C openshift-mirror .

# 上传覆盖 OSS
AK=$(jq -r '.profiles[] | select(.name=="openshift-test") | .access_key_id' ~/.aliyun/config.json)
SK=$(jq -r '.profiles[] | select(.name=="openshift-test") | .access_key_secret' ~/.aliyun/config.json)
aliyun oss cp aliocp1-4.20.tar oss://openshift-iso-samzhu-test/mirror-tarballs/aliocp1-4.20.tar \
  --endpoint=oss-cn-wulanchabu.aliyuncs.com \
  --access-key-id="$AK" --access-key-secret="$SK" \
  --part-size=104857600 --parallel=10 --force
```

**Step 2：刷新 mirror ECS**

```bash
# 在 ansible 机器
ansible-playbook ansible/playbooks/mirror-rebuild.yml
```

**Step 3：通知 OpenShift 用新的 catalog**

```bash
# 在 cluster 上（kubeconfig 已设好）
oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: redhat-operators-mirror
  namespace: openshift-marketplace
spec:
  sourceType: grpc
  image: 10.0.16.4:8443/redhat/redhat-operator-index:v4.20
  displayName: Red Hat Operators (Mirror)
EOF
```

---

### 工作流 5：Teardown + 重建

**Teardown**（默认 keep_image=true）：

```bash
ansible-playbook ansible/playbooks/99-teardown.yml -e teardown_from=3 -e teardown_confirmed=true
```

- 销毁整个 ROS stack（含 mirror ECS）
- **OSS tarball 不动**
- `state.yml` 清空 mirror_* 字段
- AI cluster + infra-env + ECS image 保留

**重建**（不需要重新 build tarball）：

```bash
# state.yml 还在但 mirror_* 已清空，mirror_enabled 仍然 true
ansible-playbook ansible/playbooks/03-create-stack.yml   # ~30 min（cloud-init 再跑一遍）
ansible-playbook ansible/playbooks/mirror-verify.yml
ansible-playbook ansible/playbooks/04-install-cluster.yml
```

---

## 配置参考

### group_vars/all.yml

| 参数 | 默认 | 说明 |
|------|------|------|
| `mirror_enabled` | `false` | 总开关 — false 时所有 mirror 逻辑 bypass，行为同今天 |
| `mirror_oss_object` | `mirror-tarballs/{{ cluster_name }}-{{ openshift_version }}.tar` | OSS 里 tarball 路径（相对 `oss_bucket`）|
| `mirror_private_ip` | `10.0.16.4` | mirror ECS 静态私网 IP（必须在 `private_subnet_cidr` 范围内）|
| `mirror_instance_type` | `ecs.g7.large` | 2vCPU/8GB 单 cluster 够用 |
| `mirror_data_disk_size` | `100` | GB，存 tarball + Quay 数据 |
| `mirror_init_user` | `mirror-admin` | Quay admin 用户名 |
| `mirror_init_password` | `""` | 留空 Phase 01 auto-generate（强烈推荐留空）|

### 构建脚本环境变量

| 变量 | 必需 | 说明 |
|------|------|------|
| `OSS_BUCKET` | ✅ | OSS bucket 名（与 group_vars 一致）|
| `CLUSTER_NAME` | ✅ | 决定 tarball 名 |
| `REGION` | 否 | 默认 `cn-wulanchabu` |
| `OPENSHIFT_VERSION` | 否 | 默认 `4.20` |
| `OPENSHIFT_PATCH_VERSION` | 否 | 默认自动查 Cincinnati 最新 patch |
| `OFFLINE_TOKEN_FILE` | 自动 | 留空 + 不传 `AI_*_IMAGE` 会报错 |
| `AI_AGENT_IMAGE` | 自动 | 优先级高于 `OFFLINE_TOKEN_FILE` |
| `AI_INSTALLER_IMAGE` | 自动 | 同上 |
| `AI_CONTROLLER_IMAGE` | 自动 | 同上 |
| `ALIYUN_PROFILE` | 否 | 默认 `openshift-test` |
| `PULL_SECRET` | 否 | 默认 `~/.docker/config.json` |
| `WORK_DIR` | 否 | 默认 `$(pwd)/mirror-build` |

### state.yml 自动写入字段

| 字段 | 何时写入 | 用途 |
|------|---------|------|
| `mirror_init_password` | Phase 01（auto-generate）| Phase 03 用同一个 password 装 mirror-registry |
| `mirror_private_ip` | Phase 03 | 后续 playbook 引用 |
| `mirror_init_user` | Phase 03 | 同上 |
| `mirror_ca_cert_path` | Phase 03 | 后续诊断 / 手动 oc login mirror 时用 |

---

## 故障排查

### Phase 03 卡在 "Wait for mirror registry"

```bash
# SSH 进 mirror ECS 看 cloud-init 进度
ssh -i ~/.ssh/<key> -J root@<jump-host-ip> root@10.0.16.4

# 看 cloud-init 全程日志
tail -200 /var/log/mirror-setup.log

# 看现在卡在哪一步
ps auxf | grep -E 'aliyun|oc-mirror|mirror-registry|podman'

# 看磁盘
df -h /var/lib/quay-storage

# 看 Quay 容器（mirror-registry install 成功后才有）
podman ps
```

| 现象 | 原因 | 处理 |
|------|------|------|
| `/var/log/mirror-setup.log` 不存在 | cloud-init 还没启动 | 等 1-2 min 或检查 ECS 实例状态 |
| 卡在 `aliyun oss cp` | OSS 下载慢或 RAM Role 没生效 | `aliyun oss ls oss://...` 测试连接 |
| 卡在 `mirror-registry install` | podman storage 或 SELinux | 看 log 尾部具体错误 |
| 卡在 `oc mirror` | tarball 损坏或 disk 满 | `sha256sum mirror-tarball.tar` 对比 |

### Phase 04 节点拉镜像失败

```bash
# SSH 进 master 看 agent log
ssh -J root@<jump-host-ip> core@<master-ip>
sudo journalctl -u agent.service -f
```

| 错误 | 原因 | 处理 |
|------|------|------|
| `401 Unauthorized` | `pull_secret` 没有 mirror auth | 重跑 Phase 01（auto-injects）|
| `connection refused` | mirror ECS 没起来 | `ansible-playbook mirror-verify.yml` |
| `manifest unknown` | tarball 没包含这个 image | 重 build tarball + `mirror-rebuild.yml` |
| `tls: bad certificate` | CA cert 没 PATCH 到 AI | 检查 Phase 03 最后两个 PATCH task 是否成功 |

### Mirror 健康检查

```bash
ansible-playbook ansible/playbooks/mirror-verify.yml
```

会检查：

- `/health/instance` 200 OK
- `/v2/_catalog` 能列出 repo
- 3 个 AI 组件 image 都在
- 至少一个 `openshift-release-dev/*` 在

### 手动登录 mirror Web UI

```bash
# 经 jump host 转发 8443 端口到本地
ssh -L 8443:10.0.16.4:8443 -i ~/.ssh/<key> root@<jump-host-ip>

# 浏览器开
https://localhost:8443
# 用户名/密码：mirror_init_user / mirror_init_password (都在 state.yml)
```

---

## 设计要点

### 为什么 mirror ECS 在 cluster ROS stack 内（不是独立 stack）

- ✅ 简化：一个 stack 一起管，teardown 一次性清理
- ✅ 同 VPC 同 VSwitch，无需 EIP / VPC peering / CEN
- ❌ 代价：每次重建 cluster 都要重新 `oc mirror` import（~25 分钟）
- 适合：**测试 / 开发场景**，反复 teardown 的工作流

### 为什么 cloud-init 用 Bash 而不是 ansible

- ROS UserData 限制 16 KB 经 Base64
- cloud-init 早于 ansible 能 SSH 进去的时机
- ansible 在 `ssh_polling /var/lib/mirror-ready` 处接管

### 为什么 mirror_init_password 在 Phase 01 生成而不是 Phase 03

Phase 01 build `pull_secret` 时就需要这个 password 注入 mirror auth entry。
Phase 03 后再生成就晚了——cluster + infra-env 已经用错误（缺 mirror auth）的
pull_secret 创建了。

state.yml 同步两阶段：Phase 01 生成 + 存，Phase 03 load 同一个值。

### 为什么 PATCH cluster 的 additional_trust_bundle 在 Phase 03 而不是 Phase 01

mirror 自签 CA 在 mirror-registry 第一次跑起来时生成。Phase 01 时 mirror ECS 还
不存在，没法拿到 CA。所以：

- Phase 01 用 `insecure=true` 让 discovery ISO 跳 TLS verify（无需 CA）
- Phase 03 mirror ready 后，`scp` CA 回来 + PATCH AI cluster/infra-env 注入
  `additional_trust_bundle`，让 install 阶段 install-config 信任 mirror

### 为什么 `registries.conf` 用 `mirror-by-digest-only = true`

OpenShift release image 都用 digest 引用（`@sha256:...`），不用 tag。设
`mirror-by-digest-only = true` 表示 mirror 只对 digest pull 生效——避免误
将 tag 形式的 pull 也重定向到 mirror（mirror 可能没那个 tag）。

---

## 当前限制

| 限制 | 影响 | 解决 |
|------|------|------|
| Mirror lifecycle = cluster lifecycle | 每次重建 cluster 都重 import ~25 min | 长期可独立 stack + VPC peering |
| OSS bucket 与 cluster 同 region | 多 region 不通用 | 改 cross-region OSS / CDN（增加复杂度）|
| Tarball 单文件 | 21 GB 单点失败 | oc-mirror 已用 archiveSize=10 分片，但脚本最后合一个 tar |
| 无内置 operator catalog | OperatorHub 显示空 | 按 [工作流 4](#工作流-4扩展-mirror-内容operators--ccm) 手动加 |
| 无 Alibaba CCM | LoadBalancer service 不工作 | 同上 |
| 静态 IP `10.0.16.4` 硬编码 | 与 `private_subnet_cidr` 强耦合 | 改 IP 时同时改 `mirror_private_ip` |
| `mirror_init_password` 明文存 state.yml | 安全担忧 | 加 Ansible Vault 加密（未实现） |

---

**相关文档**：
- [QUICKSTART.md](../QUICKSTART.md)
- [scripts/build-mirror-tarball.sh](../scripts/build-mirror-tarball.sh) （脚本顶部注释含 inline 用法）
- [ros-templates/create-cluster.yaml](../ros-templates/create-cluster.yaml) （搜 `Mirror` 看资源定义）
