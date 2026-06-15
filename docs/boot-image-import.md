# 导入 OpenShift 引导镜像到阿里云

本指南详细说明如何把 OpenShift 安装 ISO（Assisted Installer 的 Discovery ISO，或
Agent-based Installer 生成的 `agent.x86_64.iso`）导入为阿里云**自定义 ECS 镜像**。
这是创建 ROS 栈之前的**一次性前置工作**。

> 这里导入的不是 RHCOS 本身——RHCOS 内嵌在 ISO 里，由 ISO 引导后从内存运行并把
> 系统刷到 ECS 系统盘。导入的对象是 ISO，最终产出的 `m-bp1xxx...` Image ID 既
> 用作 bootstrap、master、worker 节点的引导镜像，也用作 CAPI MachineDeployment
> 的 `imageID`。

---

## 总览

```
┌────────────────────────────────────┐
│ 1. 下载/生成 ISO                   │
│    Assisted: Discovery ISO         │
│    Agent-based: agent.x86_64.iso   │
└──────────────────┬─────────────────┘
                   │
                   ▼
┌────────────────────────────────────┐
│ 2. 上传到阿里云 OSS                │
│    桶必须在目标 Region             │
└──────────────────┬─────────────────┘
                   │
                   ▼
┌────────────────────────────────────┐
│ 3. ECS Images → Import Image       │
│    OSS URL + Linux + x86_64 + ISO  │
└──────────────────┬─────────────────┘
                   │  等待 15-30 分钟
                   ▼
┌────────────────────────────────────┐
│ 4. 复制 Image ID (m-bp1xxxxx)      │
│    用作 ROS 栈的 ImageId 参数      │
└────────────────────────────────────┘
```

---

## 前置条件

- 阿里云账号，已开通 **OSS**（对象存储）和 **ECS**
- RAM 用户具备以下权限：
  - `AliyunOSSFullAccess`（或至少 `oss:PutObject` + `oss:GetObject` 对目标桶）
  - `AliyunECSFullAccess`（或至少 `ecs:ImportImage` + `ecs:DescribeImages`）
- 安装好阿里云 CLI（`aliyun`）或使用 Web 控制台均可
- 决定好集群部署的 **Region**（如 `cn-wulanchabu`）—— OSS 桶必须与 ROS 栈在**同一 Region**

---

## 步骤 1：下载/生成 ISO

### 选项 A — Assisted Installer

1. 访问 [Red Hat Hybrid Cloud Console](https://console.redhat.com/openshift)
2. **Clusters** → **Create cluster** → **Datacenter** → **Bare Metal (x86_64)**
3. 填写：
   - **Cluster name**: 与 ROS 栈 `ClusterName` 参数一致
   - **Base domain**: 与 ROS 栈 `BaseDomain` 参数一致
   - **OpenShift version**: 4.20（推荐 EUS 版本，长支持周期 + ELS 资格；只要 ≥ 4.16 都能跑 External Platform）
4. 进入 **Host discovery** → **Add hosts** → **Minimal image file**（注意：选 Minimal，不是 Full）
5. 下载 ISO 文件，重命名为有意义的名字，例如 `discovery-iso-cluster1.iso`

### 选项 B — Agent-based Installer（已自动化）

> **现在由 `site-agent.yml` 自动化**（`installation_method: agent`）。`tasks/iso_agent.yml`
> 会渲染 install-config.yaml + agent-config.yaml + custom manifests 并调用
> `openshift-install agent create image`，本节的手工步骤改由它代劳；步骤 2-5
> 的 OSS 上传 / ImportImage 也由 `02-import-image.yml` 复用。手工流程仅作原理参考。
> ⚠ 实例供给 + 静态 NMState 的 MAC 采集(`06a`/`06b`)与首次 HA live 验证待
> 「预创建 ENI 作主网卡」spike 完成(见 ABI 计划 §4/§7)。

需要本地（RHEL8 operator host）有 `openshift-install` 二进制：

```sh
# 下载 openshift-install（RHEL 8 / Alibaba Linux 3）
curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/latest-4.20/openshift-install-linux.tar.gz | tar -xz
sudo install -m 0755 openshift-install /usr/local/bin/ && rm -f openshift-install README.md

# 手工原理（自动化等价见 tasks/iso_agent.yml）：
mkdir -p install-dir/openshift
# 写入 install-config.yaml、agent-config.yaml、custom manifests 到 install-dir/openshift/
./openshift-install agent create image --dir install-dir/
ls install-dir/agent.x86_64.iso
```

详细的 install-config / agent-config / custom manifest 准备步骤见
[主 README "Agent-based Installer" 章节](../README.md)。

---

## 步骤 2：上传 ISO 到 OSS

### 选项 A — 通过 Web 控制台

1. 登录 [OSS 控制台](https://oss.console.aliyun.com/)
2. **Buckets** → **Create Bucket**
   - **Name**: `openshift-images-<your-prefix>`（全局唯一）
   - **Region**: 与 ROS 栈相同 Region
   - **Storage Class**: Standard
   - **Access Control**: Private（默认）
3. 进入 Bucket → **Upload** → 选择上一步的 ISO 文件
4. 上传完成后，在文件列表点击文件名 → **Copy URL** → 选择 **HTTPS** 或 **OSS URL**

记录下 OSS URL，形如：
```
https://openshift-images-myorg.oss-cn-wulanchabu.aliyuncs.com/discovery-iso-cluster1.iso
```

### 选项 B — 通过 aliyun CLI

```sh
# 配置（如未配置过）
aliyun configure --profile default

# 创建 Bucket
aliyun oss mb oss://openshift-images-myorg --region cn-wulanchabu

# 上传 ISO
aliyun oss cp ./discovery-iso-cluster1.iso \
  oss://openshift-images-myorg/discovery-iso-cluster1.iso \
  --region cn-wulanchabu

# 生成 URL（导入时使用）
echo "https://openshift-images-myorg.oss-cn-wulanchabu.aliyuncs.com/discovery-iso-cluster1.iso"
```

---

## 步骤 3：导入为自定义 ECS 镜像

### 选项 A — Web 控制台

1. 登录 [ECS 控制台](https://ecs.console.aliyun.com/)
2. 左侧菜单 **Storage & Snapshots** → **Images** → **Custom Image** 标签页
3. 顶部点击 **Import Image**
4. 填写：

   | 字段 | 值 |
   |---|---|
   | Region | 与 OSS Bucket 相同 |
   | OSS Object Address | 步骤 2 复制的 URL（不需要 https:// 前缀，可只填路径部分）|
   | Image Name | `openshift-discovery-iso-<cluster>` |
   | Operating System | **Linux** |
   | System Disk Size | 留空（ISO 自身就是引导介质，不占系统盘）|
   | System Architecture | **x86_64** |
   | System Platform | **Others Linux** |
   | Image Format | **ISO** |
   | License Type | **Auto** |

5. 点击 **OK** —— 导入任务开始。

> **首次导入**：阿里云会要求你**授权 ECS 访问 OSS 服务**。出现提示时点击 "Authorize"，
> 系统会自动创建 `AliyunECSImageImportDefaultRole` RAM 角色。这是一次性动作。

### 选项 B — aliyun CLI

```sh
aliyun ecs ImportImage \
  --RegionId cn-wulanchabu \
  --ImageName openshift-discovery-iso-cluster1 \
  --OSType Linux \
  --Platform Others_Linux \
  --Architecture x86_64 \
  --DiskDeviceMapping.1.Format ISO \
  --DiskDeviceMapping.1.OSSBucket openshift-images-myorg \
  --DiskDeviceMapping.1.OSSObject discovery-iso-cluster1.iso
```

返回的 JSON 包含 `ImageId` 字段——这是你最终要用的值（先记下，但**不要立即用**，
镜像状态此时是 `Importing`，还不能用于创建实例）。

---

## 步骤 4：等待导入完成

导入时间通常 **15–30 分钟**，取决于 ISO 大小（Discovery ISO 约 100 MB；
agent.x86_64.iso 约 1 GB）。

### Web 控制台

ECS Console → Images → Custom Image，看 **Status** 列：
- `Importing` / `Creating` —— 还在处理
- `Available` —— ✅ 可用

### CLI

```sh
aliyun ecs DescribeImages \
  --RegionId cn-wulanchabu \
  --ImageId m-bp1xxxxxxxxxxxxxxx \
  --query 'Images.Image[0].Status'
```

返回 `Available` 即可。

---

## 步骤 5：拿到 Image ID

最终的 Image ID 格式：`m-bp1` 开头加 22 位字符，例如：
```
m-bp1abcdef0123456789xyz
```

把这个值传给 ROS 栈：

```yaml
# 在 ROS 控制台 → Create Stack → Parameters：
ImageId: m-bp1abcdef0123456789xyz
```

或者：

```sh
aliyun ros CreateStack \
  --StackName openshift-cluster1 \
  --TemplateBody "$(cat ros-templates/create-cluster-LEGACY.yaml)" \
  --Parameters '[{"ParameterKey":"ImageId","ParameterValue":"m-bp1abcdef0123456789xyz"}, ...]'
```

---

## 后续使用

这个 Image ID 在整个集群生命周期里被多处引用，**不要删除它**：

| 用途 | 引用位置 |
|---|---|
| Master 节点引导 | ROS 栈 `MasterInstance1/2/3` |
| Worker 节点引导（初始）| ROS 栈 `WorkerInstance` |
| **CAPI MachineDeployment 扩容 Worker** | `AlibabaCloudMachineTemplate.spec.imageID` |
| 节点替换（MachineHealthCheck）| 同上 |

---

## 跨 Region 复制

如果需要在其它 Region 部署集群，**不需要重新导入**——用阿里云的 **Copy Image** 功能：

```sh
aliyun ecs CopyImage \
  --RegionId cn-wulanchabu \
  --ImageId m-bp1abcdef0123456789xyz \
  --DestinationRegionId cn-shanghai \
  --DestinationImageName openshift-discovery-iso-cluster1
```

复制完成（约 10–20 分钟）后，新 Region 会拿到一个新的 `m-rj9...` Image ID。

---

## 常见问题

### 导入失败：`InvalidOSSObject.NotFound`
- 检查 OSS URL 是否完整、是否有 `oss://` 前缀的混淆
- 确认 Bucket 与目标 Region 一致
- 确认 RAM 用户 / `AliyunECSImageImportDefaultRole` 有 OSS 读权限

### 导入卡在 `Importing` 超过 1 小时
- ISO 文件可能损坏，重新下载/生成并上传
- ISO 格式参数选错（如选成 RAW），删除任务重来

### 创建实例时报 `InvalidImageId.NotFound`
- Image 还在 `Importing`/`Creating` 状态——等到 `Available`
- Image 在另一个 Region——确认 ROS 栈和 Image 的 Region 一致

### Image 大小限制
- 单个导入 ISO 最大 500 GB（远超 OpenShift 需求）
- 系统盘镜像建议 ≤ 40 GB；OpenShift Discovery ISO ≈ 100 MB，agent ISO ≈ 1 GB

---

## 自动化建议（未来工作）

当前流程的步骤 2-4 都可以脚本化。建议把 ROS 模板扩展为支持**直接传 OSS URL 而非 Image ID**——
让 ROS 内部先创建 `ALIYUN::ECS::Image` 资源、等导入完成、再使用结果 ImageId。这样
用户只需上传 ISO 到 OSS、传 URL 给 ROS，剩下一站式完成。

不在本次范围内，留作 `roadmap`。
