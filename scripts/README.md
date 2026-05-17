# 自动化脚本套件（已弃用，推荐用 ansible/）

> **⚠️ 已被 [`ansible/`](../ansible/) 取代**——后者更稳健（原生 retry/idempotent、
> 结构化 HTTP/JSON、跨 playbook state 自动传递）。本 shell 版本保留作快速参考，
> 不再维护新特性。



把整个测试流程拆成 6 个脚本，**端到端 ~90 分钟无人值守**（不算等待时间）。

## 文件结构

```
scripts/
├── lib/common.sh           # 共用函数库（不直接执行）
├── config.sh.example       # 配置模板（复制为 config.sh 后编辑）
├── config.sh               # ← 你的配置（gitignored）
├── .state                  # ← 跨脚本传递 ID/IP 的状态文件（gitignored）
│
├── 01-prepare-iso.sh       # Phase A.1: Assisted API → Discovery ISO
├── 02-import-image.sh      # Phase A.2-4: OSS 上传 + ECS 镜像导入
├── 03-create-stack.sh      # Phase B: ROS 栈 + 等就绪 + 拿 Outputs
├── 04-install-cluster.sh   # Phase C: 等节点 + 分配角色 + 上传 manifest + 装集群 + 拉 kubeconfig
├── 05-deploy-post-install.sh  # Phase D: 部署 CSI/CAPI（在跳板上跑）
├── 99-teardown.sh          # Phase G: 销毁集群 + 检测孤儿资源
│
├── all.sh                  # 跑 01-04 一条龙
└── README.md               # 本文件
```

## 一次性准备

```sh
# 1. 复制配置模板并编辑
cp scripts/config.sh.example scripts/config.sh
vi scripts/config.sh

# 2. 从 Red Hat Console 拿 offline token（一次性）
#    打开 https://console.redhat.com/openshift/token → Load token → 复制
mkdir -p ~/.openshift
read -s OFFLINE_TOKEN && echo "$OFFLINE_TOKEN" > ~/.openshift/offline-token && chmod 600 ~/.openshift/offline-token

# 3. 确保 aliyun CLI、pull-secret、SSH key 都准备好（参看 config.sh 注释）
```

## 跑

```sh
# 端到端跑（01-04，约 90 分钟，期间可以喝咖啡）
./scripts/all.sh

# 或者一步一步跑，方便调试
./scripts/01-prepare-iso.sh
./scripts/02-import-image.sh
./scripts/03-create-stack.sh
./scripts/04-install-cluster.sh

# 04 跑完会自动 scp kubeconfig 到跳板，最后输出 ssh 命令。
# Phase 05 必须在跳板上跑：
ssh root@$(awk -F= '/^JUMP_HOST_IP=/{print $2}' scripts/.state)
cd /root/openshift-alibaba/alibaba-openshift
./scripts/05-deploy-post-install.sh
```

## 失败重跑

每个脚本都做了 idempotency：
- 01: 如果 .state 里已有 CLUSTER_ID 且 Assisted 上还在，跳过创建
- 02: 如果镜像已 Available，跳过；如果在导入中，继续等
- 03: 如果栈已 CREATE_COMPLETE，刷新 Outputs；如果在 IN_PROGRESS，继续等
- 04: 如果集群已 installed，重拉 kubeconfig

直接重跑 `./scripts/0X-...sh` 或 `./scripts/all.sh --from 03` 即可。

## 销毁

```sh
# 1. 先在跳板上清理应用层（避免孤儿 SLB/磁盘）
ssh root@$(awk -F= '/^JUMP_HOST_IP=/{print $2}' scripts/.state)
export KUBECONFIG=/root/kubeconfig
oc delete svc -A --field-selector spec.type=LoadBalancer
oc delete pvc -A --all
sleep 180   # 等 CCM/CSI 异步释放阿里云资源

# 2. 回到本地，销毁 Assisted 集群 + ROS 栈
exit
./scripts/99-teardown.sh
```

## 状态文件 .state

各脚本读写的 KEY=VALUE 文件。手动查：
```sh
cat scripts/.state
```

清空重来：
```sh
rm scripts/.state
```

## 故障排查

| 现象 | 检查 |
|---|---|
| `aliyun ... permission denied` | RAM 用户少了某个 FullAccess 策略 |
| `Failed to get access token` | offline-token 错或被 revoke |
| 01 卡在 "Polling for ISO ready" | Assisted 服务暂时挂了，等 1 分钟重跑 |
| 02 报 `Forbidden.RAM` | 首次 ImportImage 需要授权 ECS→OSS，去 web 控制台点一次 Import Image 触发授权 |
| 03 报 CREATE_FAILED | ROS Console → Events 标签页看具体哪个资源失败 |
| 04 卡在等待 hosts | ECS 节点是否启动？VNC 看节点是否进了 RHCOS Discovery 界面 |
| 04 ClusterStatus=insufficient | Assisted UI 看 cluster validations 哪一条没过 |

## 成本估算

- 完整跑 4 小时（含等待）：约 ¥15-25
- 加 OPCT 12 小时：约 ¥70-100
- 不要忘记跑 `99-teardown.sh` —— 不然 SLB / 跳板按小时计费持续累计
