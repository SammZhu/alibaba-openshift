# 端到端验证 Checklist

首次在真实阿里云环境验证时，按此清单逐项确认。每一项标注了验证方法和预期结果。

---

## 阶段 0 — 镜像导入

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 0.1 | ISO 已上传到 OSS | OSS 控制台查看 bucket | 文件存在，大小与本地一致 |
| 0.2 | 自定义镜像导入成功 | ECS 控制台 → 镜像 → 自定义镜像 | 状态为"可用"，架构为 x86_64 |
| 0.3 | 记录 Image ID | 镜像详情页 | 格式 `m-bp1xxxxxxxxxxxxxxxxx` |

---

## 阶段 1 — ROS 栈创建

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 1.1 | 栈创建成功 | ROS 控制台 → 栈列表 | 状态为"创建完成" |
| 1.2 | VPC 和 VSwitch 已创建 | VPC 控制台 | 3 个 VSwitch（2 私有 + 1 公有） |
| 1.3 | NAT 网关 + EIP 已创建并绑定 | VPC → NAT 网关 | 状态"可用"，EIP 已关联 |
| 1.4 | SNAT 规则存在（2 条） | NAT 网关 → SNAT 列表 | 2 条规则，分别对应两个私有 VSwitch |
| 1.5 | 安全组规则正确 | ECS → 安全组 → 入方向规则 | master-sg: 6443/22623/2379-2380/all-intra；worker-sg: all-from-master/intra-worker/80/443 |
| 1.6 | RAM Role 已创建且 Policy 已附加 | RAM 控制台 → 角色 | 角色存在，授权策略已关联 |
| 1.7 | API SLB 已创建（内网） | SLB 控制台 | 类型"私网"，监听 6443 和 22623 |
| 1.8 | SLB 后端服务器已注册（3 台 master） | SLB → 后端服务器 | 3 台 ECS，权重 100 |
| 1.9 | PrivateZone 已创建并绑定 VPC | PrivateZone 控制台 | Zone 名称 `<cluster>.<domain>`，已绑定 VPC |
| 1.10 | DNS 记录正确 | PrivateZone → 解析记录 | `api` 和 `api-int` 各一条 A 记录，指向 SLB IP |
| 1.11 | ECS 节点数量正确 | ECS 控制台 | 1 bootstrap（Assisted）+ 3 master + N worker |
| 1.12 | master-1 IP 符合预期（Agent-based） | ECS 实例详情 | 私有 IP = `RendezvousIp` 参数值 |
| 1.13 | 所有 ECS 节点已绑定 RAM Role | ECS 实例详情 → 实例 RAM 角色 | RAM Role 名称正确 |
| 1.14 | Outputs 包含所有预期输出 | ROS 栈 → 输出 | `InstallConfig` / `AgentConfig` / `DynamicCustomManifest` / `ApiSLBIp` / `VpcId` / `VSwitchId` |

---

## 阶段 2 — 节点引导（Discovery 阶段）

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 2.1 | ECS 节点已从镜像启动 | ECS 控制台 → 实例状态 | 所有实例状态"运行中" |
| 2.2 | 节点可以访问公网（出站） | SSH 到节点（如有跳板）→ `curl -I https://api.openshift.com` | HTTP 200，说明 NAT + SNAT 正常 |
| 2.3 | Discovery agent 已注册到 Red Hat 控制台 | cloud.redhat.com → 集群 → 主机列表 | 节点出现，状态 Ready |
| 2.4 | 节点 hostname 解析正常 | 节点上 `hostname -f` | 返回合理的主机名 |
| 2.5 | api-int DNS 可解析（Assisted） | 节点上 `dig api-int.<cluster>.<domain>` | 返回 SLB 内网 IP |

---

## 阶段 3 — 安装过程

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 3.1 | Custom Manifests 上传成功（Assisted） | Red Hat 控制台 → Custom manifests | 3 个文件已上传，无报错 |
| 3.2 | CCM ConfigMap 内容正确 | 查看 `alibaba-ccm-config.yaml` 内容 | `region`/`vpcid`/`vswitchid` 与 ROS Outputs 一致 |
| 3.3 | ProviderID MachineConfig 存在 | 查看 `03-machineconfig-providerid.yaml` | 格式 `alicloud://<region>.<instanceID>` |
| 3.4 | 安装进度正常推进 | Red Hat 控制台进度条 / `openshift-install agent wait-for` | 无长时间卡顿（>20 分钟同一步骤） |
| 3.5 | Bootstrap 完成 | 控制台或 `wait-for bootstrap-complete` | Bootstrap complete 提示 |

---

## 阶段 4 — 安装完成后基础验证

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 4.1 | kubeconfig 可用 | `oc cluster-info` | 返回 API server 地址 |
| 4.2 | 所有节点 Ready | `oc get nodes` | 所有节点 `Ready`，无 `NotReady` |
| 4.3 | 节点 ProviderID 格式正确 | `oc get node <name> -o jsonpath='{.spec.providerID}'` | 格式 `alicloud://<region>.<instanceID>` |
| 4.4 | CCM 正在运行 | `oc get pods -n alibaba-cloud-controller-manager -l k8s-app=alibaba-cloud-controller-manager` | 2 个 Pod，状态 Running |
| 4.5 | CCM 日志无错误 | `oc logs -n alibaba-cloud-controller-manager -l k8s-app=alibaba-cloud-controller-manager` | 无 `Error` 或 `Failed`；可见 node 初始化日志 |
| 4.6 | 节点 cloud-taint 已清除 | `oc describe node <name> \| grep Taints` | 无 `node.cloudprovider.kubernetes.io/uninitialized` taint |
| 4.7 | 所有 ClusterOperator Ready | `oc get clusteroperators` | 所有 CO `Available=True`，`Degraded=False` |

---

## 阶段 5 — CAPA 控制器验证

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 5.1 | CAPA CRD 已安装 | `oc get crd \| grep alibabacloud` | 4 个 CRD 存在 |
| 5.2 | CAPA controller Pod 运行正常 | `oc get pods -n openshift-cluster-api` | Pod 状态 Running |
| 5.3 | CAPA controller 日志无错误 | `oc logs -n openshift-cluster-api -l app=capa-controller-manager` | 无 panic 或 Fatal |
| 5.4 | 创建 AlibabaCloudCluster 对象 | `oc apply -f examples/capi-machinedeployment.yaml` | 资源创建成功 |
| 5.5 | AlibabaCloudCluster 状态 Ready | `oc get alibabacloudcluster -A` | `READY=true` |
| 5.6 | MachineDeployment 创建新节点 | `oc get machines -A` | Machine 出现，最终状态 `Running` |
| 5.7 | 新节点加入集群 | `oc get nodes` | 新节点出现，状态 `Ready` |
| 5.8 | 缩容删除节点 | `oc scale machinedeployment ... --replicas=0` | Machine 删除，ECS 实例在阿里云控制台消失 |

---

## 阶段 6 — Ingress 验证

| # | 检查项 | 验证方法 | 预期结果 |
|---|--------|---------|---------|
| 6.1 | CCM 已为 Ingress Service 创建 SLB | `oc get svc -n openshift-ingress router-default` | `EXTERNAL-IP` 有值（SLB IP） |
| 6.2 | 添加 *.apps DNS 记录 | PrivateZone / 公共 DNS 控制台 | `*.apps.<cluster>.<domain>` → Ingress SLB IP |
| 6.3 | OpenShift Console 可访问 | 浏览器访问 `https://console-openshift-console.apps.<cluster>.<domain>` | 登录页面正常显示 |
| 6.4 | 部署测试应用并访问 | `oc new-app httpd; oc expose svc/httpd` | `curl http://httpd-default.apps.<cluster>.<domain>` 返回 200 |

---

## 常见问题速查

| 症状 | 可能原因 | 排查命令 |
|------|---------|---------|
| 节点不出现在 Red Hat 控制台 | NAT/SNAT 未生效，节点无法访问公网 | 节点上 `curl https://api.openshift.com`；查看 SNAT 规则 |
| 安装卡在 bootstrap 阶段 | SLB 无后端 / api-int DNS 解析失败 | `dig api-int.<cluster>.<domain>` from node；查看 SLB 后端健康状态 |
| 节点一直 NotReady（云污点未清除） | CCM 未启动 / ProviderID 格式错误 | `oc logs -n alibaba-cloud-controller-manager <ccm-pod>`；检查 ProviderID 格式 |
| CCM 启动报认证错误 | RAM Role 未绑定到 ECS / Policy 权限不足 | ECS 控制台确认 RAM Role；RAM 控制台确认 Policy Action |
| CAPA 创建 ECS 失败 | RAM Policy 缺少 `ecs:RunInstances` 或子网/安全组 ID 填写有误 | `oc logs -n openshift-cluster-api <capa-pod>`；检查 AlibabaCloudMachine spec |
