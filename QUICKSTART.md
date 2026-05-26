# OpenShift on Alibaba Cloud — 端到端 QUICKSTART

> ⚠️ **本指南示范的是 LEGACY 单 stack 流程**（`create-cluster-LEGACY.yaml` +
> `03-create-stack-LEGACY.yml` + 老 `99-teardown` 的 `teardown_from` 参数）。
> 新的 split 流程（mirror-stack + cluster-stack 解耦，频繁重建更快）见
> [README.md → "Two flows: monolithic vs split"](README.md#two-flows-monolithic-vs-split-mirrorcluster-decoupled)。
>
> 命令对照速查：
>
> | LEGACY | SPLIT |
> |---|---|
> | `03-create-stack-LEGACY.yml` | `03-create-mirror-stack.yml` + `06-create-cluster-stack.yml` |
> | `99-teardown-LEGACY.yml -e teardown_from=3` | `99-teardown.yml -e teardown_target=cluster` |

从零开始安装一套 OpenShift 集群到阿里云，包含 CCM、CSI、CAPI、备份四层，
**约 90–120 分钟**。

> 详细架构和设计原理见 [README.md](README.md)。本指南只给路径，不解释为什么。
>
> **想全自动跑？** ✅ 用 [`ansible/`](ansible/README.md) —— 一条命令端到端：
> ```sh
> cp ansible/group_vars/all.yml.example ansible/group_vars/all.yml && vi $_
> cd ansible && ansible-playbook playbooks/site.yml
> ```
>
> **想手动跑或调试？** 用 [`docs/test-walkthrough.md`](docs/test-walkthrough.md) ——
> 完整的"按步骤照做"操作手册（含 Assisted/Agent-based 两条路径、
> 每个命令的预期输出、故障排查表、成本核对清单）。

---

## 概览

```
Phase 0  准备 OpenShift 引导镜像（一次性）         ~30 min
Phase 1  ROS 创建云基础设施                         ~10 min
Phase 2  安装 OpenShift（Agent-based 或 Assisted） ~45 min
Phase 3  应用 post-install 组件（一键脚本）         ~10 min
Phase 4  验证安装                                   ~5 min
Phase 5  Day-2 操作示例                             按需
```

---

## 准备工作

### 阿里云账号

- RAM 用户权限：`AdministratorAccess` 最简单（生产环境再精细化）
- 决定 **Region**（如 `cn-wulanchabu`）— 整个流程都在同一个 Region

#### 必须提前开通的云服务

以下服务默认未激活，**Phase 03 建栈前必须全部开通**，否则 ROS 会报 `Service.Status.Illegal` 错误：

| 服务 | 用途 | 开通链接 |
|---|---|---|
| **OSS** 对象存储 | 上传 Discovery ISO | https://oss.console.aliyun.com |
| **ECS** 弹性计算 | 节点实例 | https://ecs.console.aliyun.com |
| **VPC** 专有网络 | 网络基础设施 | https://vpc.console.aliyun.com |
| **SLB** 负载均衡 | API/MCS/Ingress 入口 | https://slb.console.aliyun.com |
| **PrivateZone** 私有 DNS | 集群内部域名解析（api.* / api-int.*）| https://pvtz.console.aliyun.com |
| **ROS** 资源编排 | 一键建栈 | https://ros.console.aliyun.com |
| **RAM** 访问控制 | 节点 IAM 角色 | https://ram.console.aliyun.com |
| **NAS** 文件存储 | ReadWriteMany PV（可选）| https://nas.console.aliyun.com |

> **注意**：PrivateZone 是最容易被遗漏的一个。进入控制台时若看到"服务未开通"提示，点击**立即开通**并同意协议即可，按量计费、费用可忽略不计。

#### RAM 子账号必须授予的权限策略

使用 RAM 子账号运行时，以下策略缺一不可（`AdministratorAccess` 可一次覆盖全部）：

| 策略名 | 涉及资源 | 备注 |
|---|---|---|
| `AliyunOSSFullAccess` | OSS | ISO 上传、存储桶管理 |
| `AliyunECSFullAccess` | ECS | 镜像导入、实例管理 |
| `AliyunVPCFullAccess` | VPC / VSwitch / Route Table | |
| **`AliyunEIPFullAccess`** | **弹性公网 IP** | **单独授权，VPCFullAccess 不包含；缺少时报 `Forbidden.RAM`** |
| **`AliyunNATGatewayFullAccess`** | **NAT 网关 / SNAT** | **单独授权，VPCFullAccess 不包含；缺少时报 `Forbidden.RAM`** |
| `AliyunSLBFullAccess` | SLB / Listener | |
| `AliyunPvtzFullAccess` | PrivateZone DNS | 缺少时报 `NoPermission.Operator` |
| `AliyunROSFullAccess` | ROS 资源栈 | |
| `AliyunRAMFullAccess` | RAM Role / Policy | 节点实例角色的创建与删除 |
| `AliyunTagAdministratorAccess` | 资源标签 | ROS 栈给 VPC/ECS/SLB 等资源打 `kubernetes.io/cluster/` 标签时必须 |
| `AliyunNASFullAccess` | NAS | ReadWriteMany PV（可选）|

> **重要**：`AliyunEIPFullAccess` 和 `AliyunNATGatewayFullAccess` 均需**单独授权**，`AliyunVPCFullAccess` 不包含这两项。缺少时均报 `Forbidden.RAM`。如果账号有权限边界（Permission Boundary）或资源组级别管控，还需要确认这些策略在**资源组层面**也已授权。

### 本地工具

> 本项目的安装指令都按 **RHEL 8 / Alibaba Linux 3**（EL8 兼容）写。
> 其它发行版（Ubuntu / macOS）请自行替换包管理器。

| 工具 | 用途 | RHEL 8 / AL3 安装 |
|---|---|---|
| `aliyun` CLI | 操作阿里云 | GitHub release 二进制（详见 `docs/test-walkthrough.md` §0.3）|
| `oc` / `kubectl` 4.20 | 集群访问 | mirror.openshift.com `openshift-client-linux-amd64-rhel8.tar.gz`（**必须 -rhel8 版**，否则 GLIBC 报错）|
| `openshift-install` 4.20 | Agent-based 必需 | mirror.openshift.com `openshift-install-linux.tar.gz`（静态 Go，generic 版可用）|
| `ansible-core` ≥ 2.16 | 自动化主路径 | `sudo dnf install ansible-core` |
| `kustomize` v5+ | Phase 3 部署组件 | `github.com/kubernetes-sigs/kustomize` linux_amd64 二进制 |
| `jq` / `yq` / `git` | 通用 | `sudo dnf install jq git` + yq GitHub 二进制 |
| `podman` | 仅自己构建镜像时 | `sudo dnf install podman` |

### Red Hat 账号

- [console.redhat.com/openshift](https://console.redhat.com/openshift) 账号
- Pull Secret（在控制台 → Downloads → Pull secret 下载）
- SSH 公钥（用来调试节点）

### 仓库

把这三个 git 仓库 **clone 到同一个父目录**——自动化（ansible/、scripts/）假设它们是同级目录：

```sh
mkdir openshift-alibaba && cd openshift-alibaba
git clone <url-to>/alibaba-openshift.git
git clone <url-to>/alibaba-cloud-csi-operator.git
git clone <url-to>/openshift-capi-alicloud.git
```

---

## Phase 0 — 准备 OpenShift 引导镜像

详细操作见 [`docs/boot-image-import.md`](docs/boot-image-import.md)。

简要：

1. **Assisted**：从 Red Hat Console 下载 Discovery ISO
   **Agent-based**：本地 `openshift-install agent create image` 生成 `agent.x86_64.iso`
2. 上传到阿里云 OSS Bucket（与目标 Region 相同）
3. ECS → Images → Import Image（参数：Linux / Others_Linux / x86_64 / ISO）
4. 等 15–30 分钟，记下生成的 `m-bp1xxx...` Image ID

---

## Phase 1 — 创建云基础设施

打开 [ROS 控制台](https://ros.console.aliyun.com/) → **Create Stack** → **Use the file**，
上传 `ros-templates/create-cluster-LEGACY.yaml`，填以下参数：

| 参数 | 生产示例 | 测试（compact 3 节点）|
|---|---|---|
| `ClusterName` | `cluster1` | 同上 |
| `BaseDomain` | `example.com` | 同上 |
| `Region` | `cn-wulanchabu` | 同上 |
| `ImageId` | Phase 0 得到的 `m-bp1...` | 同上 |
| `InstallationMethod` | `Assisted` 或 `Agent-based` | 同上 |
| `ControlPlaneCount` | `3` | `3` |
| `ComputeCount` | `2` | **`0`** ← 关键：让 master 也跑工作负载 |
| `ControlPlaneInstanceType` | `ecs.g7.4xlarge` | `ecs.g7.xlarge`（4C/16G）|

剩下保留默认。点 **Create**，等 10 分钟。

> **成本优化提示**：测试时用 `ComputeCount=0` + `ecs.g7.xlarge` 拼成 compact 3 节点，
> 总成本约 ¥4/h（按量付费），日均 ¥80-100。验证完销毁即可。
> Production 上线时再切回 3 master + 2 worker 的标准拓扑。
> 详见下文"测试预算控制"章节。

### Stack Outputs 里要保存的值

栈创建完成后到 **Outputs** 标签页，把这两个输出**完整复制下来**：

- `InstallConfig` —— `install-config.yaml` 内容（Assisted 粘贴到 Red Hat Console；Agent-based 写到本地）
- `DynamicCustomManifest` —— 保存为 `alibaba-ccm-config.yaml`，作为 install-time custom manifest 上传

还有这些值后续要用：`ApiSLBIp`、`VpcId`、`PrivateVSwitch`、`WorkerSecurityGroup`、`NodeRamRoleName`。

---

## Phase 2 — 安装 OpenShift

### 路径 A：Assisted Installer

1. [console.redhat.com/openshift](https://console.redhat.com/openshift) → **Create cluster** → **Datacenter** → **Bare Metal**
2. 把 `InstallConfig` 输出粘贴到 **Use saved install config**
3. 进入 **Host discovery** 等节点上线（5–10 分钟，节点会自动跑发现 agent 上报）
4. 给节点指派角色：3 master + N worker
5. 上传 **Custom Manifests**（4 个文件）：

   | 文件 | 来源 |
   |---|---|
   | `alibaba-ccm-config.yaml` | ROS Stack `DynamicCustomManifest` 输出 |
   | `00-ovn-mtu.yaml` | 仓库 `custom_manifests/00-ovn-mtu.yaml` |
   | `01-alibaba-ccm.yaml` | 仓库 `custom_manifests/01-alibaba-ccm.yaml` |
   | `03-machineconfig-providerid.yaml` | 仓库 `custom_manifests/03-machineconfig-providerid.yaml` |

6. 点 **Start installation**，等约 45 分钟

### 路径 B：Agent-based Installer

```sh
# 把 ROS 的 InstallConfig 输出写到本地
mkdir -p install-dir/openshift
cat > install-dir/install-config.yaml <<EOF
<粘贴 InstallConfig 输出>
EOF
# 填入 pullSecret 和 sshKey

# agent-config.yaml（rendezvousIP 必须匹配 ROS RendezvousIp 参数）
cat > install-dir/agent-config.yaml <<EOF
<粘贴 AgentConfig 输出>
EOF

# 把所有 install-time manifest 拷进 openshift/
cp custom_manifests/00-ovn-mtu.yaml install-dir/openshift/
cp custom_manifests/01-alibaba-ccm.yaml install-dir/openshift/
cp custom_manifests/03-machineconfig-providerid.yaml install-dir/openshift/

# 把 ROS 输出的 DynamicCustomManifest 写为 alibaba-ccm-config.yaml
cat > install-dir/openshift/alibaba-ccm-config.yaml <<EOF
<粘贴 DynamicCustomManifest 输出>
EOF

# 生成 agent ISO，导入阿里云作为新的 ImageId（如果还没有）
openshift-install agent create image --dir install-dir/

# 监控安装进度
openshift-install agent wait-for bootstrap-complete --dir install-dir/
openshift-install agent wait-for install-complete --dir install-dir/
```

完成后 kubeconfig 在 `install-dir/auth/kubeconfig`。

---

## Phase 3 — 应用 post-install 组件

> 推荐用 Ansible（在跳板上）：
> ```sh
> ansible-playbook ansible/playbooks/08-deploy-post-install.yml
> ```

或 shell 等价版本：

```sh
# 设置 kubeconfig
export KUBECONFIG=install-dir/auth/kubeconfig
# 或 Assisted：从 Red Hat Console 下载 kubeconfig

# 验证集群可达
oc get nodes

# 一键部署 CAPI + CSI Operator + CSI Driver CR
./scripts/05-deploy-post-install.sh

# 如果要装 OADP 备份：
./scripts/05-deploy-post-install.sh --with-oadp
# 然后编辑 OSS 凭证并 apply：
vi custom_manifests/05-oadp-oss-credentials.yaml
oc apply -f custom_manifests/05-oadp-oss-credentials.yaml
oc apply -f custom_manifests/05-oadp-dpa.yaml
```

**仅测试，不上 OperatorHub**：脚本默认走 OLM 旁路（`kustomize build | oc apply`）。

**走 OLM 正式路径**：把脚本里的 CSI 阶段换成：
```sh
oc apply -f custom_manifests/04-csi-catalogsource.yaml
oc apply -f custom_manifests/04-csi-operatorgroup.yaml
oc apply -f custom_manifests/04-csi-subscription.yaml
# 等 Subscription Healthy 后
oc apply -f custom_manifests/04-csi-driver-cr.yaml
```

---

## Phase 4 — 验证

```sh
# 1. 节点 Ready + ProviderID 注入
oc get nodes -o wide
oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.providerID}{"\n"}{end}'
# 应见 alicloud://<region>.<instance-id>

# 2. CCM 健康
oc get pods -n alibaba-cloud-controller-manager
oc logs -n alibaba-cloud-controller-manager -l k8s-app=alibaba-cloud-controller-manager --tail=20

# 3. CSI Operator + Driver
oc get pods -n alibaba-cloud-csi-operator-system
oc get alibabacloudcsidriver cluster -o yaml
oc get storageclass

# 4. CAPI Provider
oc get pods -n capa-system
oc get crd | grep cluster.x-k8s.io

# 5. ClusterOperators 全部 Available
oc get clusteroperators

# 6. 实际挂盘测试
cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-disk
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
  storageClassName: alicloud-disk-essd
---
apiVersion: v1
kind: Pod
metadata:
  name: test-disk-pod
spec:
  containers:
    - name: app
      image: registry.access.redhat.com/ubi9/ubi-minimal:latest
      command: [sh, -c, "echo hello > /data/x && sleep 3600"]
      volumeMounts:
        - mountPath: /data
          name: vol
  volumes:
    - name: vol
      persistentVolumeClaim:
        claimName: test-disk
EOF

oc wait pod/test-disk-pod --for=condition=Ready --timeout=120s
oc exec test-disk-pod -- cat /data/x  # 输出 hello
oc delete pod/test-disk-pod pvc/test-disk
```

详细检查清单见 [`docs/validation-checklist.md`](docs/validation-checklist.md)。

---

## Phase 5 — Day-2 操作

### 扩缩容 Worker

```sh
# 准备 MachineDeployment（一次性，按 examples 改占位符）
cp ../openshift-capi-alicloud/examples/capi-machinedeployment.yaml my-workers.yaml
# 编辑 CLUSTER_NAME、NAMESPACE、REGION_ID、VPC_ID、VSWITCH_ID、WORKER_SG_ID、RAM_ROLE_NAME、IMAGE_ID
oc apply -f my-workers.yaml

# 扩容
oc scale machinedeployment <name> --replicas=5 -n openshift-cluster-api
```

### 创建 VolumeSnapshot

```sh
# 启用 snapshot（默认关闭）：
oc patch alibabacloudcsidriver cluster --type merge -p \
  '{"spec":{"disk":{"snapshot":{"enabled":true}}}}'

# 等 VolumeSnapshotClass 创建
oc get volumesnapshotclass

# 拍快照
cat <<'EOF' | oc apply -f -
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: my-snap
spec:
  source:
    persistentVolumeClaimName: test-disk
  volumeSnapshotClassName: alibaba-cloud-disk-snapclass
EOF
```

### 跑一次集群备份

```sh
oc create -n openshift-adp -f - <<'EOF'
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: full-cluster-backup-1
spec:
  storageLocation: alibaba-oss
  includedNamespaces: ['*']
EOF

oc get backup -n openshift-adp -w
```

---

## 测试预算控制

如果你只是验证而不是上线，按这套配置跑：

### Compact 3-node 拓扑（推荐测试配置）

```
3× master（4C/16G）+ 0 worker + 1 SLB + 1 NAT + 3× 100GB ESSD
≈ ¥4/h ≈ ¥80-100/天（按量付费）
```

**何时合适**：
- ✅ 验证 CSI Operator + CCM + CAPI provider 的完整链路
- ✅ 验证 PVC 挂载、Service Type=LoadBalancer、节点 ProviderID
- ✅ 跑 OPCT 的 Kubernetes + 部分 OpenShift conformance 套件
- ❌ 不适合：跨多 AZ 容灾验证、独立 worker 池压力测试

### 关键省钱招式

1. **按量付费**，不要订阅式（subscription）
2. **测试用 Spot 实例做临时 worker**（CAPI 扩容时）：
   ```yaml
   # AlibabaCloudMachineTemplate.spec.template.spec 加：
   spotStrategy: SpotAsPriceGo  # 当前 SDK 还需扩展支持
   ```
   现成的 spot 实例价 = 按量价的 10-30%
3. **预算告警**（一次性配置）：
   ```sh
   aliyun bssopenapi CreateBudget --BudgetType DAILY --BudgetAmount 50
   ```
4. **资源组隔离 + 标签**，方便整批清理：
   ```sh
   aliyun resourcemanager CreateResourceGroup --Name openshift-test
   ```
5. **测试结束立即销毁**（见下文"销毁集群"）

### 三种典型成本

| 场景 | 配置 | 时费 | 4 小时 | 一天 |
|---|---|---|---|---|
| Compact 3 节点（推荐）| 3× g7.xlarge + SLB + NAT | ¥4 | ¥16 | ¥80-100 |
| 标准 3+2 | 3× g7.xlarge + 2× g7.large | ¥6 | ¥24 | ¥120-150 |
| 完整 OPCT 验证 | 3+2 跨多 AZ + 24h 跑 OPCT | ¥6 | — | ¥150 |

新阿里云账号通常有 ¥300-1000 抵扣金，足够覆盖 1-2 次完整验证。

---

## 参数速查

所有参数集中在 `ansible/group_vars/all.yml`（从 `all.yml.example` 复制过来）。
下面是完整的参数说明和典型取值。

### 集群身份

| 参数 | 示例值 | 说明 |
|---|---|---|
| `cluster_name` | `cluster1` | 集群短名，用于 DNS / 资源名 / 标签；一旦定下来不要改 |
| `base_domain` | `example.local` | 集群基础域名；`api.<name>.<domain>` 会被 PrivateZone 解析 |
| `openshift_version` | `"4.20"` | 主版本号（字符串）；用于 Assisted Installer API 筛选版本 |

### 阿里云基础设施

| 参数 | 示例值 | 说明 |
|---|---|---|
| `region` | `cn-wulanchabu` | 目标 Region；整个集群所有资源都在同一个 Region |
| `zone` | `cn-wulanchabu-a` | 主可用区；Rendezvous（第一台 master）和 VSwitch1 在这里 |
| `zone2` | `cn-wulanchabu-b` | 备可用区；master-2/3 和 VSwitch2 在这里 |
| `oss_bucket` | `openshift-iso-my-test` | OSS 存储桶名（**全局唯一**）；存放 Discovery ISO |
| `aliyun_profile` | `openshift-test` | `aliyun configure` 里的 profile 名；用于调用阿里云 API |

### 计算规格

| 参数 | 默认值 | 说明 |
|---|---|---|
| `control_plane_count` | `3` | 控制平面节点数，必须是奇数（3 或 5）|
| `control_plane_type` | `ecs.g7.xlarge` | 控制平面实例类型（4C/16G）；生产建议 `ecs.g7.4xlarge` |
| `compute_count` | `0` | Worker 节点数；`0` = compact 模式，master 也跑工作负载 |
| `compute_type` | `ecs.g7.xlarge` | Worker 实例类型 |
| `system_disk_category` | `cloud_essd` | 系统盘类型；`cloud_essd` 性能最好 |
| `system_disk_size` | `120` | 系统盘大小（GiB）；RHCOS 最小需要 100 |

> **Compact 省钱配置**：`control_plane_count: 3`，`compute_count: 0`，`control_plane_type: ecs.g7.xlarge`
> 约 ¥4/h，验证完即销毁。

### 网络

| 参数 | 默认值 | 说明 |
|---|---|---|
| `vpc_cidr` | `10.0.0.0/16` | VPC 地址段 |
| `private_subnet_cidr` | `10.0.16.0/20` | 私有子网 1（zone，master-1 在此）|
| `private_subnet_cidr2` | `10.0.32.0/20` | 私有子网 2（zone2，master-2/3 在此）|
| `public_subnet_cidr` | `10.0.0.0/20` | 公网子网（NAT / 跳板机 / EIP 在此）|
| `rendezvous_ip` | `10.0.16.5` | Rendezvous 节点（master-1）固定 IP；必须在 `private_subnet_cidr` 内 |
| `ingress_vip` | `10.0.16.6` | Ingress VIP；Assisted Installer 用于绑定 ingress SLB；必须在同一子网 |

### 跳板机（可选）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `enable_jump_host` | `false` | `true` = Phase 03 创建跳板机并预装 `aliyun` CLI + `oc` + kubeconfig |
| `jump_host_type` | `ecs.t6-c1m1.large` | 跳板机实例类型；最低配即可（1C/1G）|

> 跳板机用于：从公网 SSH 进 VPC、在 VPC 内运行 `oc` 命令、查看集群状态。
> 不需要的话保持 `false`，节省费用（~¥0.1/h）。

### 本地文件路径

| 参数 | 默认值 | 说明 |
|---|---|---|
| `pull_secret_file` | `~/.openshift/pull-secret.json` | Red Hat Pull Secret 文件路径 |
| `ssh_pub_key_file` | `~/.ssh/openshift_ed25519.pub` | 注入到节点的 SSH 公钥 |
| `ssh_priv_key_file` | `~/.ssh/openshift_ed25519` | 用于 SCP / 跳板机登录的私钥 |
| `offline_token_file` | `~/.openshift/offline-token` | Red Hat Offline Token（Assisted API 认证）|
| `output_dir` | `~/openshift-install/<cluster_name>` | kubeconfig / 密码 / ISO 的输出目录 |

### 销毁行为（Teardown）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `teardown_from` | `3` | 销毁范围（见下表）|
| `keep_image` | `true` | `true` = 保留 ECS 自定义镜像（重建栈时省去 ~30 分钟重新上传）|
| `teardown_confirmed` | `false` | `true` = 跳过"请输入 yes"确认提示，适合 CI/CD |

**`teardown_from` 取值说明：**

| 值 | 销毁范围 | `keep_image=true`（默认）后续步骤 | `keep_image=false` 后续步骤 |
|---|---|---|---|
| `1` | AI 集群 + infra-env 记录 | Phase 01 → 02 → 03 → 04（infra-env 已删，ECS image 凭证失效，必须完整重建）| 同左 |
| `2` | 上面 + ECS 镜像（受 `keep_image` 控制）| 同 `1`（keep_image=true 时镜像未删，效果与 `1` 相同）| Phase 01 → 02 → 03 → 04 |
| `3` | 上面 + ROS 栈（VPC / ECS / SLB / NAT / EIP）| **Phase 03 → 04**（AI 记录 + image 保留，直接重建栈，节省 ~30 min 重新导入）| Phase 01 → 02 → 03 → 04 |
| `4` | 上面 + 先清理应用层（LB SVC + PVC）| Phase 03 → 04 | Phase 01 → 02 → 03 → 04 |

> **`keep_image=true` 的正确含义**：ECS image 里 bake-in 了 infra-env 的注册 token。删 infra-env 而保留 image 会让 image 变废品（节点无法向新 infra-env 注册）。所以 `keep_image=true` 时 AI 记录（cluster + infra-env）也一起保留，下次 Phase 03 建新栈时节点用同一 image 和同一 infra-env 重新注册，Phase 04 auto-reset 处理集群状态。
>
> **Phase 04 auto-reset**：绝大多数安装失败（error/cancelled/installing 状态）直接重跑 Phase 04 即可，不需要 teardown。teardown 只在需要重建基础设施时才用。

### 常用命令组合

```sh
# 全自动端到端安装（所有参数从 all.yml 读取）
cd ansible && ansible-playbook playbooks/site.yml

# ── 安装失败时的恢复（最常用）──────────────────────────────────────────
# 安装中途失败（error/cancelled/installing-pending-user-action 等状态）
# 直接重跑 Phase 04，auto-reset 自动处理，不需要 teardown
ansible-playbook playbooks/07-install-cluster.yml

# ── 重建栈但保留 image（节省 ~30 min 重新导入，最常用的 teardown）────
# keep_image=true（默认）：AI 记录 + image 同时保留（凭证配对，缺一不可）
# 删栈 → 重建栈 → Phase 04 auto-reset 重装
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_from=3
ansible-playbook playbooks/03-create-stack-LEGACY.yml
ansible-playbook playbooks/07-install-cluster.yml

# ── 完整销毁（删 AI + image + 栈），需要全部重新跑 ────────────────────
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_from=3 -e keep_image=false
ansible-playbook playbooks/01-prepare-iso.yml
ansible-playbook playbooks/02-import-image.yml
ansible-playbook playbooks/03-create-stack-LEGACY.yml
ansible-playbook playbooks/07-install-cluster.yml

# ── 集群已安装，含应用层清理，然后完整重建 ──────────────────────────
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_from=4 -e keep_image=false
# （然后同上，Phase 01 → 04）

# ── CI/CD 无交互完整销毁 ─────────────────────────────────────────────
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_confirmed=true -e teardown_from=3 -e keep_image=false

# ── 仅清 AI 记录（teardown_from=1）──────────────────────────────────
# 删掉 AI cluster + infra-env；ECS 镜像 + 栈不动
# 注意：infra-env 已删，ECS image 凭证失效，必须完整重建
# 用途：清理孤儿 AI 记录，之后跑完整流程 01 → 02 → 03 → 04
ansible-playbook playbooks/99-teardown-LEGACY.yml -e teardown_from=1
```

---

## 销毁集群

**推荐**：用自动化（一条命令搞定应用层清理 + 删栈 + 9 类资源孤儿扫描）：

```sh
# Ansible
ansible-playbook ansible/playbooks/99-teardown-LEGACY.yml

# 或 shell
./scripts/99-teardown.sh
```

手动等价：

```sh
# 1. 删除 OpenShift 内的工作负载（让 CCM/CSI 清理 SLB + 磁盘 + 快照）
oc delete pvc --all -A
oc delete svc --all -A --field-selector spec.type=LoadBalancer

# 2. 等阿里云资源真正释放（SLB 删除是异步的，~3 分钟）
sleep 180

# 3. ROS 删除栈
aliyun ros DeleteStack --StackId <stack-id>

# 4. 用 cluster tag 扫描孤儿（详见 docs/test-walkthrough.md G.3）
TAG_KEY="kubernetes.io/cluster/cluster1"
for cmd in 'ecs DescribeInstances' 'ecs DescribeDisks' 'ecs DescribeSecurityGroups' \
           'vpc DescribeEipAddresses' 'vpc DescribeNatGateways' 'vpc DescribeVSwitches' \
           'vpc DescribeVpcs' 'pvtz DescribeZones'; do
  echo "=== $cmd ==="
  aliyun $cmd --RegionId cn-wulanchabu --Tag.1.Key "$TAG_KEY" --Tag.1.Value owned 2>/dev/null | jq '.[][] | length' 2>/dev/null
done
# SLB 单独（用 TagKey/TagValue，不是 Tag.1.Key/Value）
aliyun slb DescribeLoadBalancers --RegionId cn-wulanchabu --Tag.1.TagKey "$TAG_KEY" --Tag.1.TagValue owned
```

> **重要**：直接 ROS 删栈而不先清理工作负载会留下"孤儿" SLB 和磁盘，
> 需要按 tag 手动清理才能避免持续计费。ROS 模板已经给 17 个顶级资源都打了
> `kubernetes.io/cluster/${ClusterName}=owned` 标签，孤儿扫描可靠覆盖。

---

## Disconnected install via private mirror (China regions)

跨境拉 `quay.io` 在 `cn-*` region 常不稳定。设 `mirror_enabled: true` 后 Phase 03
会在集群 VPC 内额外建一台 mirror ECS（静态私网 IP `10.0.16.4`），节点全部走内网拉镜像。

**3 步速览**：

```bash
# Step 1: 境外主机构建 tarball 上传 OSS（一次性，~1-2 小时）
OFFLINE_TOKEN_FILE=/path/to/offline-token \
OSS_BUCKET=openshift-iso-samzhu-test \
CLUSTER_NAME=aliocp1 \
OPENSHIFT_VERSION=4.20 \
    ./scripts/build-mirror-tarball.sh

# Step 2: 在 ansible 机器开 mirror
echo 'mirror_enabled: true' >> ansible/group_vars/all.yml
echo 'mirror_oss_object: "mirror-tarballs/aliocp1-4.20.tar"' >> ansible/group_vars/all.yml

# Step 3: 正常跑 + 验证
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-stack-LEGACY.yml    # 多 ~30 min（mirror cloud-init）
ansible-playbook ansible/playbooks/05-verify-mirror.yml      # Phase 04 前 sanity check
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

**完整文档（架构 / 成本 / 配置参考 / 扩展 operator + CCM / 详细故障排查）**：  
📘 **[`docs/MIRROR.md`](docs/MIRROR.md)**

辅助 playbook（按需用）：

| Playbook | 用途 |
|---------|------|
| `mirror-rebuild.yml` | 刷新 mirror 镜像（不动 cluster），加 operator / 升级版本时用 |
| `05-verify-mirror.yml` | 健康检查 + 镜像存在验证 |

---

## 故障排查

| 现象 | 检查 |
|---|---|
| 节点 NotReady | `oc describe node` → 看 cloud-taint；CCM 日志 |
| Pod CrashLoopBackOff | OVN MTU 没设？参看 `custom_manifests/00-ovn-mtu.yaml` |
| PVC Pending | CSI 控制器日志：`oc logs -n alibaba-cloud-csi-operator-system -l app=csi-provisioner-disk` |
| SLB 没自动创建 | CCM 健康？Service 是否 `type: LoadBalancer`？|
| CAPI Machine 卡 Provisioning | `oc logs -n capa-system deploy/capa-controller-manager`；RAM Role 权限？ |

---

## 下一步

- 跑 OPCT 合规性测试（Red Hat 认证关键）：`opct run --watch`
- 启用 OpenShift Virtualization（VM 工作负载，需要 NAS CSI）
- 加 OpenShift Logging + 监控持久化（PVC backed）
- 申请 Red Hat Connect partner certification
