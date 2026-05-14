# Alibaba Cloud CSI Driver 集成设计

**状态**：规划中  
**版本**：v0.1  
**日期**：2026-05-14  
**阶段**：Phase 1 扩展（存储支持）

---

## 1. 背景与目标

OpenShift 在 External Platform 模式下运行时，OCP 不会自动安装任何云厂商的 CSI driver。
集群没有 CSI driver 意味着：

- 无法使用 `PersistentVolumeClaim`（PVC 无法动态供给）
- Operator 及工作负载若依赖持久存储则无法正常运行
- 无法使用 VolumeSnapshot 做应用级备份

**目标**：在 install-time 注入 Alibaba Cloud CSI Driver，使集群具备云盘（EBS 等价）动态供给能力，无需 Day 2 手动操作。

### 1.1 范围

| 存储类型 | 优先级 | 说明 |
|---------|--------|------|
| 云盘（Disk/EBS） | **P1（本期）** | 最常用，块存储，ReadWriteOnce |
| NAS 文件存储 | P2 | 共享文件存储，ReadWriteMany |
| OSS 对象存储 | P3 | 对象存储挂载，特殊场景 |

---

## 2. 上游项目分析

### 2.1 alibaba-cloud-csi-driver

- **仓库**：https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver
- **最新版本**：v1.35.3（2026-05-13 推送，非常活跃）
- **维护方**：kubernetes-sigs（CNCF 托管）+ 阿里云官方维护
- **部署方式**：Helm Chart（`deploy/charts/alibaba-cloud-csi-driver/`）
- **组件**：
  - `csi-provisioner`：Controller 插件（Deployment，2 副本）
  - `csi-plugin`：Node 插件（DaemonSet，每节点一个 Pod）
  - CSIDriver 对象（集群级）
  - RBAC（ServiceAccount、ClusterRole、ClusterRoleBinding）
  - StorageClass（默认云盘）
  - VolumeSnapshotClass

### 2.2 参考实现：OCI OpenShift

`oracle-quickstart/oci-openshift` 的做法：

```
custom_manifests/
  01-oci-ccm.yml          # CCM 部署
  01-oci-csi.yml          # CSI driver 部署（预渲染，非 Helm）
  01-oci-driver-configs.yml  # ConfigMap（cloud.conf 等）
  02-machineconfig-ccm.yml   # kubelet 配置
  02-machineconfig-csi.yml   # CSI 相关内核模块加载
```

**关键设计决策**：OCI 预先渲染 Helm chart 为静态 YAML，通过 custom_manifests 在 install-time 注入，不依赖集群上的 Helm。

---

## 3. 认证方案

### 3.1 方案对比

| 方案 | 适用场景 | 安全性 | 配置复杂度 |
|------|---------|--------|-----------|
| AK/SK（密钥） | 非 ECS 节点（外部注册集群） | 低，密钥需轮换 | 需要 Secret |
| RAM Role Instance Principal | ECS 节点（本方案） | 高，无密钥 | 零配置 |

### 3.2 本方案选择：RAM Role Instance Principal（ramToken: v2）

节点已通过 ROS template 绑定了 RAM Role（`NodeRamRole`），CSI driver 通过 ECS instance metadata 自动获取临时 Token，无需任何 AK/SK。

RAM Role 需要追加以下 Policy（在现有 `NodeRamRole` 基础上）：

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
        "ecs:CancelAutoSnapshotPolicy",
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

## 4. OpenShift 特殊处理

### 4.1 Security Context Constraints（SCC）

CSI Node Plugin（DaemonSet）需要 `privileged` SCC 才能挂载块设备、访问 `/dev`。

需要添加：
```yaml
# 绑定 privileged SCC 到 CSI ServiceAccount
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

### 4.2 kubelet Root Dir

OpenShift 默认 kubelet root dir 为 `/var/lib/kubelet`，与 CSI driver 默认值一致，无需修改。

### 4.3 节点标签

CSI driver 默认通过节点标签做亲和性调度。ECS 节点由 CCM 自动打标签，无需额外处理。

### 4.4 与 OCP Storage Operator 的关系

External Platform 模式下，`cluster-storage-operator` 处于 `Unmanaged` 状态，不会与手动安装的 CSI driver 冲突。

---

## 5. 部署架构

```
install-time（custom_manifests 注入）
│
├── 03-alibabacloud-csi-rbac.yaml        # ServiceAccount + RBAC + SCC binding
├── 03-alibabacloud-csi-driver.yaml      # CSIDriver 对象
├── 03-alibabacloud-csi-controller.yaml  # Controller Deployment（2 副本，控制面节点）
├── 03-alibabacloud-csi-node.yaml        # Node DaemonSet（全节点）
├── 03-alibabacloud-csi-storageclass.yaml # 默认 StorageClass（云盘）
└── 03-alibabacloud-csi-snapshotclass.yaml # VolumeSnapshotClass（可选）
```

**命名前缀 `03-`**：依照 OCI 的数字前缀惯例，确保在 CCM（`01-`）和 MachineConfig（`02-`）之后应用。

### 5.1 Controller 部署策略

CSI Controller（provisioner）调度到**控制面节点**，理由：
- 控制面节点是长期存在的稳定节点
- 避免 Worker 节点滚动更新时 Controller 中断
- 与 OCI 的做法一致

```yaml
nodeSelector:
  node-role.kubernetes.io/master: ""
tolerations:
  - key: node-role.kubernetes.io/master
    effect: NoSchedule
```

---

## 6. Helm Chart 渲染策略

由于 custom_manifests 是静态 YAML，需要预先渲染 Helm chart。

### 6.1 渲染命令

```bash
helm template alibabacloud-csi \
  kubernetes-sigs/alibaba-cloud-csi-driver \
  --version 1.35.3 \
  --namespace kube-system \
  --set deploy.ramToken=v2 \
  --set deploy.ecs=true \
  --set csi.disk.enabled=true \
  --set csi.nas.enabled=false \
  --set csi.oss.enabled=false \
  --set controller.replicas=2 \
  --set defaultStorageClass.enabled=true \
  --set defaultVolumeSnapshotClass.enabled=false \
  -f values-openshift.yaml \
  > custom_manifests/03-alibabacloud-csi.yaml
```

### 6.2 values-openshift.yaml（OpenShift 覆盖值）

```yaml
# OpenShift 特定覆盖
deploy:
  ramToken: v2          # 使用 RAM Role Instance Principal
  ecs: true             # 节点在 ECS 上运行

csi:
  disk:
    enabled: true
  nas:
    enabled: false      # Phase 1 不启用
  oss:
    enabled: false      # Phase 1 不启用

controller:
  replicas: 2
  nodeSelector:
    node-role.kubernetes.io/master: ""
  tolerations:
    - key: node-role.kubernetes.io/master
      effect: NoSchedule

defaultStorageClass:
  enabled: true

# OpenShift 不使用 Prometheus Operator，禁用监控
monitoring:
  plugin:
    enabled: false
  controller:
    enabled: false
```

---

## 7. ROS Template 变更

需要在现有 ROS template 的 `NodeRamRole` 中追加磁盘权限，新增一个 Policy Statement：

```yaml
# 在 create-cluster.yaml 中追加
NodeRamRoleDiskPolicy:
  Type: ALIYUN::RAM::ManagedPolicy
  Properties:
    PolicyName:
      Fn::Sub: "${ClusterName}-node-disk-policy"
    PolicyDocument:
      Version: "1"
      Statement:
        - Action:
            - ecs:AttachDisk
            - ecs:DetachDisk
            - ecs:DescribeDisks
            - ecs:CreateDisk
            - ecs:DeleteDisk
            - ecs:ResizeDisk
            - ecs:CreateSnapshot
            - ecs:DeleteSnapshot
            - ecs:DescribeSnapshots
          Effect: Allow
          Resource: "*"

NodeDiskPolicyAttachment:
  Type: ALIYUN::RAM::AttachPolicyToRole
  Properties:
    PolicyType: Custom
    PolicyName:
      Fn::GetAtt: [NodeRamRoleDiskPolicy, PolicyName]
    RoleName:
      Fn::GetAtt: [NodeRamRole, RoleName]
  DependsOn:
    - NodeRamRoleDiskPolicy
    - NodeRamRole
```

---

## 8. 验证计划

### 8.1 功能验证

```bash
# 1. 检查 CSI driver 注册
oc get csidriver diskplugin.csi.alibabacloud.com

# 2. 检查 Controller Pod 运行状态
oc get pods -n kube-system -l app=csi-provisioner

# 3. 检查 Node DaemonSet 状态
oc get pods -n kube-system -l app=csi-plugin

# 4. 动态供给测试
cat <<EOF | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-pvc
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
  storageClassName: alicloud-disk-efficiency
EOF

# 5. 确认 PVC Bound
oc get pvc test-pvc
# 预期：STATUS=Bound

# 6. 确认云盘已创建
oc get pv $(oc get pvc test-pvc -o jsonpath='{.spec.volumeName}') -o jsonpath='{.spec.csi.volumeHandle}'
# 用该 ID 在阿里云控制台确认磁盘存在
```

### 8.2 StorageClass 设计

| StorageClass 名称 | 磁盘类型 | 性能 | 用途 |
|------------------|---------|------|------|
| `alicloud-disk-efficiency`（默认） | cloud_efficiency | 通用 | 普通工作负载 |
| `alicloud-disk-essd` | cloud_essd | 高性能 | 数据库、etcd |
| `alicloud-disk-essd-auto` | cloud_auto | 弹性 | 动态业务 |

---

## 9. 实施步骤

```
Step 1  更新 ROS template 追加磁盘 IAM Policy        （无需环境，可立即做）
Step 2  编写 values-openshift.yaml                  （无需环境，可立即做）
Step 3  渲染 Helm chart → 生成静态 YAML              （无需环境，可立即做）
Step 4  添加 SCC ClusterRoleBinding                  （无需环境，可立即做）
Step 5  整合到 custom_manifests/，更新安装文档         （无需环境，可立即做）
────────────────────────────────────────────────────
Step 6  阿里云环境就绪后：端到端安装验证               （需要环境）
Step 7  PVC 动态供给功能验证                          （需要环境）
Step 8  VolumeSnapshot 验证（可选）                   （需要环境）
```

**Step 1-5 均可在等待环境期间完成。**

---

## 10. 风险与注意事项

| 风险 | 可能性 | 缓解措施 |
|------|--------|---------|
| CSI driver 版本与 OpenShift 内核不兼容 | 中 | 参考阿里云 ACK 支持的 OpenShift 版本矩阵；从保守版本开始 |
| SCC 权限不足导致 Node Plugin 无法启动 | 高 | 验证阶段优先检查 SCC 绑定 |
| 控制面节点磁盘 Controller 单点 | 低 | 2 副本 + 3 控制面节点，Lease 选举保证高可用 |
| RAM Role 权限不够导致 Attach 失败 | 中 | 提前在 ROS template 中补全所需 Action |

---

## 11. 参考资料

- [alibaba-cloud-csi-driver](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver)
- [Helm Chart values.yaml](https://github.com/kubernetes-sigs/alibaba-cloud-csi-driver/blob/master/deploy/charts/alibaba-cloud-csi-driver/values.yaml)
- [OCI OpenShift CSI 参考实现](https://github.com/oracle-quickstart/oci-openshift/blob/main/custom_manifests/oci-ccm-csi-drivers/)
- [OpenShift External Platform 文档](https://docs.openshift.com/container-platform/latest/installing/installing_platform_agnostic/installing-platform-agnostic.html)
- [阿里云 ECS Disk Action 列表](https://help.aliyun.com/document_detail/25397.html)
