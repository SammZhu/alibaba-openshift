# 阿里云 OpenShift 云原生集成项目 — 预研、设计与开发总结

**文档日期**：2026-05-13  
**项目状态**：开发中（基础框架已完成，待端到端验证）

---

## 一、项目背景与目标

### 目标

在阿里云上安装 OpenShift Container Platform，实现生产可用的云原生集成，包括：

- 节点生命周期管理（Cloud Controller Manager）
- 节点自动伸缩（Cluster API）
- 与阿里云基础设施的深度集成（SLB、DNS、VPC、RAM）

### 技术路线选择

经过预研，确定采用 **External Platform** 模式，而非修改 OpenShift 内部组件：

```yaml
platform:
  external:
    platformName: AlibabaCloud
    cloudControllerManager: External
```

**选择理由：**

| 方案 | 描述 | 问题 |
|------|------|------|
| 原生平台集成 | 修改 CCCMO、CCO 等上游组件 | 维护成本极高，需跟随每个 OCP 版本 |
| **External Platform（选定）** | 独立部署 CCM、CAPI，通过标准接口集成 | 与上游解耦，可独立迭代 |

### 对标参考

主要参考 [oracle-quickstart/oci-openshift](https://github.com/oracle-quickstart/oci-openshift)，OCI 是目前 External Platform 模式最完整的公有云实现。

---

## 二、已回退的早期方案

在确定 External Platform 路线之前，曾尝试以下方案，均已回退：

### Phase 1 — CCCMO 阿里云集成（已回退）
- **仓库**：`SammZhu/cluster-cloud-controller-manager-operator`，分支 `alibaba-cloud-provider`
- **内容**：在 `pkg/cloud/alibaba/` 下实现阿里云 CCM 生命周期管理
- **回退原因**：External Platform 模式不经过 CCCMO，直接部署 CCM manifest

### Phase 3 — CCO Passthrough 模式（已回退）
- **仓库**：`SammZhu/cloud-credential-operator`，分支 `alibaba-cloud-actuator`
- **内容**：在 `pkg/alibaba/` 下实现凭据 Passthrough actuator
- **回退原因**：External Platform 使用 RAM Role（Instance Principal），无需 CCO 管理凭据

---

## 三、现有开发成果

### 3.1 仓库结构

| 仓库 | 分支 | 内容 | 状态 |
|------|------|------|------|
| `SammZhu/cluster-api-provider-alibaba` | `feature/capi-v1beta1-rewrite` | CAPA 控制器 | ✅ 已完成 |
| `SammZhu/alibaba-openshift` | `main` | ROS 模板 + Custom Manifests | ✅ 已完成 |

---

### 3.2 Phase 2 — CAPA 控制器

**仓库**：`SammZhu/cluster-api-provider-alibaba`，分支 `feature/capi-v1beta1-rewrite`

**技术栈**：
- CAPI v1.12.7 / controller-runtime v0.22.5 / Go 1.21
- 基于上游 CAPI v1beta1 接口，非 OpenShift Machine API

**实现的 CRD（`infrastructure.cluster.x-k8s.io/v1beta1`）**：

| CRD | 说明 |
|-----|------|
| `AlibabaCloudCluster` | 集群级基础设施（VPC、SLB 绑定） |
| `AlibabaCloudClusterTemplate` | 集群模板 |
| `AlibabaCloudMachine` | 单台 ECS 实例 |
| `AlibabaCloudMachineTemplate` | ECS 实例模板 |

**Reconciler 实现**：

| 文件 | 负责逻辑 |
|------|---------|
| `alibabacloudcluster_controller.go` | 集群就绪状态、controlPlaneEndpoint 设置 |
| `alibabacloudmachine_controller.go` | ECS 实例创建/查询/ProviderID 注入 |
| `alibabacloudmachinetemplate_controller.go` | 模板不可变性校验 |

**ProviderID 格式**（已修正，与 CCM 对齐）：
```
alicloud://<region>.<instanceID>
```
> 原始设计为 `alibabacloud:///<region>/<instanceID>`，经查阅 `kubernetes/cloud-provider-alibaba-cloud` 源码发现 `NodeFromProviderID()` 只解析 `alicloud://` 前缀加 `.` 分隔符，已修正。

**镜像占位符**：`quay.io/sammzhu/cluster-api-provider-alibaba:latest`（待构建推送）

**与 OCI 的差异化**：OCI 使用手动 Terraform `add-nodes` 扩容，本项目使用 CAPA MachineSet/MachineDeployment 自动伸缩。

---

### 3.3 Phase 4 — 安装框架

**仓库**：`SammZhu/alibaba-openshift`，分支 `main`

#### 3.3.1 ROS 模板（`ros-templates/create-cluster.yaml`）

替代 OCI 的 Terraform，使用阿里云原生 ROS（Resource Orchestration Service），规避 Terraform BSL 许可证问题。

**创建的资源：**

| 资源 | 类型 | 说明 |
|------|------|------|
| VPC | `ALIYUN::ECS::VPC` | 集群专属 VPC，带 k8s cluster 标签 |
| PrivateVSwitch x2 | `ALIYUN::ECS::VSwitch` | 跨可用区私有子网 |
| PublicVSwitch | `ALIYUN::ECS::VSwitch` | NAT 网关使用 |
| NatGateway + EIP | `ALIYUN::VPC::NatGateway` | 私有节点出公网 |
| SNAT Entry x2 | `ALIYUN::VPC::SnatEntry` | 两个私有子网的出公网规则 |
| ControlPlaneSecurityGroup | `ALIYUN::ECS::SecurityGroup` | 放行 6443/22623/2379-2380/集群内互通 |
| WorkerSecurityGroup | `ALIYUN::ECS::SecurityGroup` | 放行来自 master 的流量 |
| NodeRamRole + Policy | `ALIYUN::RAM::Role` | Instance Principal，无 AK/SK |
| ApiSLB | `ALIYUN::SLB::LoadBalancer` | 内网 API Server 负载均衡（6443+22623） |
| PrivateZone DNS | `ALIYUN::PVTZone::Zone` | `api.*` / `api-int.*` 解析到 SLB |
| RendezvousInstance | `ALIYUN::ECS::Instance` | master-1，Agent-based 时固定 IP |
| ControlPlaneInstanceGroup | `ALIYUN::ECS::InstanceGroup` | master-2/3（共 3 台控制平面） |
| WorkerInstanceGroup | `ALIYUN::ECS::InstanceGroup` | 工作节点组 |
| BootstrapInstance | `ALIYUN::ECS::Instance` | 仅 Assisted 模式创建（Condition 控制） |

**支持两种安装模式**（`InstallationMethod` 参数）：

| 参数值 | 模式 | 差异 |
|--------|------|------|
| `Assisted`（默认）| Assisted Installer | 创建 bootstrap 节点；RendezvousInstance 不固定 IP |
| `Agent-based` | Agent-based Installer | 不创建 bootstrap；RendezvousInstance 使用 `RendezvousIp` 固定 IP |

**ROS 输出项：**

| 输出 | 用途 |
|------|------|
| `InstallConfig` | 生成完整 install-config.yaml（含 platform.external 配置） |
| `AgentConfig` | agent-config.yaml，含 rendezvousIP（Agent-based 专用） |
| `DynamicCustomManifest` | CCM ConfigMap，含实际 Region/VPC/Zone/VSwitch 值 |
| `ApiSLBIp` | API Server SLB 内网 IP |
| `VpcId` / `VSwitchId` | 基础设施 ID |

#### 3.3.2 Custom Manifests

| 文件 | 安装时机 | 说明 |
|------|---------|------|
| `01-alibaba-ccm.yaml` | 安装时（Custom Manifests 上传） | CCM 静态资源：SA、RBAC、Deployment |
| `02-capa-crds.yaml` | Post-install | 4 个 CAPI CRD 定义 |
| `02-capa-controller.yaml` | Post-install | CAPA 控制器 Deployment |
| `03-machineconfig-providerid.yaml` | 安装时（Custom Manifests 上传） | kubelet ProviderID 注入（systemd unit） |

> `DynamicCustomManifest` ROS 输出（保存为 `alibaba-ccm-config.yaml`）也需在安装时上传，不在仓库中静态存放。

**CCM 镜像版本**：`registry.k8s.io/provider-alibaba-cloud/alibaba-cloud-controller-manager:v2.14.0`

---

## 四、关键设计决策

### 4.1 认证：RAM Role Instance Principal

所有节点通过 RAM Role 获取临时 STS Token，无 AK/SK 存储：

```
认证优先级（cloud-provider-alibaba-cloud 源码）：
AddonToken > ServiceToken > AKMode > RamRoleToken（我们走最后一条）
```

cloud.conf 中不需要填写 `accessKeyID` / `accessKeySecret`。

### 4.2 ProviderID 格式（关键）

格式必须与 `kubernetes/cloud-provider-alibaba-cloud` CCM 的 `NodeFromProviderID()` 解析逻辑一致：

```
正确格式：alicloud://<region>.<instanceID>
错误格式：alibabacloud:///<region>/<instanceID>  ← 早期设计，已修正
```

CCM 源码解析逻辑：
```go
// 仅识别 alicloud:// 前缀，然后用 "." 分割 region 和 instanceID
func NodeFromProviderID(providerID string) (string, string, error) {
    if strings.HasPrefix(providerID, "alicloud://") {
        providerID = strings.Split(providerID, "://")[1]
    }
    name := strings.Split(providerID, ".")
    return name[0], name[1], nil
}
```

### 4.3 Manifest 注入时序

借鉴 vSphere PostCreateManifestsHook 模式和 OCI `dynamic_custom_manifest` 设计：

| Manifest | 时序 | 原因 |
|----------|------|------|
| CCM ConfigMap（DynamicCustomManifest） | 安装时 | 需要 ROS 输出的实际值，不能静态存放 |
| CCM 静态资源（01-alibaba-ccm.yaml） | 安装时 | 与 ConfigMap 同步注入 |
| MachineConfig ProviderID | 安装时 | 必须在 kubelet 首次启动前写入，不能 post-install |
| CAPA CRDs + Controller | Post-install | 依赖集群 API Server 可用，无法安装时注入 |

### 4.4 OCI vs 阿里云 核心对比

| 能力 | OCI 实现 | 阿里云实现 |
|------|---------|-----------|
| IaC 工具 | Terraform（BSL 许可证） | ROS（原生，无许可证问题）|
| 安装方式 | Assisted / Agent-based | Assisted / Agent-based |
| 节点伸缩 | 手动 Terraform add-nodes | **CAPA MachineSet 自动伸缩**（差异化）|
| CCM 认证 | Instance Principal | RAM Role（等价）|
| 动态 Manifest | `dynamic_custom_manifest` Terraform 输出 | `DynamicCustomManifest` ROS 输出 |
| CSI 驱动 | OCI CSI（含 iSCSI） | 未实现（后续规划）|

---

## 五、参考工程分析

### 5.1 `kubernetes/cloud-provider-alibaba-cloud`

- **状态**：活跃，最新版本 v2.14.0（2026-04-13）
- **用途**：直接作为 CCM 使用（不修改）
- **关键发现**：`NodeFromProviderID()` 格式要求，已据此修正我们的 ProviderID 格式
- **认证**：支持 RAM Role 自动 fallback，cloud.conf 无需 AK/SK

### 5.2 `openshift/cluster-api-provider-alibaba`

- **状态**：未归档但实质停滞（依赖停在 k8s v0.24.1 / 2022 年）
- **性质**：OpenShift **Machine API** 实现，非 CAPI v1beta1 — 与本项目架构不同
- **借鉴价值**：`instances.go` 中 ECS RunInstances 参数处理、ProviderID 格式参考

---

## 六、待完成事项

| 优先级 | 事项 | 说明 |
|--------|------|------|
| P0 | 构建并推送 CAPA 镜像 | `quay.io/sammzhu/cluster-api-provider-alibaba:latest` 当前为占位符 |
| P0 | Assisted Installer 端到端验证 | 走完完整三段式流程，验证 ROS 模板和 Manifests |
| P1 | Agent-based 端到端验证 | 验证 rendezvousIP 固定分配和 `openshift-install agent` 流程 |
| P1 | CAPA MachineSet 示例 | 补充完整的 `AlibabaCloudCluster` + `MachineDeployment` 示例 YAML |
| P2 | 阿里云 CSI 驱动集成 | 块存储持久卷支持（参考 OCI CSI 实现） |
| P2 | 多可用区 worker 分布 | ControlPlaneInstanceGroup 目前只用一个 VSwitch |

---

## 七、安装流程速查

### Assisted Installer

```
1. Red Hat Console → 生成 Discovery ISO → 上传 OSS
2. ROS Console → 创建栈（InstallationMethod=Assisted）
   → 输出 InstallConfig（粘贴到控制台）
   → 输出 DynamicCustomManifest（保存为 alibaba-ccm-config.yaml）
3. Red Hat Console → 上传 Custom Manifests：
     alibaba-ccm-config.yaml
     custom_manifests/01-alibaba-ccm.yaml
     custom_manifests/03-machineconfig-providerid.yaml
   → 分配节点角色 → 开始安装
4. Post-install：
     oc apply -f custom_manifests/02-capa-crds.yaml
     oc apply -f custom_manifests/02-capa-controller.yaml
```

### Agent-based Installer

```
1. ROS Console → 创建栈（InstallationMethod=Agent-based，placeholder ISO）
   → 输出 InstallConfig / AgentConfig / DynamicCustomManifest
2. 本地：
     mkdir -p install-dir/openshift
     # 填写 install-config.yaml 的 pullSecret 和 sshKey
     # 放入 openshift/ 目录：01-alibaba-ccm.yaml / alibaba-ccm-config.yaml /
     #                        03-machineconfig-providerid.yaml
     openshift-install agent create image --dir install-dir/
3. 上传 agent.x86_64.iso 到 OSS → 更新 ROS 栈 DiscoveryIsoUrl → 重启 ECS 节点
4. openshift-install agent wait-for install-complete --dir install-dir/
5. Post-install：
     oc apply -f custom_manifests/02-capa-crds.yaml
     oc apply -f custom_manifests/02-capa-controller.yaml
```
