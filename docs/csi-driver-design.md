# Alibaba Cloud CSI Driver 集成设计

**状态**：代码完成（env-free 全部实现，待集群 live 验证）
**版本**：v0.5（operator 代码对齐 + per-SC volumeMode + install CR 启用 snapshot/storageProfile/NAS）
**日期**：2026-06-12
**阶段**：Phase 1 扩展（存储支持）

---

## 变更历史

| 版本 | 日期 | 变更说明 |
|------|------|---------|
| v0.1 | 2026-05-14 | 初稿，Helm 方案 |
| v0.2 | 2026-05-14 | 改用 OLM Operator 方案 |
| v0.3 | 2026-05-14 | OpenShift Virtualization 存储适配分析；NAS 升为 P1；OSS 定位为备份存储；新增 VolumeSnapshotClass、StorageProfile、OADP 集成 |
| v0.4 | 2026-05-14 | 参考 AWS ROSA EBS CSI Operator 和 CDI StorageProfile 机制：新增 seLinuxMount、StorageProfile 主动 patch 策略、条件化 VolumeSnapshotClass、CDI 上游贡献计划、clone strategy 设计 |
| v0.5 | 2026-06-12 | operator 代码已落地 v0.4 全部设计（seLinuxMount ✅、VolumeSnapshotClass ✅、StorageProfile patch ✅、NAS driver ✅）；新增 per-StorageClass `volumeMode` 字段（去掉 isBlock 硬编码，通用盘 Filesystem / VM 盘 Block 分离）；install CR(04-csi-driver-cr.yaml) 启用 snapshot + storageProfile + NAS。剩余仅集群 live 验证（#32-34 e2e）|

---

## 1. 背景与目标

OpenShift 在 External Platform 模式下运行时，OCP 不会自动安装任何云厂商的 CSI driver。
集群没有 CSI driver 意味着：

- 无法使用 `PersistentVolumeClaim`（PVC 无法动态供给）
- Operator 及工作负载若依赖持久存储则无法正常运行
- 无法使用 VolumeSnapshot 做应用级备份
- OpenShift Virtualization VM **无法热迁移**（缺少 RWX 存储）

**目标**：通过 OLM（Operator Lifecycle Manager）安装 Alibaba Cloud CSI Driver Operator，
在 install-time 完成注入，使集群具备：
1. 云盘动态供给（块存储，VM OS 盘）
2. NAS 动态供给（共享文件存储，VM 热迁移）
3. VM 快照与备份（VolumeSnapshotClass + OADP + OSS）

---

## 2. 阿里云存储服务适用性评估

### 2.1 各存储服务对 OpenShift Virtualization 的适配矩阵

| 存储服务 | accessMode | volumeMode | 热迁移 | VM 快照 | 适用场景 | 优先级 |
|---------|-----------|-----------|--------|--------|---------|--------|
| **云盘 ESSD** | RWO | Block / Filesystem | ❌ | ✅ | VM OS 盘（无热迁移要求）| **P1** |
| **NAS 性能型** | RWX | Filesystem | ✅ | ⚠️ | VM OS 盘（需热迁移）+ 共享数据盘 | **P1** ↑ |
| **NAS 极速型** | RWX | Filesystem | ✅ | ⚠️ | 高 IOPS 要求的 VM 盘 | P2 |
| OSS | ❌ 不适合挂载 | 不适用 | ❌ | ❌ | **备份仓库**（S3 协议，非挂载）| P1（备份） |
| 共享块存储 | RWX | Block | ✅ | ✅ | 热迁移 + Block 性能 | P3（生态不成熟）|
| CPFS | RWX | Filesystem | ✅ | ❌ | HPC 场景 | P3（成本极高）|

### 2.2 核心矛盾：云盘 RWO vs 热迁移 RWX

```
OpenShift Virtualization 热迁移要求：
  VM disk PVC.accessMode = ReadWriteMany (RWX)

阿里云云盘限制：
  ecs:AttachDisk → 同一时刻只能挂载到单台 ECS → 只支持 RWO

结论：
  仅有 Disk CSI → VM 无法热迁移 → 节点维护/驱逐时 VM 被强制关机
  生产环境必须同时部署 NAS CSI 以支持热迁移
```

### 2.3 OSS 作为运行时存储：不适用

OSS 通过 FUSE（ossfs）挂载时存在以下问题，不能作为 VM 运行时存储：

- 随机小块 I/O 性能极差（对象存储语义，非块设备）
- 非 POSIX 兼容（无文件锁、fsync 语义不完整）
- 无 Block volumeMode 支持
- FUSE 层额外延迟

### 2.4 OSS 作为备份存储：高度适用

OSS 完全规避了上述缺点，备份场景只需顺序写和 S3 API：

| 备份诉求 | OSS 能力 |
|---------|---------|
| 持久性 | 99.9999999999%（12个9）|
| 容量 | 无上限 |
| 成本 | ¥0.015/GB/月（标准）→ ¥0.01（低频）→ ¥0.0045（归档）|
| S3 兼容 | ✅ OADP/Velero 原生支持 |
| 跨区域复制 | ✅ 灾备场景 |
| 生命周期管理 | ✅ 自动冷数据分层、过期删除 |

---

## 3. 存储优先级（更新后）

| 存储类型 | 优先级 | 说明 | 变更 |
|---------|--------|------|------|
| 云盘（Disk/ESSD） | **P1** | VM OS 盘，高性能块存储，RWO + Block | 不变 |
| NAS 文件存储 | **P1** ↑ | VM 热迁移必须，RWX + Filesystem | **从 P2 升为 P1** |
| VolumeSnapshotClass | **P1** 新增 | VM 快照、OADP 备份链路依赖 | 新增 |
| StorageProfile patch | **P1** 新增 | OKV 自动识别正确 accessMode/volumeMode | 新增 |
| OADP + OSS 备份 | **P1** 新增 | VM 数据保护，生产必须 | 新增 |
| OSS 挂载 CSI | P3（降级） | 不做挂载驱动，只做 CDI 导入源 | 从 P3 降为不实现 |
| 共享块存储 | P3 | 生态不成熟 | 新增（观察）|

---

## 4. 整体架构（更新）

```
┌─────────────────────────────────────────────────────────────────────┐
│  quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest           │
│  （OLM Catalog Image，含 Disk + NAS CSI + VolumeSnapshotClass 管理）  │
└─────────────────────────────────────────────────────────────────────┘
              ↓  CatalogSource（install-time 注入）
┌─────────────────────────────────────────────────────────────────────┐
│  OLM → InstallPlan → alibaba-cloud-csi-operator Pod                 │
│         监听 CR：AlibabaCloudCSIDriver                               │
└─────────────────────────────────────────────────────────────────────┘
              ↓  reconcile
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│  Disk CSI 组件（RWO/Block）   │   │  NAS CSI 组件（RWX/Filesystem）   │
│  ├── CSIDriver               │   │  ├── CSIDriver                    │
│  ├── Controller Deployment   │   │  ├── Controller Deployment        │
│  ├── Node DaemonSet          │   │  ├── Node DaemonSet               │
│  ├── StorageClass (Block)    │   │  ├── StorageClass (RWX)           │
│  ├── VolumeSnapshotClass     │   │  └── StorageProfile patch         │
│  └── StorageProfile patch    │   └──────────────────────────────────┘
└──────────────────────────────┘
              ↓  VolumeSnapshot（VM 快照）
┌─────────────────────────────────────────────────────────────────────┐
│  OADP Operator（Velero）                                             │
│  ├── BackupStorageLocation → OSS Bucket（S3 协议）                   │
│  ├── VolumeSnapshotLocation → 阿里云快照（CSI）                       │
│  └── Schedule → 定时备份 VM + PVC                                    │
└─────────────────────────────────────────────────────────────────────┘
              ↓  备份数据
┌─────────────────────────────────────────────────────────────────────┐
│  OSS Bucket（openshift-backup-<cluster>）                            │
│  生命周期：7天标准 → 30天低频 → 90天归档                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. OKV（OpenShift Virtualization）存储设计

### 5.1 VM 存储拓扑

```
VM 类型                  OS 盘存储          数据盘存储        迁移能力
─────────────────────────────────────────────────────────────────────
普通 VM（无迁移要求）     云盘 ESSD RWO      云盘 ESSD RWO     ❌ 关机迁移
生产 VM（需热迁移）       NAS 性能型 RWX     NAS / 云盘        ✅ 热迁移
高性能 VM               NAS 极速型 RWX     云盘 ESSD         ✅ 热迁移
```

### 5.2 StorageClass 设计

```yaml
# SC-1: 云盘块存储（VM OS 盘，高性能，无热迁移）
# OKV 会选择 Block volumeMode，直接作为原始块设备挂给 VM
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: alicloud-disk-essd-block
  annotations:
    storageclass.kubernetes.io/is-default-class: "false"
    storageclass.kubevirt.io/is-default-virt-class: "true"   # OKV 默认虚拟机存储类
provisioner: diskplugin.csi.alibabacloud.com
parameters:
  type: cloud_essd
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true

---
# SC-2: NAS 文件存储（VM 热迁移，RWX）
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: alicloud-nas-performance
provisioner: nasplugin.csi.alibabacloud.com
parameters:
  mountProtocol: nfs
  storageType: Performance
volumeBindingMode: Immediate          # NAS 需要 Immediate（无节点亲和性）
allowVolumeExpansion: true
```

### 5.3 VolumeSnapshotClass

```yaml
# VM 快照依赖 CSI VolumeSnapshotClass
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshotClass
metadata:
  name: alicloud-disk-snapshot
  annotations:
    snapshot.storage.kubernetes.io/is-default-class: "true"
driver: diskplugin.csi.alibabacloud.com
deletionPolicy: Delete
parameters:
  forceDelete: "false"   # 保留快照直到所有引用删除
```

### 5.4 StorageProfile（OKV 自动发现）

OKV 为每个 StorageClass 自动创建 StorageProfile，需要 patch 声明最优 accessMode + volumeMode 组合：

```yaml
# 云盘：优先 Block + RWO
apiVersion: cdi.kubevirt.io/v1beta1
kind: StorageProfile
metadata:
  name: alicloud-disk-essd-block
spec:
  claimPropertySets:
    - accessModes: [ReadWriteOnce]
      volumeMode: Block               # VM 首选，无格式化层开销
    - accessModes: [ReadWriteOnce]
      volumeMode: Filesystem          # 兼容非 VM 工作负载

---
# NAS：RWX + Filesystem（热迁移）
apiVersion: cdi.kubevirt.io/v1beta1
kind: StorageProfile
metadata:
  name: alicloud-nas-performance
spec:
  claimPropertySets:
    - accessModes: [ReadWriteMany]
      volumeMode: Filesystem          # NAS 仅支持 Filesystem
```

---

## 6. OADP + OSS 备份集成

### 6.1 备份链路

```
OKV VirtualMachineSnapshot
    │ (触发 CSI CreateSnapshot)
    ▼
云盘快照（阿里云侧）
    │ (OADP VolumeSnapshotLocation)
    ▼
OADP Velero 导出
    │ (S3 API，内网 endpoint)
    ▼
OSS Bucket（openshift-backup-<cluster>）
    │
    ├── 7天内：标准存储（¥0.015/GB/月）
    ├── 30天内：低频存储（¥0.01/GB/月）
    └── 90天+：归档存储（¥0.0045/GB/月）
```

### 6.2 OADP BackupStorageLocation

```yaml
apiVersion: oadp.openshift.io/v1alpha1
kind: DataProtectionApplication
metadata:
  name: alibaba-cloud-dpa
  namespace: openshift-adp
spec:
  configuration:
    velero:
      defaultPlugins:
        - openshift
        - aws         # OSS 使用 aws S3 兼容插件
      resourceTimeout: 10m
  backupLocations:
    - name: default
      velero:
        provider: aws
        default: true
        objectStorage:
          bucket: openshift-backup-<cluster-name>
          prefix: velero
        config:
          region: cn-hangzhou
          s3Url: https://oss-cn-hangzhou-internal.aliyuncs.com   # 内网访问免流量费
          s3ForcePathStyle: "true"
          insecureSkipTLSVerify: "false"
        credential:
          name: oss-backup-credentials
          key: cloud
  snapshotLocations:
    - name: default
      velero:
        provider: aws
        config:
          region: cn-hangzhou
          # 使用 CSI VolumeSnapshot，不直接调用 ECS 快照 API
```

### 6.3 OSS 认证（独立 RAM Policy，最小权限）

OADP 使用独立的 AK/SK，**不共用节点 RAM Role**：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:PutObject",
        "oss:GetObject",
        "oss:DeleteObject",
        "oss:ListObjects",
        "oss:ListBuckets",
        "oss:GetBucketInfo",
        "oss:GetBucketLocation"
      ],
      "Resource": [
        "acs:oss:*:*:openshift-backup-*",
        "acs:oss:*:*:openshift-backup-*/*"
      ]
    }
  ]
}
```

Velero credentials secret：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: oss-backup-credentials
  namespace: openshift-adp
stringData:
  cloud: |
    [default]
    aws_access_key_id=<OADP_AK>
    aws_secret_access_key=<OADP_SK>
```

---

## 7. AlibabaCloudCSIDriver CR（更新后完整 spec）

```yaml
apiVersion: csi.alibabacloud.com/v1alpha1
kind: AlibabaCloudCSIDriver
metadata:
  name: cluster
  namespace: kube-system
spec:
  # ── Phase 1: 云盘（块存储）────────────────────────────────────────
  disk:
    enabled: true
    defaultStorageClass: true
    storageClasses:
      - name: alicloud-disk-efficiency
        type: cloud_efficiency
        reclaimPolicy: Delete
        allowVolumeExpansion: true
        volumeMode: Filesystem           # 通用容器工作负载
      - name: alicloud-disk-essd
        type: cloud_essd
        reclaimPolicy: Delete
        allowVolumeExpansion: true
        volumeMode: Filesystem
      - name: alicloud-disk-essd-block   # OKV VM OS 盘专用（Block 模式）
        type: cloud_essd
        reclaimPolicy: Delete
        allowVolumeExpansion: true
        volumeMode: Block
        virtDefault: true                # storageclass.kubevirt.io/is-default-virt-class
    snapshot:
      enabled: true
      className: alicloud-disk-snapshot  # VolumeSnapshotClass 名称
    storageProfile:
      patch: true                        # 自动 patch OKV StorageProfile

  # ── Phase 1: NAS（共享文件存储，VM 热迁移）────────────────────────
  nas:
    enabled: true                        # ↑ 从 false 改为 true（P1）
    storageClasses:
      - name: alicloud-nas-performance
        storageType: Performance
        mountProtocol: nfs
        reclaimPolicy: Delete
        allowVolumeExpansion: true
    storageProfile:
      patch: true

  # ── OSS：不做挂载驱动，备份通过 OADP 独立对接 ─────────────────────
  oss:
    enabled: false

  imageTag: v1.35.3

  auth:
    ramToken: v2

  controller:
    replicas: 2
    nodeSelector:
      node-role.kubernetes.io/master: ""
    tolerations:
      - key: node-role.kubernetes.io/master
        effect: NoSchedule
```

---

## 8. Reconcile 逻辑（更新）

```
AlibabaCloudCSIDriver CR
  ↓
reconcile()
  ├── 确保 RBAC（ServiceAccount、ClusterRole、ClusterRoleBinding）
  ├── 确保 SCC privileged binding
  │
  ├── 如果 spec.disk.enabled：
  │     ├── 确保 CSIDriver 对象（diskplugin.csi.alibabacloud.com）
  │     ├── 确保 Controller Deployment（provisioner + attacher + resizer）
  │     ├── 确保 Node DaemonSet（plugin + node-registrar，privileged）
  │     ├── 确保 StorageClass（按列表，含 Block volumeMode 变体）
  │     ├── 如果 spec.disk.snapshot.enabled：
  │     │     └── 确保 VolumeSnapshotClass
  │     └── 如果 spec.disk.storageProfile.patch：
  │           └── patch StorageProfile（Block 优先）
  │
  ├── 如果 spec.nas.enabled：           ← P1 新增
  │     ├── 确保 CSIDriver 对象（nasplugin.csi.alibabacloud.com）
  │     ├── 确保 Controller Deployment（nas-provisioner）
  │     ├── 确保 Node DaemonSet（nas-plugin）
  │     ├── 确保 StorageClass（RWX，Immediate binding）
  │     └── 如果 spec.nas.storageProfile.patch：
  │           └── patch StorageProfile（RWX + Filesystem）
  │
  └── 更新 CR Status
        ├── DiskDriverReady
        ├── NASDriverReady
        └── Conditions（Available / Progressing / Degraded）
```

---

## 9. RAM 权限汇总

### 9.1 节点 RAM Role（已在 ROS template 实现）

| 用途 | 权限 |
|------|------|
| CCM（SLB、VPC、PVTZ）| 已有 |
| CAPA（ECS 实例生命周期）| 已有 |
| Disk CSI | ecs:AttachDisk/DetachDisk/CreateDisk/DeleteDisk/ResizeDisk/ModifyDiskAttribute/CreateSnapshot/DeleteSnapshot/DescribeDisks/DescribeSnapshots/\*AutoSnapshotPolicy\* |
| NAS CSI | nas:DescribeFileSystems/CreateFileSystem/DescribeMountTargets/CreateMountTarget（由 NAS controller 调用）|

### 9.2 OADP 专用 RAM Policy（独立 AK/SK，不绑定节点）

```
oss:PutObject / GetObject / DeleteObject / ListObjects / GetBucketInfo
Resource: acs:oss:*:*:openshift-backup-*
```

---

## 10. Install-time 注入（custom_manifests）

```
custom_manifests/
  04-csi-catalogsource.yaml       ✅ 已完成
  04-csi-operatorgroup.yaml       ✅ 已完成
  04-csi-subscription.yaml        ✅ 已完成
  04-csi-driver-cr.yaml           ✅ 已更新（NAS + snapshot + storageProfile + per-SC volumeMode）
  05-oadp-subscription.yaml       🔜 待实现
  05-oadp-dpa.yaml                🔜 待实现（DataProtectionApplication）
  05-oadp-oss-credentials.yaml    🔜 待实现（Secret，敏感，需加密处理）
```

---

## 11. 实现工作项（更新）

### P1 — 代码实现（无需环境）

| 工作项 | 工程 | 状态 |
|-------|------|------|
| Disk CSI controller + DaemonSet | alibaba-cloud-csi-operator | ✅ 已实现 |
| Disk StorageClass（含 Block 模式）| alibaba-cloud-csi-operator | ✅ 已实现（per-SC `volumeMode` 字段，2026-06-12）|
| VolumeSnapshotClass | alibaba-cloud-csi-operator | ✅ 已实现（条件创建，CRD 缺失则跳过）|
| StorageProfile patch | alibaba-cloud-csi-operator | ✅ 已实现（claimPropertySets + cloneStrategy + snapshotClass）|
| NAS CSI controller + DaemonSet | alibaba-cloud-csi-operator | ✅ 已实现 |
| NAS StorageClass（RWX）| alibaba-cloud-csi-operator | ✅ 已实现（Immediate binding）|
| CRD 新字段（volumeMode、snapshot、nas.storageClasses）| alibaba-cloud-csi-operator | ✅ 已实现 |
| OADP Subscription manifest | alibaba-openshift | 🔜 待实现 |
| OADP DPA manifest（OSS backend）| alibaba-openshift | 🔜 待实现 |
| 更新 04-csi-driver-cr.yaml | alibaba-openshift | ✅ 已更新（snapshot+storageProfile+NAS+volumeMode）|
| ROS template 加 NAS 权限 | alibaba-openshift | ✅ 已有（mirror-stack NodeRamPolicy nas:*）|
| 三层镜像 build + push | alibaba-cloud-csi-operator | 🔜 待实现（CSI operator 镜像发布）|

### P1 — 验证（需要环境）

| 工作项 | 说明 |
|-------|------|
| 云盘 PVC 动态供给 | 创建 PVC → Pod 挂载 |
| NAS PVC 动态供给 | 创建 RWX PVC → 多 Pod 挂载 |
| OKV VM 启动 | 使用 Block SC 创建 VM |
| OKV VM 热迁移 | 使用 NAS SC 创建 VM，触发热迁移 |
| VM 快照 | VirtualMachineSnapshot → 验证 VolumeSnapshot 创建 |
| OADP 备份 | 触发 Schedule，验证数据写入 OSS |
| OADP 恢复 | 从 OSS 恢复 VM + PVC |

---

## 12. 风险与注意事项（更新）

| 风险 | 可能性 | 缓解措施 |
|------|--------|---------|
| OLM 安装 operator 超时 | 中 | catalog 镜像提前推送；增大 installPlanApproval timeout |
| SCC privileged 在 CSV 中声明不当 | 中 | 参考 dell-csi-operator CSV 写法；已在 clusterPermissions 中声明 |
| NAS CSI 需要 NAS 实例预先存在 | 高 | ROS template 中创建 NAS 实例和挂载点，通过 storageClass 参数引用 |
| StorageProfile patch 与 OKV 版本兼容性 | 中 | 检查目标 OCP 版本的 CDI API 版本（v1beta1 vs v1alpha1）|
| OADP OSS credentials 泄露风险 | 中 | Secret 不进 Git；ROS 输出中使用 NoEcho；考虑 RRSA（类似 IRSA）|
| NAS RWX 性能不满足高 IOPS VM 要求 | 中 | 高性能场景改用 NAS 极速型（另建 StorageClass）|
| VM 热迁移时 NAS 网络带宽成为瓶颈 | 低 | 确保 NAS 挂载点与 ECS 在同一可用区，使用 VPC 内网访问 |

---

## 13. 参考实现深度对齐（AWS ROSA + CDI）

本章记录对 AWS EBS CSI Driver Operator 和 CDI StorageProfile 机制的研究结论，
以及对本项目的具体影响和设计决策。

### 13.1 AWS EBS CSI Operator 关键发现

**来源**：`github.com/openshift/aws-ebs-csi-driver-operator`

#### CSIDriver 对象 — `seLinuxMount: true` 是必须项

AWS operator 的 `csidriver.yaml`：

```yaml
spec:
  attachRequired: true
  podInfoOnMount: false
  fsGroupPolicy: File
  seLinuxMount: true          # ← RHCOS 节点 SELinux 强制要求
  volumeLifecycleModes:
    - Persistent
```

**影响**：我们当前的 `ensureCSIDriver()` 缺少 `seLinuxMount: true`。
RHCOS 默认开启 SELinux Enforcing 模式，没有此字段会导致 CSI mount 操作被 SELinux 拒绝。
**必须修复**，在 `ensureCSIDriver()` 中加入此字段。

#### VolumeSnapshotClass — 条件创建

AWS operator 使用 `WithConditionalStaticResourcesController`，
只有当 `volumesnapshotclasses.snapshot.storage.k8s.io` CRD 存在时才创建 VolumeSnapshotClass。
创建后不再删除（`removeCondition` 返回 `false`）。

```yaml
metadata:
  annotations:
    snapshot.storage.kubernetes.io/is-default-class: "true"  # ← 标记为默认快照类
driver: ebs.csi.aws.com
deletionPolicy: Delete
```

**影响**：我们的 operator 创建 VolumeSnapshotClass 前需先用 Discovery Client 检查 CRD 是否存在，
避免在旧版 OCP（未安装 VolumeSnapshot CRD）上报错。

#### Node DaemonSet SCC — 通过 ClusterRoleBinding 绑定 privileged

AWS 不创建自定义 SCC，而是直接绑定：

```yaml
# ebs-node-privileged-binding
roleRef:
  kind: ClusterRole
  name: system:openshift:scc:privileged   # ← OCP 内置，无需自定义 SCC
subjects:
  - kind: ServiceAccount
    name: aws-ebs-csi-driver-node-sa
```

**影响**：我们已按此模式实现，确认方向正确。

#### StorageClass — `WaitForFirstConsumer` + 磁盘加密

```yaml
volumeBindingMode: WaitForFirstConsumer   # 区可用性感知，避免跨 AZ 挂载失败
parameters:
  type: gp3
  encrypted: "true"                        # 默认开启磁盘加密
```

**影响**：阿里云 ESSD 支持加密，应在 StorageClass parameters 中加入 `encrypted: "true"` 参数，
由用户通过 CR spec 控制是否开启。`WaitForFirstConsumer` 已实现。

---

### 13.2 CDI StorageProfile 机制 — 关键发现

**来源**：`github.com/kubevirt/containerized-data-importer`

#### 阿里云驱动不在 CDI 内置能力表

CDI 在 `pkg/storagecapabilities/storagecapabilities.go` 维护硬编码的驱动能力表。
**阿里云的两个驱动均不在此表中**：

```go
// CDI 内置表（摘录）
"ebs.csi.aws.com":   {{rwo, block}},
"pd.csi.storage.gke.io": {{rwo, block}, {rwx, block}},
// "diskplugin.csi.alibabacloud.com" → 不存在 ← 问题所在
// "nasplugin.csi.alibabacloud.com"  → 不存在
```

**后果**：OKV 无法自动识别阿里云 StorageClass 的正确 accessMode + volumeMode 组合，
`StorageProfile.status.conditions[Recognized]` 为 `False`，
CDI 会为 VM DataVolume 选择错误的 accessMode，导致 VM 启动失败。

#### 解决策略（双轨）

**轨道 A（短期）：operator 主动 patch StorageProfile**

CDI 的优先级规则：`spec.claimPropertySets`（用户/operator 写入）> CDI 自动检测。
我们的 operator 在创建 StorageClass 后，watch 对应 StorageProfile 并 patch spec：

```go
// operator 在 reconcile 中主动 patch
storageProfile.Spec.ClaimPropertySets = []cdiv1.ClaimPropertySet{
    {AccessModes: []v1.PersistentVolumeAccessMode{v1.ReadWriteOnce}, VolumeMode: &block},
    {AccessModes: []v1.PersistentVolumeAccessMode{v1.ReadWriteOnce}, VolumeMode: &filesystem},
}
storageProfile.Spec.CloneStrategy = &csiClone  // 支持 CSI clone
```

**轨道 B（长期）：向 CDI 上游提 PR**

向 `storagecapabilities.go` 提交 PR，注册阿里云驱动：

```go
// 待提交到 kubevirt/containerized-data-importer
"diskplugin.csi.alibabacloud.com": {
    {rwo, block},        // ESSD Block 模式（VM OS 盘最优）
    {rwo, file},         // ESSD Filesystem 模式（通用容器）
},
"nasplugin.csi.alibabacloud.com": {
    {rwx, file},         // NAS（热迁移）
    {rwo, file},         // NAS 单挂载
},
```

PR 合并后，新版 CDI 自动识别，无需 operator 手动 patch。两轨并行，上游接受后 patch 逻辑可移除。

#### Clone Strategy 决策

CDI clone strategy 优先级：
1. `StorageProfile.spec.cloneStrategy`（显式覆盖）
2. StorageClass 注解 `cdi.kubevirt.io/clone-strategy`
3. CDI 自动：有 VolumeSnapshotClass → `snapshot`；无 → `host-assisted`（慢，占带宽）

**建议**：patch StorageProfile 时设置 `cloneStrategy: csi-clone`。
阿里云云盘支持 CSI clone（`ecs:CreateDisk` + source disk），
比 `snapshot` 更快（无需创建快照再从快照恢复），比 `host-assisted` 更高效。

#### StorageProfile Reconcile 流程（operator 视角）

```
ensureDiskDriver()
  └── ensureStorageClass(sc)          // 创建 StorageClass
  └── ensureVolumeSnapshotClass()     // 条件创建（CRD 存在时）
  └── ensureStorageProfile(sc)        // patch CDI StorageProfile
        ├── Get StorageProfile（名称 == StorageClass 名称）
        ├── 如果 CDI 未创建（稍后会创建）→ 等待下次 reconcile
        └── Patch spec.claimPropertySets + spec.cloneStrategy
```

**Watch 触发**：operator 应 Watch `StorageProfile` 对象，CDI 创建 StorageProfile 后自动触发 reconcile。

---

### 13.3 OKV StorageClass 注解设计

参考 CDI 文档，OKV 使用两个独立注解：

| 注解 | 作用 | 谁写 |
|------|------|------|
| `storageclass.kubernetes.io/is-default-class: "true"` | 通用 PVC 默认 StorageClass | operator（由 `spec.disk.defaultStorageClass` 控制）|
| `storageclass.kubevirt.io/is-default-virt-class: "true"` | OKV VM 默认 StorageClass | operator（由 `spec.disk.storageClasses[].virtDefault` 控制）|

两者可以相同也可以不同，建议：
- 通用默认：`alicloud-disk-efficiency`（Filesystem，兼容性最广）
- OKV 默认：`alicloud-disk-essd-block`（Block，VM 性能最优）

---

### 13.4 对当前实现的影响（必须修复清单）

| 问题 | 严重性 | 修复内容 | 状态 |
|------|--------|---------|------|
| CSIDriver 缺 `seLinuxMount: true` | 🔴 严重 | `ensureDiskCSIDriver()` 加此字段，否则 RHCOS 上 SELinux 拒绝 mount | ✅ 已修复 |
| 无 VolumeSnapshotClass | 🔴 严重 | 条件创建（NoMatchError 时跳过），加 `is-default-class: "true"` | ✅ 已修复 |
| 无 StorageProfile patch | 🔴 严重 | `ensureStorageProfile()` patch claimPropertySets + cloneStrategy + snapshotClass | ✅ 已修复 |
| StorageClass 无 Block 变体 | 🟠 重要 | per-SC `volumeMode` 字段 + install CR 加 `alicloud-disk-essd-block`（Block + virtDefault）| ✅ 已修复（2026-06-12）|
| StorageClass 无加密参数 | 🟡 建议 | `DiskStorageClassSpec.Encrypted` → parameters `encrypted: "true"`（由 CR spec 控制）| ✅ 已实现 |
| NAS StorageClass 未实现 | 🔴 严重 | NAS controller/DaemonSet/StorageClass(Immediate) 全实现；install CR 已启用 | ✅ 已修复 |
| clone strategy 未设置 | 🟠 重要 | patch StorageProfile 时设 `csi-clone`（CR 可覆盖）| ✅ 已修复 |
| isBlock 对所有 disk SC 硬编码 true | 🟠 重要 | 改 per-SC `volumeMode`：通用盘 RWO+Filesystem / VM 盘 RWO+Block，避免通用盘被误标 Block | ✅ 已修复（2026-06-12）|
| CDI 上游无阿里云能力条目 | 🟡 长期 | 向 kubevirt/containerized-data-importer 提 PR（轨道 B，patch 为短期轨道 A）| 🔜 待提 PR |

---

## 14. 上游项目与参考

- [alibaba-cloud-csi-driver](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver) — v1.35.3
- [operator-sdk 文档](https://sdk.operatorframework.io/docs/building-operators/golang/quickstart/)
- [OLM Bundle 格式规范](https://olm.operatorframework.io/docs/tasks/creating-operator-bundle/)
- [OADP 文档](https://docs.openshift.com/container-platform/latest/backup_and_restore/application_backup_and_restore/oadp-intro.html)
- [Velero S3 兼容存储配置](https://velero.io/docs/main/supported-providers/)
- [OpenShift Virtualization 存储推荐](https://docs.openshift.com/container-platform/latest/virt/storage/virt-storage-config-overview.html)
- [StorageProfile API](https://containerized-data-importer.github.io/containerized-data-importer/api.html)
- [KubeVirt CDI 存储配置](https://github.com/kubevirt/containerized-data-importer/blob/main/doc/storageprofile.md)
