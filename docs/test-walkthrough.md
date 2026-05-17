# 完整测试操作手册（Compact 3-node）

从零到运行 OpenShift 集群的**手动操作步骤**，针对 Compact 3-node 测试场景
（成本最优，约 ¥80-100/天）。两条安装路径都覆盖：

- **路径 A — Assisted Installer**：通过 Red Hat 网页 UI 安装（推荐第一次）
- **路径 B — Agent-based Installer**：本地命令行安装（自动化友好）

> **🤖 想直接全自动跑？** 用 [`scripts/`](../scripts/README.md)：
> ```sh
> cp scripts/config.sh.example scripts/config.sh && vi scripts/config.sh
> ./scripts/all.sh   # 跑完 01-04，约 90 分钟
> ```
> 本文档详细解释每一步**为什么**和**手动该怎么点**，仅在出错排查或想理解机制时阅读。
> 6 个脚本各自对应这里的一个 Phase：
> `01-prepare-iso.sh` (A.1-3) / `02-import-image.sh` (A.2-4) /
> `03-create-stack.sh` (B) / `04-install-cluster.sh` (C) /
> `05-deploy-post-install.sh` (D，跳板上跑) / `99-teardown.sh` (G)

---

## 目录

- [Day 0：一次性准备](#day-0一次性准备)（~ 30 分钟，只做一次）
- [Phase A：准备 OpenShift 引导镜像](#phase-a准备-openshift-引导镜像)（30-60 分钟）
- [Phase B：创建云基础设施（ROS）](#phase-b创建云基础设施ros)（15 分钟）
- [Phase C：安装 OpenShift](#phase-c安装-openshift)（45-90 分钟）
- [Phase D：应用 post-install 组件](#phase-d应用-post-install-组件)（10 分钟）
- [Phase E：功能验证](#phase-e功能验证)（30 分钟）
- [Phase F：（可选）OPCT 合规性测试](#phase-f可选opct-合规性测试)（4-12 小时）
- [Phase G：销毁和成本检查](#phase-g销毁和成本检查)（10 分钟）

**总计：单次完整验证 4-6 小时，¥80-100 费用**

---

## Day 0：一次性准备

### 0.1 阿里云账号

注册账号（如已有跳过）：
1. 访问 [aliyun.com](https://www.aliyun.com/) 注册
2. **完成实名认证**（个人/企业，否则无法创建 ECS）
3. 充值 ¥500 起（保证 6 小时测试预算）

新账号检查 [免费试用资源](https://free.aliyun.com/)：
- ECS 试用券（通常 3 个月内有效）
- VPC、SLB 通常没有免费额度

### 0.2 准备 RAM 子用户

不要用主账号操作，必须用 RAM 子用户。两种情况：

#### 情况 A：已有 RAM 子用户（你属于这种）

先确认它的权限是否够用：

1. [RAM 控制台 → Users](https://ram.console.aliyun.com/users) → 点击你的子用户名
2. **Permissions** 标签 → 列出该用户已有的系统策略
3. 对照下面这个清单，**缺哪个加哪个**：

   | 系统策略名 | 用途 |
   |---|---|
   | `AliyunECSFullAccess` | 创建 ECS、安全组、自定义镜像 |
   | `AliyunVPCFullAccess` | 创建 VPC、VSwitch、NAT、EIP |
   | `AliyunSLBFullAccess` | 创建 API SLB |
   | `AliyunOSSFullAccess` | 上传 ISO 到 OSS |
   | `AliyunRAMFullAccess` | 创建 NodeRamRole（Instance Principal）|
   | `AliyunPVTZFullAccess` | 创建 PrivateZone（DNS）|
   | `AliyunROSFullAccess` | 创建 ROS 栈本身 |

   或者最简单：直接给 `AdministratorAccess`，覆盖一切（**仅测试场景推荐**）。

   > 资源组（Resource Group）功能没有单独的公开 FullAccess 策略——
   > 要么用 `AdministratorAccess`（已包含），要么跳过 0.4 节（资源组是
   > 可选的，仅方便清理；用资源标签 `cluster=test-xxx` 一样能批量过滤）。

   补权限：**Permissions** → **Grant Permission** → 搜索策略名 → 勾选 → **OK**。

4. 选择 CLI 认证方式：

   阿里云推荐**优先用临时凭证而非长期 AccessKey**。三种方式按推荐度排：

   | 方式 | 凭证寿命 | 适合 | 设置复杂度 |
   |---|---|---|---|
   | **A. CloudShell**（推荐）| 会话级 | 一次性测试，不碰本地凭证 | 零 |
   | **B. RAM 用户 + STS 临时令牌** | 1-12 h | 本地操作但不留长期 AK | 低 |
   | **C. RAM 用户 + 长期 AccessKey** | 永久（手动轮转）| CI/CD 自动化 | 低 |

   **本测试 walkthrough 场景下推荐 A 或 B**：

   - **A. CloudShell**：浏览器里直接运行 aliyun CLI，凭证由阿里云自动注入，
     关闭浏览器即失效。但**本机的 oc / openshift-install 工具就够不着 OpenShift
     集群的 API**（除非把 kubeconfig 也搬进 CloudShell），所以适合只跑
     Phase A/B/G 这些纯阿里云操作的步骤。
     入口：[shell.aliyun.com](https://shell.aliyun.com/) 或控制台右上角 `>_` 图标。

   - **B. STS 临时令牌**：本地 aliyun CLI 用临时令牌，不持久化 AK：
     ```sh
     # 浏览器里登录 RAM 用户 → 个人头像 → AccessKey → "获取临时凭证"
     # 或者用主账号 AssumeRole 拿临时令牌
     aliyun configure --mode StsToken --profile openshift-test
     # 粘贴 AccessKeyId / AccessKeySecret / StsToken（三个）
     # 默认有效期 1-12 小时
     ```
     测试结束令牌自动失效，无需手动清理。

   - **C. 长期 AccessKey**：传统方式，安全性最弱但兼容性最好：
     - **Authentication** 标签 → **AccessKeys** 子标签
     - 已有现役 AK 且 SK 还在：直接用
     - SK 丢了：Disable 旧的 → Create AccessKey 建新的（**SK 仅显示一次**）
     - 一个都没有：Create AccessKey

   > 后续命令都假设你已经把凭证配进了 `aliyun-cli` 的 `openshift-test` profile，
   > 不区分用 A/B/C 哪种方式获取的凭证。

5. 用 CLI 自检权限是否到位：
   ```sh
   # 用你的 AK/SK 配置 aliyun CLI（见 0.3 节）
   # 然后跑这几条 dry-run 命令验证权限：
   aliyun ecs DescribeRegions --RegionId cn-wulanchabu >/dev/null && echo "ECS OK"
   aliyun vpc DescribeVpcs --RegionId cn-wulanchabu >/dev/null && echo "VPC OK"
   aliyun slb DescribeLoadBalancers --RegionId cn-wulanchabu >/dev/null && echo "SLB OK"
   aliyun ram ListRoles >/dev/null && echo "RAM OK"
   aliyun ros DescribeRegions >/dev/null && echo "ROS OK"
   aliyun pvtz DescribeZones >/dev/null && echo "PVTZ OK"
   aliyun oss ls --region cn-wulanchabu >/dev/null && echo "OSS OK"
   ```
   全部输出 "OK" 即可往下走。任一失败 → 缺对应的 FullAccess 策略，回步骤 3 补。

#### 情况 B：还没有 RAM 子用户

按下面步骤创建：

1. [RAM 控制台](https://ram.console.aliyun.com/) → **Identities** → **Users** → **Create User**
2. **Logon Name**: `openshift-test`
3. **Access Mode**: 勾选 **Console Access** + **Programmatic Access**
4. 创建后**立即记录** AccessKey ID 和 Secret（仅显示一次）
5. 切到 **Permissions** 标签 → **Grant Permission**
6. 加上情况 A 表格中的所有系统策略，或者直接 `AdministratorAccess`

### 0.3 安装并配置 aliyun CLI（必做）

```sh
# 安装 aliyun CLI（macOS）
brew install aliyun-cli

# 配置（用 0.2 步骤拿到的 RAM 用户 AK/SK）
# 推荐用独立 profile，不影响你已有的默认 profile：
aliyun configure --profile openshift-test
# Region: cn-wulanchabu
# Access Key Id: <你的子用户 AK>
# Access Key Secret: <你的子用户 SK>

# 后续命令默认你已设置环境变量：
export ALIBABA_CLOUD_PROFILE=openshift-test
```

### 0.4 创建资源组（可选，方便统一清理）

> 跳过这一步不影响后续。改用资源标签 `cluster=test-${ClusterName}` 一样能批量
> 过滤删除。下面只在你拥有 `AdministratorAccess` 时可行（资源组创建权限不在
> ECS/VPC/SLB 等 FullAccess 策略里）。

```sh
aliyun resourcemanager CreateResourceGroup \
  --Name openshift-test \
  --DisplayName "OpenShift testing"

# 返回 JSON 含 ResourceGroupId（rg-xxxxxxxxxx），保存下来
export RG_ID=rg-xxxxxxxxxx
```

### 0.5 配置预算告警

```sh
# 设置每日 ¥50 预算告警（避免意外漏算）
# 注：BSS API 在 web 控制台操作更直观：
# https://usercenter.console.aliyun.com/#/manage/budget
```

或网页：[费用中心 → 预算管理](https://usercenter.console.aliyun.com/#/manage/budget)：
- 创建预算 → 类型 "每日" → 金额 ¥50 → 通知 80%/100% → 邮箱

### 0.6 本地工具

```sh
# macOS
brew install kubectl openshift-cli kustomize jq yq

# 下载 openshift-install
curl -L https://mirror.openshift.com/pub/openshift-v4/clients/ocp/latest/openshift-install-mac-arm64.tar.gz | tar -xz
sudo mv openshift-install /usr/local/bin/

# 验证
oc version --client
openshift-install version

# 仅 Agent-based 路径需要 Butane（生成 MachineConfig）
brew install butane
```

### 0.7 Red Hat 账号

1. 注册 [console.redhat.com](https://console.redhat.com)（免费）
2. **Downloads** → **Pull secret** → **Copy** 或 **Download**（保存到 `~/.openshift/pull-secret.json`）
3. 准备 SSH 公钥：
   ```sh
   # 如果没有
   ssh-keygen -t ed25519 -C "openshift-debug" -f ~/.ssh/openshift_ed25519
   cat ~/.ssh/openshift_ed25519.pub
   ```

### 0.8 Clone 三个仓库（同级目录）

```sh
mkdir -p ~/openshift-alibaba && cd ~/openshift-alibaba
git clone https://github.com/SammZhu/alibaba-openshift.git
git clone https://github.com/SammZhu/alibaba-cloud-csi-operator.git
git clone https://github.com/SammZhu/openshift-capi-alicloud.git
```

---

## Phase A：准备 OpenShift 引导镜像

> 详细背景见 [`docs/boot-image-import.md`](boot-image-import.md)

### 路径 A1 — Assisted Installer（推荐）

#### 步骤 1：在 Red Hat Console 创建集群获取 ISO

1. 浏览器打开 [console.redhat.com/openshift](https://console.redhat.com/openshift)
2. **Clusters** → **Create cluster** → **Datacenter** tab
3. 选 **Bare Metal (x86_64)** → **Create cluster**
4. 填写：
   - **Cluster name**: `cluster1`（任意，记下来）
   - **Base domain**: `example.local`（测试用，无需真实域名）
   - **OpenShift version**: **4.20**（推荐 EUS，4.20 标准 14 月 + EUS 12 月 + ELS 24 月 ≈ 50 月支持；4.21 是奇数版本只 14 月）
   - **CPU architecture**: x86_64
5. **Next** → **Operators** 页面：全部跳过 → **Next**
6. **Host discovery** 页面 → **Add hosts** → 选 **Minimal image file**
7. SSH public key: 粘贴 `~/.ssh/openshift_ed25519.pub` 内容
8. **Generate Discovery ISO**
9. **Download Discovery ISO** → 保存到本地，例如 `~/Downloads/discovery_image_cluster1.iso`

> ⚠️ **保持这个浏览器标签页打开**——稍后还要回来

#### 步骤 2：上传 ISO 到 OSS

```sh
# 创建 Bucket（名字必须全局唯一，加你的标识）
aliyun oss mb oss://openshift-iso-samchoo-test --region cn-wulanchabu

# 上传 ISO
aliyun oss cp ~/Downloads/discovery_image_cluster1.iso \
  oss://openshift-iso-samchoo-test/discovery.iso \
  --region cn-wulanchabu

# 验证
aliyun oss ls oss://openshift-iso-samchoo-test/ --region cn-wulanchabu
# 应见 100MB 左右的 discovery.iso
```

### 路径 B1 — Agent-based Installer

#### 步骤 1：在本地生成 ISO

需要先决定参数（这些后面 ROS 也要用）：

```sh
export CLUSTER_NAME=cluster1
export BASE_DOMAIN=example.local
export REGION=cn-wulanchabu
export RENDEZVOUS_IP=10.0.16.5  # 必须在 PrivateSubnetCidr 10.0.16.0/20 内
export PULL_SECRET=$(cat ~/.openshift/pull-secret.json)
export SSH_KEY=$(cat ~/.ssh/openshift_ed25519.pub)

# 创建工作目录
mkdir -p ~/openshift-install/$CLUSTER_NAME/openshift
cd ~/openshift-install/$CLUSTER_NAME

# 写 install-config.yaml（注意 ComputeCount=0）
cat > install-config.yaml <<EOF
apiVersion: v1
metadata:
  name: $CLUSTER_NAME
baseDomain: $BASE_DOMAIN
networking:
  clusterNetwork:
    - cidr: 10.128.0.0/14
      hostPrefix: 23
  networkType: OVNKubernetes
  machineNetwork:
    - cidr: 10.0.0.0/16
  serviceNetwork:
    - 172.30.0.0/16
compute:
  - architecture: amd64
    hyperthreading: Enabled
    name: worker
    replicas: 0          # ← Compact 3-node 关键
controlPlane:
  architecture: amd64
  hyperthreading: Enabled
  name: master
  replicas: 3
platform:
  external:
    platformName: AlibabaCloud
    cloudControllerManager: External
pullSecret: '$PULL_SECRET'
sshKey: '$SSH_KEY'
EOF

# 写 agent-config.yaml
cat > agent-config.yaml <<EOF
apiVersion: v1alpha1
kind: AgentConfig
metadata:
  name: $CLUSTER_NAME
rendezvousIP: $RENDEZVOUS_IP
hosts: []
EOF

# 拷贝 install-time custom manifests 到 openshift/
cp ~/openshift-alibaba/alibaba-openshift/custom_manifests/00-ovn-mtu.yaml openshift/
cp ~/openshift-alibaba/alibaba-openshift/custom_manifests/01-alibaba-ccm.yaml openshift/
cp ~/openshift-alibaba/alibaba-openshift/custom_manifests/03-machineconfig-providerid.yaml openshift/
# alibaba-ccm-config.yaml 还没有——ROS 跑完才能拿到

# 生成 ISO
openshift-install agent create image --dir .
ls -lh agent.x86_64.iso
# 约 1 GB
```

#### 步骤 2：上传到 OSS（同 A 路径）

```sh
aliyun oss mb oss://openshift-iso-samchoo-test --region cn-wulanchabu
aliyun oss cp agent.x86_64.iso \
  oss://openshift-iso-samchoo-test/agent.iso \
  --region cn-wulanchabu
```

### 步骤 3：导入 ISO 为 ECS 自定义镜像（两个路径相同）

```sh
# Path A 用 discovery.iso；Path B 用 agent.iso
ISO_OBJECT=discovery.iso  # 或 agent.iso

aliyun ecs ImportImage \
  --RegionId cn-wulanchabu \
  --ImageName "openshift-${CLUSTER_NAME:-cluster1}-iso" \
  --OSType Linux \
  --Platform Others_Linux \
  --Architecture x86_64 \
  --DiskDeviceMapping.1.Format ISO \
  --DiskDeviceMapping.1.OSSBucket openshift-iso-samchoo-test \
  --DiskDeviceMapping.1.OSSObject $ISO_OBJECT
```

返回 JSON 含 `ImageId`。**记下来**，例如 `m-bp1abc...`。

> 如果首次导入，阿里云会提示授权 ECS 访问 OSS。点 "Authorize"，自动创建
> `AliyunECSImageImportDefaultRole` RAM 角色。

### 步骤 4：等导入完成（15-30 分钟）

```sh
export IMAGE_ID=m-bp1xxxxxxxxxxxxxxx

# 轮询状态
while true; do
  STATUS=$(aliyun ecs DescribeImages --RegionId cn-wulanchabu --ImageId $IMAGE_ID \
    --query 'Images.Image[0].Status' --output text)
  echo "$(date '+%H:%M:%S') Status: $STATUS"
  [ "$STATUS" = "Available" ] && break
  sleep 60
done
```

或网页：[ECS Console → Images → Custom Image](https://ecs.console.aliyun.com/image/region/cn-wulanchabu/imageList)

---

## Phase B：创建云基础设施（ROS）

### B.1 提交 ROS 栈

1. 打开 [ROS 控制台](https://ros.console.aliyun.com/cn-wulanchabu/stacks/create)
2. **Choose template** → **Use existing template** → **Upload template file**
3. 选择本地的 `~/openshift-alibaba/alibaba-openshift/ros-templates/create-cluster.yaml`
4. **Next**：**Stack Information**
   - **Stack Name**: `openshift-cluster1`
   - **Specify the rollback policy**: Disable（测试时禁用，方便看报错）

5. **Configure Parameters**：

   | 参数 | 测试值（compact 3-node）|
   |---|---|
   | `ClusterName` | `cluster1` |
   | `BaseDomain` | `example.local` |
   | `Region` | `cn-wulanchabu` |
   | `ZoneId` | `cn-wulanchabu-a`（任选） |
   | `VpcCidr` | `10.0.0.0/16` |
   | `PrivateSubnetCidr` | `10.0.16.0/20` |
   | `PublicSubnetCidr` | `10.0.0.0/20` |
   | `ControlPlaneCount` | `3` |
   | `ComputeCount` | **`0`** ← compact |
   | `ControlPlaneInstanceType` | `ecs.g7.xlarge` ← 4C/16G |
   | `ComputeInstanceType` | `ecs.g7.xlarge`（不会用到，留默认）|
   | `SystemDiskCategory` | `cloud_essd` |
   | `SystemDiskSize` | `120` |
   | `InstallationMethod` | `Assisted` 或 `Agent-based`（与 Phase A 一致）|
   | `ImageId` | 上一步得到的 `m-bp1xxx...` |
   | `RendezvousIp` | `10.0.16.5`（仅 Agent-based 用）|
   | **`EnableJumpHost`** | **`true`**（本地操作必填，详见 B.1.1）|
   | **`SshPublicKey`** | **粘贴你 `~/.ssh/openshift_ed25519.pub` 内容** |
   | `JumpHostInstanceType` | `ecs.t6-c1m1.large`（1C/1G burst，默认）|

6. **Next** → **Confirm** → 勾选两个确认框 → **Create**

### B.1.1 为什么必须开 EnableJumpHost

集群的 API SLB 是 **VPC 内网 IP**，PrivateZone 解析也只在 VPC 内有效——
你的本地 RHEL VM **既 ping 不到 API SLB，也解析不了 `api.cluster1.example.local`**。

`EnableJumpHost=true` 会额外创建：

- 1× `ecs.t6-c1m1.large`（1C/1G burst）+ 20 GB ESSD
- 1× 公网 EIP（1 Mbps 按流量计费）
- 自动开机脚本，预装：oc、kubectl、openshift-install、kustomize、aliyun CLI、jq、git
- 自动 clone 三个工程仓库到 `/root/openshift-alibaba/`
- 自动授权你的 SSH 公钥给 root

**费用增量约 ¥2/天**。Compact 3-node 总日费 ≈ ¥80-100 + 跳板 ≈ ¥85/天。

> **不开跳板能不能跑？** 能装集群（PrivateZone 内部解析自动），但你拿到
> kubeconfig 后**没法本地用 oc 操作集群**，因为 API SLB 在 VPC 内网。
> 仅看 Red Hat Console 显示"装完了"的话可以跳过这个选项。

### B.2 等栈完成（10-15 分钟）

```sh
# 命令行查询进度
export STACK_ID=stk-xxxxxxxxxxxxx  # 从控制台 Outputs 拿

aliyun ros GetStack --StackId $STACK_ID --query 'Status' --output text
# CREATE_IN_PROGRESS → CREATE_COMPLETE
```

### B.3 保存关键 Outputs

栈完成后，**Outputs** 标签页有这些值，**全部记下来**：

```sh
# 命令行获取所有 outputs
aliyun ros GetStack --StackId $STACK_ID --query 'Outputs' | jq
```

关键值：

| Output Key | 用途 |
|---|---|
| `InstallConfig` | install-config.yaml 内容（Assisted 路径粘到 Console；Agent-based 已用过）|
| `DynamicCustomManifest` | 保存为 `alibaba-ccm-config.yaml`，Phase C 用 |
| `ApiSLBIp` | API Server 内网 IP（如 `10.0.16.10`）|
| `BootstrapPublicIp` | 仅 Assisted 用，调试 SSH 用 |
| `VpcId` / `PrivateVSwitch` / `WorkerSecurityGroup` | CAPI MachineDeployment 用 |
| `NodeRamRoleName` | CAPI 用 |

```sh
# 把 DynamicCustomManifest 保存下来备用
aliyun ros GetStack --StackId $STACK_ID \
  --query "Outputs[?OutputKey=='DynamicCustomManifest'].OutputValue" \
  --output text > ~/openshift-install/alibaba-ccm-config.yaml
```

---

## Phase C：安装 OpenShift

### 路径 A2 — Assisted Installer（接 Phase A1）

#### 步骤 1：回到 Red Hat Console

之前打开的标签页（如果关了重新打开 cluster 详情）。

#### 步骤 2：等节点上线

ROS 创建的 ECS（3 master + 1 bootstrap）会自动从 ISO 启动 Discovery agent，
agent 会自动联系 Red Hat Console 上报。

在 Console 的 **Host discovery** 页面：
- 等 5-10 分钟，应该看到 4 台主机出现（或 3 台，bootstrap 可能不出现）
- 每台主机显示 hostname、CPU、内存、磁盘信息

#### 步骤 3：分配角色

在主机列表的每行的 Role 下拉菜单：
- master-1, master-2, master-3 → 选 **Control plane**
- bootstrap → 选 **Auto-assign** 或忽略（Assisted 自动处理）

> Compact 3-node：bootstrap 是临时节点，安装完成后销毁，3 个 master 才是最终节点。

#### 步骤 4：上传 Custom Manifests

**Networking** 页面：保持默认（machine network 已自动检测）

**Custom manifests** 页面（如未自动跳转，左边导航点）→ **Add custom manifest**：

上传以下 4 个文件，逐个添加：

| Folder | File | 来源 |
|---|---|---|
| `manifests` | `00-ovn-mtu.yaml` | `~/openshift-alibaba/alibaba-openshift/custom_manifests/00-ovn-mtu.yaml` |
| `manifests` | `01-alibaba-ccm.yaml` | 同上目录 |
| `openshift` | `03-machineconfig-providerid.yaml` | 同上目录 |
| `manifests` | `alibaba-ccm-config.yaml` | `~/openshift-install/alibaba-ccm-config.yaml`（Phase B 保存的）|

> 注意 Folder 选项：MachineConfig 必须放 `openshift/`，其他放 `manifests/`。

#### 步骤 5：开始安装

**Review and create** → 检查无误 → **Install cluster**

等约 45 分钟。期间 Console 显示进度条。

#### 步骤 6：下载 kubeconfig

安装完成后，Console 顶部 **Download kubeconfig**：
```sh
mkdir -p ~/openshift-install/$CLUSTER_NAME/auth
mv ~/Downloads/kubeconfig.cluster1 ~/openshift-install/$CLUSTER_NAME/auth/kubeconfig
chmod 600 ~/openshift-install/$CLUSTER_NAME/auth/kubeconfig
```

**Console 上还会显示**:
- kubeadmin password（保存到 `auth/kubeadmin-password`）
- Console URL（如 `https://console-openshift-console.apps.cluster1.example.local`）—— 测试场景下你访问不到，因为 DNS 没指向真实 IP

### 路径 B2 — Agent-based Installer（接 Phase A2）

#### 步骤 1：补上 alibaba-ccm-config.yaml

```sh
cd ~/openshift-install/$CLUSTER_NAME
cp ~/openshift-install/alibaba-ccm-config.yaml openshift/

# 验证 4 个 manifest 都在
ls openshift/
# 应见：00-ovn-mtu.yaml  01-alibaba-ccm.yaml  03-machineconfig-providerid.yaml  alibaba-ccm-config.yaml
```

> **重要**：你之前 `openshift-install agent create image` 已经生成了 ISO，但当时没有
> `alibaba-ccm-config.yaml`。需要重新生成 ISO 才能把它打进去：

```sh
# 删除旧 ISO 和缓存
rm -f agent.x86_64.iso
rm -rf .openshift_install_state.json

# 重新生成
openshift-install agent create image --dir .
```

新 ISO 含 4 个 manifest。**问题是 ROS 栈已经用了旧 ISO 启动节点了**——这就是
为什么 Agent-based 路径推荐**先 Phase B 后 Phase A**：拿到 `DynamicCustomManifest`
再生成 ISO。

**实操建议**：
- 第一次跑：用 Assisted（路径 A），manifest 上传是"按钮点击"
- 自动化：用 Agent-based（路径 B），但调整顺序为 B → A，先 ROS 出 outputs 再生成 ISO

#### 步骤 2：等待安装完成

```sh
# 监控 bootstrap 阶段
openshift-install agent wait-for bootstrap-complete --dir . --log-level=info

# 监控集群完成
openshift-install agent wait-for install-complete --dir .
```

完成后 kubeconfig 在 `auth/kubeconfig`。

---

## Phase D：应用 post-install 组件

> 集群装完后你拿到了 `kubeconfig`（Assisted: Console 下载；Agent-based: `install-dir/auth/kubeconfig`）。
> 因为本地 RHEL VM 看不到 VPC 内网，**所有 oc 操作都在跳板上跑**。

### D.0 把 kubeconfig 拷到跳板

```sh
# 从 ROS Outputs 拿跳板 IP
JUMP_IP=$(aliyun ros GetStack --StackId $STACK_ID \
  --query "Outputs[?OutputKey=='JumpHostPublicIp'].OutputValue" --output text)
echo "Jump host: $JUMP_IP"

# 把本地 kubeconfig 拷到跳板
scp -i ~/.ssh/openshift_ed25519 \
  ~/Downloads/kubeconfig.cluster1 \
  root@$JUMP_IP:/root/kubeconfig

# SSH 进跳板
ssh -i ~/.ssh/openshift_ed25519 root@$JUMP_IP
```

> 首次 SSH 跳板，确认 cloud-init 已跑完：
> ```sh
> # 在跳板上
> tail /var/log/userdata.log
> # 应见 "9. Marker file" 一行
> ls /var/log/userdata.done
> # 文件存在 → 工具齐全
> which oc kubectl openshift-install kustomize aliyun
> # 全部输出 /usr/local/bin/...
> ```
> 如果还没装完，等 1-2 分钟（开机后台跑的）。

### D.1 在跳板上验证集群

```sh
# 跳板上
export KUBECONFIG=/root/kubeconfig
oc get nodes
# 期望：3 个 master 节点，状态 Ready
```

### D.2 在跳板上跑部署脚本

```sh
# 跳板上（仓库已自动 clone）
cd /root/openshift-alibaba/alibaba-openshift

# 一键部署 CAPI Provider + CSI Operator + CSI Driver CR
./scripts/deploy-post-install.sh

# 输出应包含：
# [✓] Connected to OpenShift X.Y.Z
# [✓] CAPI provider deployed
# [✓] CSI operator deployed
# [✓] CSI driver CR applied
```

> **如果报"AlibabaCloudCSIDriver CRD never became Established"**：
> 等 30 秒再跑一次，OLM 异步建 CRD 有延迟。

---

## Phase E：功能验证

### E.1 节点和 ProviderID

```sh
oc get nodes -o wide
# 期望：3 个节点，状态 Ready，无 NotReady

oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.providerID}{"\n"}{end}'
# 期望：每行 alicloud://cn-wulanchabu.i-bp1xxx
# 如果是空——CCM 没起来或 MachineConfig 没生效
```

### E.2 CCM 健康

```sh
oc get pods -n alibaba-cloud-controller-manager
# 期望：2 个 Pod，Status Running

oc logs -n alibaba-cloud-controller-manager \
  -l k8s-app=alibaba-cloud-controller-manager --tail=20
# 期望：无 Error/Failed；可见 "node initialized" 之类日志
```

### E.3 CSI Operator 和 Driver

```sh
oc get pods -n alibaba-cloud-csi-operator-system
# 期望：1 个 controller-manager Pod Running

oc get alibabacloudcsidriver cluster -o yaml
# 期望：status.diskDriverReady: true

oc get storageclass
# 期望：alicloud-disk-essd 和 alicloud-disk-efficiency；
# essd 标记为 default：(default) 字样

oc get pods -n kube-system -l app=csi-provisioner-disk
# 期望：磁盘 provisioner 在跑
```

### E.4 真实 PVC 挂载测试（核心验证）

```sh
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
      command: [sh, -c, "echo $(date) > /data/x && sleep 3600"]
      volumeMounts:
        - mountPath: /data
          name: vol
  volumes:
    - name: vol
      persistentVolumeClaim:
        claimName: test-disk
EOF

# 等 PVC 绑定（约 30 秒）
oc wait pvc/test-disk --for=jsonpath='{.status.phase}'=Bound --timeout=60s

# 等 Pod 起来
oc wait pod/test-disk-pod --for=condition=Ready --timeout=120s

# 读取数据验证
oc exec test-disk-pod -- cat /data/x
# 期望：当前时间戳

# 清理
oc delete pod/test-disk-pod pvc/test-disk
```

**到这一步如果全部通过——CSI Operator 端到端验证 PASS** ✅

### E.5 Service type=LoadBalancer 测试（验证 CCM）

```sh
cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: Namespace
metadata:
  name: lb-test
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx
  namespace: lb-test
spec:
  replicas: 2
  selector:
    matchLabels: {app: nginx}
  template:
    metadata:
      labels: {app: nginx}
    spec:
      containers:
      - name: nginx
        image: registry.access.redhat.com/ubi9/nginx-122:latest
        ports: [{containerPort: 8080}]
---
apiVersion: v1
kind: Service
metadata:
  name: nginx
  namespace: lb-test
  annotations:
    service.beta.kubernetes.io/alibaba-cloud-loadbalancer-address-type: intranet
spec:
  type: LoadBalancer
  selector: {app: nginx}
  ports:
  - port: 80
    targetPort: 8080
EOF

# 等 EXTERNAL-IP 出现（约 1-2 分钟，CCM 在阿里云创建 SLB）
oc get svc -n lb-test nginx -w
# 期望：EXTERNAL-IP 列从 <pending> 变成具体 IP（如 10.0.16.20）

# 在阿里云控制台 SLB 列表里能看到一个新建的 SLB

# 测试访问（从集群内）
oc run curl --rm -it --restart=Never --image=quay.io/curl/curl -- curl -sI http://nginx.lb-test
# 期望：HTTP/1.1 200 OK

# 清理（重要：不删 Service 会留下 SLB 持续计费）
oc delete namespace lb-test
```

### E.6 CAPI Provider 健康（不做 scale 测试）

```sh
oc get pods -n capa-system
# 期望：capa-controller-manager Running

oc get crd | grep cluster.x-k8s.io
# 期望：alibabacloudclusters / alibabacloudmachines / 等 4 个 CRD
```

> Scale 测试需要 worker MachineDeployment + ECS RAM 凭证传递，比较复杂。
> 可在 Phase F 的 OPCT 之后再做（或干脆放过，CAPI 镜像本身已通过 CI 验证）。

### E.7 ClusterOperators 全绿

```sh
oc get clusteroperators
# 期望：所有 Available=True，Degraded=False
# Compact 3-node 下 monitoring/console 可能 Degraded（因为没 ingress DNS），
# 但 storage/network/cloud-credential 等核心必须绿
```

---

## Phase F：（可选）OPCT 合规性测试

如果要做合规性测试，**预算追加 ¥80**（24 小时不能销毁集群）。

### F.1 安装 OPCT

```sh
# 选择 macOS arm64 或 linux amd64
curl -L https://github.com/redhat-openshift-ecosystem/opct/releases/latest/download/opct-darwin-arm64.tar.gz | tar -xz
sudo mv opct /usr/local/bin/
opct version
```

### F.2 准备专用测试节点

OPCT 推荐用一个专用节点跑 sonobuoy（避免影响其他工作负载）。
Compact 3-node 可以暂时把 master 当 worker 用：

```sh
# 标记一个 master 作为测试节点
oc label node <master-1-name> node-role.kubernetes.io/tests=
```

### F.3 启动 OPCT

```sh
opct run --watch --dedicated
# 跑 4-12 小时，期间不能销毁集群
```

### F.4 收集结果

```sh
opct retrieve
opct report artifact.tar.gz --save-to ~/opct-results
# 打开 ~/opct-results/index.html 看完整报告
```

> OPCT 完整报告会列出所有 conformance 测试通过/失败/跳过情况。
> 跑完即可销毁集群（不用提交报告，本地存档作证据即可）。

---

## Phase G：销毁和成本检查

### G.1 应用层清理（**必做**）

如果跳过这一步，CCM 创建的 SLB 不会被 ROS 删除，会持续计费。

```sh
export KUBECONFIG=~/openshift-install/$CLUSTER_NAME/auth/kubeconfig

# 删除所有 LoadBalancer Service（让 CCM 销毁对应 SLB）
oc get svc -A --field-selector spec.type=LoadBalancer
oc get svc -A --field-selector spec.type=LoadBalancer -o json \
  | jq -r '.items[] | "\(.metadata.namespace) \(.metadata.name)"' \
  | while read ns name; do oc delete svc -n $ns $name; done

# 删除所有 PVC（让 CSI 销毁对应云盘）
oc get pvc -A
oc delete pvc -A --all

# 等阿里云资源真正释放（异步，约 3 分钟）
sleep 180

# 在控制台确认：
# https://slb.console.aliyun.com/  ← 应只剩 ROS 创建的 ApiSLB
# https://ecs.console.aliyun.com/disk/region/cn-wulanchabu/diskList ← 应只剩 ECS 系统盘
```

### G.2 删除 ROS 栈

```sh
aliyun ros DeleteStack --StackId $STACK_ID
# 等 5-10 分钟

# 监控
while true; do
  STATUS=$(aliyun ros GetStack --StackId $STACK_ID --query 'Status' --output text 2>&1)
  echo "$(date '+%H:%M:%S') $STATUS"
  echo "$STATUS" | grep -q "does not exist" && break
  echo "$STATUS" | grep -q "DELETE_FAILED" && { echo "DELETE FAILED, check console"; break; }
  sleep 30
done
```

### G.3 检查孤儿资源（**重要**）

ROS 删栈不会清理它没创建的东西（CCM 建的 SLB、CSI 建的盘）。

```sh
# 检查残留
echo "=== ECS 实例 ==="
aliyun ecs DescribeInstances --RegionId cn-wulanchabu \
  --query 'Instances.Instance[?contains(InstanceName, `cluster1`)].[InstanceName,Status]' --output table

echo "=== 云盘 ==="
aliyun ecs DescribeDisks --RegionId cn-wulanchabu \
  --query 'Disks.Disk[?contains(DiskName, `cluster1`) || contains(Description, `kubernetes`)].[DiskName,Status]' --output table

echo "=== SLB ==="
aliyun slb DescribeLoadBalancers --RegionId cn-wulanchabu \
  --query 'LoadBalancers.LoadBalancer[].[LoadBalancerName,LoadBalancerStatus]' --output table

echo "=== EIP ==="
aliyun vpc DescribeEipAddresses --RegionId cn-wulanchabu \
  --query 'EipAddresses.EipAddress[].[Name,Status]' --output table

echo "=== VPC ==="
aliyun vpc DescribeVpcs --RegionId cn-wulanchabu \
  --query 'Vpcs.Vpc[?contains(VpcName, `cluster1`)].[VpcName,Status]' --output table
```

任何 cluster1 相关的残留都要手动删除。

### G.4 成本核对

24 小时后到 [费用账单](https://usercenter.console.aliyun.com/#/manage/bill)：
- **Bills** → **过去 1 天** → 按服务分组
- 主要费用项应该是：ECS（最大）、SLB、NAT Gateway、流量、ESSD
- 总额应在 ¥80-100 之间（compact 3-node 跑 24 小时）

---

## 故障排查速查

| 现象 | 检查 / 修复 |
|---|---|
| ROS 栈 `CREATE_FAILED` | Console 看 Events 标签页找具体资源失败原因；常见 IAM 权限不足 |
| Discovery 节点不出现 | ECS 节点是否成功启动？查 ECS Console → Instance → VNC，看是否进了 RHCOS live |
| 节点 NotReady | `oc describe node` 看 cloud-taint 是否清除；CCM 日志 |
| Pod CrashLoopBackOff | OVN MTU 没设？参看 `00-ovn-mtu.yaml` 是否正确上传 |
| PVC Pending | CSI 控制器日志：`oc logs -n alibaba-cloud-csi-operator-system -l control-plane=controller-manager` |
| LoadBalancer Service EXTERNAL-IP 一直 pending | CCM 日志；RAM Role 权限是否含 SLB API |
| `oc apply` 报 forbidden | 权限问题，用 kubeadmin 而不是 system:admin |

---

## 一次完整验证的时间和成本

```
Day 0 一次性准备：       30 min（不计费）
Phase A 引导镜像：       30-60 min（OSS 几乎免费）
Phase B ROS 栈：          15 min  (¥1)
Phase C OpenShift 安装： 45-90 min (¥4-6)
Phase D 部署组件：        10 min  (¥1)
Phase E 功能验证：        30 min  (¥2)
Phase F OPCT（可选）：   4-12 h  (¥16-50)
Phase G 销毁：            10 min

不跑 OPCT 总计：          3-4 h, ¥10-20
跑 OPCT 总计：           1 天, ¥80-100
```

测试结束**务必走完 Phase G**，否则 SLB 等资源会持续计费。
