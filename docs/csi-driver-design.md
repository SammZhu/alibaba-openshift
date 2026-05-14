# Alibaba Cloud CSI Driver 集成设计

**状态**：规划中  
**版本**：v0.2（改用 OLM Operator 方案）  
**日期**：2026-05-14  
**阶段**：Phase 1 扩展（存储支持）

---

## 1. 背景与目标

OpenShift 在 External Platform 模式下运行时，OCP 不会自动安装任何云厂商的 CSI driver。
集群没有 CSI driver 意味着：

- 无法使用 `PersistentVolumeClaim`（PVC 无法动态供给）
- Operator 及工作负载若依赖持久存储则无法正常运行
- 无法使用 VolumeSnapshot 做应用级备份

**目标**：通过 OLM（Operator Lifecycle Manager）安装 Alibaba Cloud CSI Driver Operator，
在 install-time 完成注入，使集群具备云盘动态供给能力，并支持后续版本升级管理。

### 1.1 为什么选择 OLM 而非 Helm / 静态 YAML

| 维度 | 静态 YAML | Helm | OLM Operator |
|------|-----------|------|--------------|
| 版本升级 | 手动替换文件 | helm upgrade | Subscription 自动 |
| OpenShift 集成度 | 无 | 无 | OperatorHub UI、Install Plan |
| 生命周期管理 | 无 | 有限 | 完整（install/upgrade/uninstall） |
| 与 OCP 标准对齐 | 否 | 否 | 是 |
| install-time 注入 | 可行 | 可行 | 可行（CatalogSource + Subscription）|

### 1.2 范围

| 存储类型 | 优先级 | 说明 |
|---------|--------|------|
| 云盘（Disk/EBS） | **P1（本期）** | 块存储，ReadWriteOnce |
| NAS 文件存储 | P2 | 共享文件存储，ReadWriteMany |
| OSS 对象存储 | P3 | 对象存储挂载，特殊场景 |

---

## 2. 上游项目

- **alibaba-cloud-csi-driver**：https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver
- 最新版本：v1.35.3（2026-05-13，活跃维护）
- 维护方：kubernetes-sigs（CNCF）+ 阿里云官方
- 提供 Helm Chart，无官方 OLM bundle（需自行构建）

---

## 3. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│  quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest   │
│  （OLM Catalog Image）                                       │
│    └── alibaba-cloud-csi-operator Bundle v1.35.x            │
└─────────────────────────────────────────────────────────────┘
              ↓  CatalogSource（install-time 注入）
┌─────────────────────────────────────────────────────────────┐
│  OLM（已内置于 OpenShift）                                    │
│    ├── CatalogSource → 发现 operator                         │
│    ├── Subscription  → 订阅并触发安装                         │
│    └── InstallPlan   → 自动执行                              │
└─────────────────────────────────────────────────────────────┘
              ↓  部署 operator Pod
┌─────────────────────────────────────────────────────────────┐
│  alibaba-cloud-csi-operator（Go operator，namespace: kube-system）│
│    监听 CR：AlibabaCloudCSIDriver                             │
└─────────────────────────────────────────────────────────────┘
              ↓  reconcile CR 实例
┌─────────────────────────────────────────────────────────────┐
│  CSI Driver 组件（由 operator 管理）                          │
│    ├── CSIDriver 对象（diskplugin.csi.alibabacloud.com）      │
│    ├── Controller Deployment（2 副本，控制面节点）             │
│    ├── Node DaemonSet（全节点）                               │
│    ├── RBAC + SCC binding                                    │
│    └── StorageClass（alicloud-disk-efficiency 等）           │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 新工程：alibaba-cloud-csi-operator

### 4.1 仓库规划

- **仓库**：`SammZhu/alibaba-cloud-csi-operator`
- **语言**：Go（operator-sdk v1.x）
- **镜像**：`quay.io/samzhu/alibaba-cloud-csi-operator:v1.35.x`
- **Bundle 镜像**：`quay.io/samzhu/alibaba-cloud-csi-operator-bundle:v1.35.x`
- **Catalog 镜像**：`quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest`

### 4.2 Custom Resource：AlibabaCloudCSIDriver

```yaml
apiVersion: csi.alibabacloud.com/v1alpha1
kind: AlibabaCloudCSIDriver
metadata:
  name: cluster          # 单例，名称固定为 cluster
  namespace: kube-system
spec:
  # 存储类型开关
  disk:
    enabled: true
    defaultStorageClass: true    # 设为默认 StorageClass
    storageClasses:
      - name: alicloud-disk-efficiency
        type: cloud_efficiency
        reclaimPolicy: Delete
        allowVolumeExpansion: true
      - name: alicloud-disk-essd
        type: cloud_essd
        reclaimPolicy: Delete
        allowVolumeExpansion: true
  nas:
    enabled: false               # Phase 1 不启用
  oss:
    enabled: false               # Phase 1 不启用

  # CSI driver 镜像版本
  imageTag: v1.35.3

  # 认证：使用 RAM Role Instance Principal，无需 AK/SK
  auth:
    ramToken: v2                 # ECS instance metadata token

  # Controller 调度到控制面节点
  controller:
    replicas: 2
    nodeSelector:
      node-role.kubernetes.io/master: ""
    tolerations:
      - key: node-role.kubernetes.io/master
        effect: NoSchedule
```

### 4.3 工程目录结构

```
alibaba-cloud-csi-operator/
├── api/
│   └── v1alpha1/
│       ├── alibabacloudcsidriver_types.go   # CR 类型定义
│       └── zz_generated.deepcopy.go
├── internal/
│   └── controller/
│       └── alibabacloudcsidriver_controller.go  # reconcile 逻辑
├── config/
│   ├── crd/                    # CRD manifests
│   ├── rbac/                   # operator 自身 RBAC
│   ├── manager/                # operator Deployment
│   └── samples/                # CR 示例
├── bundle/
│   ├── manifests/
│   │   ├── alibaba-cloud-csi-operator.clusterserviceversion.yaml
│   │   └── csi.alibabacloud.com_alibabacloudcsidrivers.yaml
│   └── metadata/
│       └── annotations.yaml
├── Dockerfile                  # operator 镜像
├── bundle.Dockerfile           # bundle 镜像
└── Makefile
```

### 4.4 Reconcile 逻辑（controller 核心）

```
AlibabaCloudCSIDriver CR
  ↓
reconcile()
  ├── 确保 CSIDriver 对象存在
  ├── 确保 RBAC（ServiceAccount、ClusterRole、ClusterRoleBinding）
  ├── 确保 SCC privileged binding
  ├── 如果 spec.disk.enabled：
  │     ├── 确保 Controller Deployment（csi-provisioner + csi-attacher sidecar）
  │     ├── 确保 Node DaemonSet（csi-plugin + node-driver-registrar sidecar）
  │     └── 确保 StorageClass（按 spec.disk.storageClasses 列表）
  ├── 如果 spec.nas.enabled：（Phase 2）
  │     └── ...
  └── 更新 CR Status（Ready、ComponentsReady 等）
```

---

## 5. 认证方案

### RAM Role Instance Principal（ramToken: v2）

节点已通过 ROS template 绑定 RAM Role，operator 部署的 CSI driver 通过 ECS instance metadata 自动获取临时 Token，**无需 AK/SK**。

需在 ROS template 的 `NodeRamRole` 追加磁盘权限：

```json
{
  "Statement": [
    {
      "Action": [
        "ecs:AttachDisk",
        "ecs:DetachDisk",
        "ecs:DescribeDisks",
        "ecs:CreateDisk",
        "ecs:DeleteDisk",
        "ecs:ResizeDisk",
        "ecs:CreateSnapshot",
        "ecs:DeleteSnapshot",
        "ecs:DescribeSnapshots",
        "ecs:CreateAutoSnapshotPolicy",
        "ecs:ApplyAutoSnapshotPolicy",
        "ecs:DeleteAutoSnapshotPolicy",
        "ecs:DescribeAutoSnapshotPolicyEx",
        "ecs:ModifyDiskAttribute"
      ],
      "Effect": "Allow",
      "Resource": "*"
    }
  ],
  "Version": "1"
}
```

---

## 6. OpenShift 特殊处理

### 6.1 SCC（Security Context Constraints）

CSI Node DaemonSet 需要访问 `/dev`、执行 `mount`，必须绑定 `privileged` SCC。
Operator reconcile 时自动创建 ClusterRoleBinding：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: alibabacloud-csi-privileged
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: system:openshift:scc:privileged
subjects:
  - kind: ServiceAccount
    name: alibaba-cloud-csi-sa
    namespace: kube-system
```

CSV 中需在 `spec.install.spec.clusterPermissions` 声明该权限，OLM 才允许 operator 执行此操作。

### 6.2 cluster-storage-operator

External Platform 下 `cluster-storage-operator` 处于 `Unmanaged` 状态，不会冲突。

### 6.3 kubelet Root Dir

OpenShift 默认 `/var/lib/kubelet`，与 CSI driver 默认值一致，无需修改。

---

## 7. Install-time 注入（custom_manifests）

OLM 方案下，install-time 注入的是 OLM 对象而非 CSI 组件本身：

```
custom_manifests/
  03-csi-namespace.yaml          # 确保 kube-system（通常已存在，可省略）
  03-csi-operatorgroup.yaml      # OperatorGroup
  03-csi-catalogsource.yaml      # 指向 catalog 镜像
  03-csi-subscription.yaml       # Subscription（触发 OLM 安装 operator）
  03-csi-instance.yaml           # AlibabaCloudCSIDriver CR（触发 operator reconcile）
```

### 7.1 各文件内容

```yaml
# 03-csi-catalogsource.yaml
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: alibaba-cloud-csi-catalog
  namespace: openshift-marketplace
spec:
  sourceType: grpc
  image: quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest
  displayName: Alibaba Cloud CSI Driver
  publisher: SammZhu
  updateStrategy:
    registryPoll:
      interval: 24h
---
# 03-csi-operatorgroup.yaml
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: alibaba-cloud-csi-og
  namespace: kube-system
spec:
  targetNamespaces:
    - kube-system
---
# 03-csi-subscription.yaml
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: alibaba-cloud-csi-operator
  namespace: kube-system
spec:
  channel: stable
  name: alibaba-cloud-csi-operator
  source: alibaba-cloud-csi-catalog
  sourceNamespace: openshift-marketplace
  installPlanApproval: Automatic
---
# 03-csi-instance.yaml
apiVersion: csi.alibabacloud.com/v1alpha1
kind: AlibabaCloudCSIDriver
metadata:
  name: cluster
  namespace: kube-system
spec:
  disk:
    enabled: true
    defaultStorageClass: true
    storageClasses:
      - name: alicloud-disk-efficiency
        type: cloud_efficiency
        reclaimPolicy: Delete
        allowVolumeExpansion: true
      - name: alicloud-disk-essd
        type: cloud_essd
        reclaimPolicy: Delete
        allowVolumeExpansion: true
  nas:
    enabled: false
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

## 8. 构建流程

```bash
# 1. 初始化 operator 工程（在新目录中执行）
operator-sdk init \
  --domain csi.alibabacloud.com \
  --repo github.com/SammZhu/alibaba-cloud-csi-operator

operator-sdk create api \
  --group csi \
  --version v1alpha1 \
  --kind AlibabaCloudCSIDriver \
  --resource --controller

# 2. 构建 operator 镜像
make docker-build docker-push \
  IMG=quay.io/samzhu/alibaba-cloud-csi-operator:v1.35.3

# 3. 生成 OLM bundle
make bundle \
  IMG=quay.io/samzhu/alibaba-cloud-csi-operator:v1.35.3 \
  VERSION=1.35.3 \
  CHANNELS=stable \
  DEFAULT_CHANNEL=stable

# 4. 构建并推送 bundle 镜像
make bundle-build bundle-push \
  BUNDLE_IMG=quay.io/samzhu/alibaba-cloud-csi-operator-bundle:v1.35.3

# 5. 构建 catalog 镜像（使用 opm）
opm index add \
  --bundles quay.io/samzhu/alibaba-cloud-csi-operator-bundle:v1.35.3 \
  --tag quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest

podman push quay.io/samzhu/alibaba-cloud-csi-operator-catalog:latest
```

---

## 9. 实施步骤

```
Step 1  创建新工程 alibaba-cloud-csi-operator           （无需环境）
          operator-sdk init + create api
          定义 AlibabaCloudCSIDriver CR types
          实现 reconcile 逻辑（创建 CSI 组件）
          
Step 2  编写 CSV ClusterServiceVersion                  （无需环境）
          声明 clusterPermissions（含 SCC privileged）
          描述 owned CRDs 和 required CRDs
          
Step 3  构建三层镜像                                     （无需环境）
          operator 镜像 → bundle 镜像 → catalog 镜像
          推送到 quay.io/samzhu/
          
Step 4  更新 ROS template 追加磁盘 IAM Policy            （无需环境）

Step 5  编写 custom_manifests 中的 OLM 对象              （无需环境）
          CatalogSource / OperatorGroup / Subscription / CR

Step 6  更新安装文档和 validation checklist              （无需环境）
────────────────────────────────────────────────────────────
Step 7  阿里云环境就绪后：端到端安装验证                   （需要环境）
Step 8  PVC 动态供给功能验证                              （需要环境）
Step 9  升级路径验证（更新 Subscription channel）         （需要环境）
```

**Step 1-6 全部可在等待环境期间完成。**

---

## 10. 版本管理策略

| operator 版本 | 对应 CSI driver 版本 | OCP 版本 |
|--------------|---------------------|---------|
| v1.35.3      | v1.35.3             | 4.14+   |

Operator channel 设计：
- `stable`：当前稳定版，自动升级补丁版本
- 大版本升级通过修改 `Subscription.spec.channel` 手动触发

---

## 11. 风险与注意事项

| 风险 | 可能性 | 缓解措施 |
|------|--------|---------|
| OLM 安装 operator 超时，install-time 等待失败 | 中 | 配置足够的 Subscription timeout；catalog 镜像提前推送 |
| SCC privileged 在 CSV 中声明不当被 OLM 拒绝 | 中 | 参考 dell-csi-operator CSV 的 clusterPermissions 写法 |
| CSI driver 版本与 RHCOS 内核模块不兼容 | 低 | 固定 imageTag，验证后再升级 |
| RAM Role 权限不够导致磁盘操作失败 | 中 | Step 4 提前在 ROS template 中补全 Action 列表 |

---

## 12. 参考资料

- [alibaba-cloud-csi-driver](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver)
- [operator-sdk 官方文档](https://sdk.operatorframework.io/docs/building-operators/golang/quickstart/)
- [OLM Bundle 格式规范](https://olm.operatorframework.io/docs/tasks/creating-operator-bundle/)
- [beegfs-csi-driver-operator CSV 参考](https://github.com/redhat-openshift-ecosystem/community-operators-prod/tree/main/operators/beegfs-csi-driver-operator)
- [dell-csi-operator CSV 参考（含 SCC）](https://github.com/redhat-openshift-ecosystem/community-operators-prod/tree/main/operators/dell-csi-operator)
- [OCI OpenShift CSI 注入参考](https://github.com/oracle-quickstart/oci-openshift/tree/main/custom_manifests)
