# Post-install components (CAPA / CSI / OADP)

`ansible/playbooks/08-deploy-post-install.yml` 跑在跳板上，部署"装完
cluster 之后才能装"的组件 —— 这些组件需要 `oc` + 工作中的 cluster API
endpoint，而 Phase 07 结束时 kubeconfig 刚好被 scp 到跳板。

> 本文档讲**实际部署路径**和操作步骤。CSI 的架构 / 选型 / 设计
> 原理见 [`docs/csi-driver-design.md`](csi-driver-design.md)（v0.4 设计文档，
> 含与 AWS ROSA EBS CSI Operator 对比、CDI StorageProfile 机制等）。

## 当前部署的组件

| 组件 | 由 08 自动部署？ | 入口 |
|---|---|---|
| **CAPA**（Cluster API Provider Alicloud）| ✅ | `08-deploy-post-install.yml` "CAPI provider" block |
| **CSI driver operator** | ✅ | `08-deploy-post-install.yml` "CSI operator" block |
| **OADP**（OpenShift API for Data Protection）| ❌ 手动 | `custom_manifests/05-oadp-*.yaml`，oc apply 三件套 |

`custom_manifests/04-csi-{catalogsource,operatorgroup,subscription}.yaml`
存在但**当前未被 08 引用** —— 这是 `csi-driver-design.md` 描述的 OLM-based
未来形态；今天 08 走的是"sibling 仓库 + kustomize render"路径。详见下面
"CSI 部署路径不一致"。

## 1. CAPA — Cluster API Provider Alicloud

部署在 `capa-system` namespace。08 的流程：

```
1. oc apply -f ../../openshift-capi-alicloud/config/crd/bases/    # CRD（来自 sibling repo）
2. sed image override < custom_manifests/02-capa-controller.yaml | oc apply -f -
```

依赖 sibling 仓库 `openshift-capi-alicloud/` 提供 CRD 定义。镜像默认：
`quay.io/samzhu/openshift-capi-alicloud:v0.1.0`（在 08 playbook 顶部的
`capi_provider_img` 变量里）。

仓库布局：

```
~/openshift-alibaba/
├── alibaba-openshift/                     ← 本仓库（自动化 + 文档）
├── openshift-capi-alicloud/               ← CAPA provider 源码（CRD + controller）
└── alibaba-cloud-csi-operator/            ← CSI operator 源码（kustomize 入口）
```

跳板的 `home` 下必须有这三个仓库 clone（mirror-stack 的 jump host
cloud-init 会自动 clone，手工跑时自己 git clone 即可）。

### 验证

```sh
oc get pods -n capa-system
oc get crds | grep capa
```

### 用法示例

把 `examples/capi-machinedeployment.yaml`（如存在）apply 上去，CAPA
controller 会按 spec 起 ECS 工作节点并加入 cluster。

## 2. CSI driver — Alibaba Cloud CSI Operator

部署在 `alibaba-cloud-csi-operator-system` namespace（operator）+
`kube-system` namespace（DaemonSet / Deployment）。08 的流程：

```
1. kustomize edit set image controller=<csi_operator_img>           # in sibling repo
2. kustomize build ../../alibaba-cloud-csi-operator/config/default | oc apply -f -
3. oc wait --for=condition=Established crd/alibabacloudcsidrivers.csi.alibabacloud.com
4. oc apply -f custom_manifests/04-csi-driver-cr.yaml               # singleton CR
```

镜像默认：`quay.io/samzhu/alibaba-cloud-csi-operator:v1.35.3`。

### CSI 部署路径不一致（已知 TODO）

`custom_manifests/04-csi-*.yaml` 里有四个文件：

| 文件 | 当前是否被 08 引用 |
|---|---|
| `04-csi-driver-cr.yaml` | ✅ 是（步骤 4） |
| `04-csi-catalogsource.yaml` | ❌ 否 |
| `04-csi-operatorgroup.yaml` | ❌ 否 |
| `04-csi-subscription.yaml` | ❌ 否 |

后三个文件描述了**未来 OLM-based 部署形态**（对应 `csi-driver-design.md`
v0.2+ 的方案）。今天 08 走的是 kustomize-from-sibling-repo 路径，二者
**不能混用**。

迁移到 OLM 路径需要：(a) 把 operator bundle 推到一个 catalog；(b) 在 04
playbook 把 mirror catalog 也推过去（disconnected 装时）；(c) 把 08 改成
apply 这三个 OLM manifest 而非 kustomize render。目前没排期。

### 验证

```sh
oc get pods -n alibaba-cloud-csi-operator-system
oc get pods -n kube-system | grep csi
oc get alibabacloudcsidriver cluster -o yaml
oc get storageclass | grep alicloud           # 预期至少有 alicloud-disk / alicloud-nas
```

PVC smoke-test：

```sh
cat <<'EOF' | oc apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-disk
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: alicloud-disk
  resources:
    requests:
      storage: 20Gi
EOF
oc get pvc test-disk -w        # 等 Bound（CSI 会动态 provision 一块 ESSD）
```

## 3. OADP — backup / restore to Alibaba OSS

OADP（Velero 的 OCP 封装）目前**不被 08 自动部署**，原因：需要先在
阿里云创建专用 OSS bucket 和最小权限 RAM 用户，凭证不能 hardcode 进
仓库。三个文件按顺序手动 apply：

| 文件 | 内容 |
|---|---|
| `custom_manifests/05-oadp-subscription.yaml` | OLM Subscription 装 OADP operator（拉起 Velero） |
| `custom_manifests/05-oadp-oss-credentials.yaml` | **模板** —— Velero 用的 OSS AK/SK Secret，apply 前必须填实际值 |
| `custom_manifests/05-oadp-dpa.yaml` | DataProtectionApplication CR —— Velero 主配置（OSS bucket / endpoint / 插件 / kopia uploader） |

### 一次性准备

1. **OSS bucket**（与 cluster 同 region，避免跨 region 流量费）：
   ```sh
   aliyun oss mb oss://<your-backup-bucket> --region <region> --profile <p>
   ```
2. **RAM 用户 + 最小权限策略**（见 `05-oadp-oss-credentials.yaml` 头部注释里的
   策略 JSON）。**不要复用节点 instance RAM Role 的凭证**。
3. 改 `05-oadp-oss-credentials.yaml` 的 `<YOUR-OSS-ACCESS-KEY-ID>` /
   `<YOUR-OSS-ACCESS-KEY-SECRET>` 占位符。
4. 改 `05-oadp-dpa.yaml` 的 `bucket` / `region` / `endpoint`（**内网** endpoint：
   `oss-<region>-internal.aliyuncs.com`）。

### 部署

```sh
oc apply -f custom_manifests/05-oadp-subscription.yaml
# 等 operator 起来
oc wait --for=condition=Available deployment/openshift-adp-controller-manager \
  -n openshift-adp --timeout=300s
oc apply -f custom_manifests/05-oadp-oss-credentials.yaml
oc apply -f custom_manifests/05-oadp-dpa.yaml
```

### 验证

```sh
oc get pods -n openshift-adp
oc get dataprotectionapplication -n openshift-adp
oc get backupstoragelocation -n openshift-adp     # 应为 Available
```

Smoke-test backup：

```sh
cat <<'EOF' | oc apply -f -
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: smoke-test
  namespace: openshift-adp
spec:
  includedNamespaces:
    - default
  storageLocation: default
  ttl: 24h0m0s
EOF
oc get backup -n openshift-adp smoke-test -o yaml | grep phase   # 等 Completed
```

## 4. butane sources

`custom_manifests/butane/` 是空目录占位符。原计划放
`00-ovn-mtu.yaml` / `03-machineconfig-providerid-*.yaml` 等
MachineConfig 的 butane 源（YAML 写起来比纯 ignition JSON 直观）。
当前所有 MachineConfig 都是手写的 `MachineConfig` 资源 + `contents.source`
data URI（不是 inline；inline 在 MCO render 阶段会被静默丢弃 —— 历史教训）。

如果将来要补 butane 源：

```sh
# 安装 butane
go install github.com/coreos/butane/cmd/butane@latest

# 编辑 .bu，编译成 MachineConfig
butane --pretty --strict --output ../03-machineconfig-providerid-master.yaml \
  03-machineconfig-providerid-master.bu
```

## See also

- [`docs/csi-driver-design.md`](csi-driver-design.md) — CSI driver 选型 /
  架构设计 / 与 AWS ROSA EBS CSI Operator 对齐 / CDI StorageProfile 机制
- [`docs/CCM.md`](CCM.md) — CCM 跟 CSI / OADP 都不直接交互，但 LoadBalancer
  Service 是它管的（OADP backup web UI 等会走它）
- `ansible/playbooks/08-deploy-post-install.yml` — 实际部署代码
- `examples/` 目录（如存在）—— CAPA MachineDeployment 等示例
