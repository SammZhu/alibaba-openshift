# 99-teardown.yml 参考手册

split 架构下 `ansible/playbooks/99-teardown.yml` 的所有模式、行为、组合规则、典型场景。

> 适用：split 架构（`mirror-stack` 持久 + `cluster-stack` 短命）。
> legacy 单 stack 架构请用 `ansible/playbooks/99-teardown-LEGACY.yml`，规则不同。

---

## 1. 三个核心变量

| 变量 | 默认 | 取值 | 含义 |
|---|---|---|---|
| `teardown_target` | `cluster` | `cluster` / `mirror` / `both` | 拆哪一层 |
| `teardown_preserves_ai` | `false` | bool | mirror 拆除时是否保留 AI cluster + infra-env + ECS image |
| `delete_mirror_snapshots` | `false` | bool | 是否连同 mirror 双盘 snapshot 一起删 |
| `cluster_includes_ai` | `false` | bool | cluster 拆除时是否也拆 AI cluster + infra-env + ECS image（用于改 `openshift_version`） |
| `teardown_confirmed` | `false` | bool | `true` 跳过交互确认；脚本/CI 场景必填 |

---

## 2. 各 target 在云上删什么

### `teardown_target=cluster`（默认）

| 资源 | 默认 | `-e cluster_includes_ai=true` |
|---|---|---|
| `aliocp1-cluster` ROS stack（含 3 master + NLB + SG + PrivateZone）| ✅ | ✅ |
| state.yml 里 `cluster_*` 字段 | ✅ | ✅ |
| mirror-stack | ❌ | ❌ |
| AI cluster + infra-env | ❌ | **✅** |
| ECS image（Phase 02 build 的） | ❌ | **✅** |
| state.yml 里 `cluster_id` / `infra_env_id` / `ecs_image_id` / `iso_path` | ❌ | **✅ (清空)** |
| mirror snapshot（vda + vdb）| ❌ | ❌ |

**默认用途**：迭代 cluster install。常见场景。重建只需 06 → 07（约 1 小时）。

**`cluster_includes_ai=true` 用途**：换 OCP `openshift_version`。AI v2 API 不允许 PATCH 已创建 cluster 的 openshift_version（属于不可变字段），所以必须重建 AI cluster + infra-env + 配套 ECS image。mirror 内容不动（不需要重 build tarball、不需要重导入 image），只需要：`01 → 02 → 04 → 06 → 07`（mirror 用 already_ready 跳过 import，整体 ~50 min）。

### `teardown_target=mirror`

行为受 `teardown_preserves_ai` + `delete_mirror_snapshots` 联合决定。见第 3 节。

### `teardown_target=both`

先按 `cluster` 模式拆 cluster-stack，再按 `mirror` 模式拆 mirror。`teardown_preserves_ai` 在这里没意义（cluster 也死了，AI 留着也没用）—— 不要组合用。

---

## 3. `teardown_target=mirror` 的三种模式

mirror 拆除时的资源处置依赖两个开关。下表是**完整组合矩阵**：

| 命令 | mirror-stack | mirror snapshot | AI cluster + infra-env | ECS image | 下次重建路径 | 总耗时 |
|---|---|---|---|---|---|---|
| `99 -t mirror`（默认）| 删 | **留** | 删 | 删 | 01 → 02 → 03 (fast-path) → 04 (skip import) → 06 → 07 | ~15 min |
| `99 -t mirror -e teardown_preserves_ai=true` | 删 | **留** | **留** | **留** | 03 (fast-path) → 04 (skip) → 06 → 07 | **~10 min** ✨ |
| `99 -t mirror -e delete_mirror_snapshots=true` | 删 | 删 | 删 | 删 | 01 → 02 → 03 (fresh) → 04 (full import ~30 min) → 06 → 07 | ~70 min |

### 互斥规则

`teardown_preserves_ai=true` 跟 `delete_mirror_snapshots=true` 互斥，playbook 开头 assert 拦截。原因：preserves_ai 假设你下次走 snapshot 恢复（AI install-config 引用的 CA/IP 在快照里仍有效），snapshot 都删了 AI 留着没用——AI 的 ignition / IDMS 跟新 mirror 对不上。

### 为什么 mirror 拆除会牵连 AI cluster + ECS image（默认模式）

它们三个是一组耦合资源：
- AI cluster 的 install-config 被 Phase 04 PATCH 过，里面写死了 **mirror 的 CA cert + IP**（trustBundle + IDMS）
- ECS image (Phase 02) 烤进去了 discovery ignition，里面写死了 **infra-env-id + 同一组 CA/IP**

mirror 重建 = 通常新 CA + 新 IP → AI install-config 失效 → AI cluster 重建 → infra-env 重建 → ECS image 重建。

**snapshot 恢复**绕开这条链：vda snapshot 里保留了 `/etc/quay-config/ssl.cert`（CA），mirror IP 在 ROS 模板里写死（`MirrorPrivateIp` 参数 = 10.0.16.4），所以 CA 和 IP 都不变 → AI 和 ECS image 都能继续用。`teardown_preserves_ai=true` 就是利用这一点。

### 为什么 snapshot 跟 ECS image 是一组（`delete_mirror_snapshots=true` 必然连删 image）

`aliocp1-mirror-system-from-snap` 这个 custom image 是 03 fast-path 通过 `CreateImage --SnapshotId <vda-snap>` 派生出来的，Aliyun 把它跟原 snapshot 视为强关联。控制台手工删 image 时勾"同时删除快照"会把原 snapshot 也带走（CLI 通过 `DeleteImage --Force true` **不**级联，但 image 是临时产物，留着也没用）。代码里 `delete_mirror_snapshots=true` 触发的删除顺序是：先 DeleteImage（image-only），再 DeleteSnapshot（system+data 两个）。

### 03 fast-path 的防御性降级

state.yml 里 `mirror_snapshot_*_id` 可能跟云上不一致（控制台手工删过、其他渠道清理过、`delete_mirror_snapshots=true` 删了但 state 没同步）。03 fast-path 在 `CreateImage --SnapshotId` 之前先 `DescribeSnapshots --SnapshotIds` 验证两个 snapshot 都活着，缺一个就自动**降级到 fresh-path**，不会硬挂在 InvalidSnapshotId.NotFound。

---

## 4. state.yml 字段处置

每种模式拆除后 `ansible/state.yml` 的字段变化：

| 字段 | `-t cluster` | `-t mirror` 默认 | `-t mirror -e preserves_ai=true` | `-t mirror -e delete_snapshots=true` | `-t both` |
|---|---|---|---|---|---|
| `cluster_stack_id` | 清空 | — | — | — | 清空 |
| `api_lb_endpoint`, `worker_sg`, `master_ip*`, `*_zone_id*` | 清空 | — | — | — | 清空 |
| `mirror_stack_id` 系列 (VPC/VSwitch/SG/RamRole) | — | 清空 | 清空 | 清空 | 清空 |
| `mirror_init_password` | — | 清空 | 清空 | 清空 | 清空 |
| `mirror_snapshot_system_id`, `mirror_snapshot_data_id` | — | **保留** | **保留** | 清空 | 保留 / 清空（同 mirror 规则）|
| `cluster_id`, `infra_env_id` | — | 清空 | **保留** | 清空 | 清空 |
| `ecs_image_id`, `iso_path` | — | 清空 | **保留** | 清空 | 清空 |

清空 = 写入空字符串而非 unset，保证 `load_state.yml` 后所有 fact 都已定义（避免 `is defined` 检查歧义）。

---

## 5. 典型使用场景

### 场景 A：调 cluster install 的某个 manifest，反复试

```bash
# 拆 cluster（mirror 不动）
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=cluster -e teardown_confirmed=true

# 修代码 / manifest / ROS template ...

# 重建 cluster
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

### 场景 B：mirror ECS 网络坏了/想换 InstanceType，但 mirror 数据要保留

```bash
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=mirror \
  -e teardown_preserves_ai=true \
  -e teardown_confirmed=true

# 调 mirror-stack.yaml ROS 模板 / group_vars 之类
# 重建 mirror（fast-path 用 snapshot 恢复 + AI 沿用）
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml
ansible-playbook ansible/playbooks/04-prepare-mirror.yml       # 走 already_ready 直接跳过
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

### 场景 C：换 OCP 版本（patch 内：4.20.22 → 4.20.23）

只换 patch，mirror channel 不变。AI cluster.openshift_version 不可 PATCH，必须重建 AI；但 mirror 可以复用（如果新 patch 的 image 已在 mirror 里，否则需要重 build mirror tarball）。

```bash
# 1) 改 all.yml 把 openshift_version 改成新 patch（例如 "4.20.23"）

# 2) 如果新 patch 的 image 不在当前 mirror 里 → 重 build tarball + 重做完整 mirror（场景 D）。
#    如果新 patch 已在 mirror（罕见，但可能 build 时多 mirror 了几个 patch）→ 直接走 cluster_includes_ai 路径：
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=cluster \
  -e cluster_includes_ai=true \
  -e teardown_confirmed=true

# 3) mirror 不动；只重做 AI/image/cluster 链
ansible-playbook ansible/playbooks/01-prepare-iso.yml         # 新 AI cluster + infra-env
ansible-playbook ansible/playbooks/02-import-image.yml        # 新 ECS image
ansible-playbook ansible/playbooks/04-prepare-mirror.yml      # already_ready 跳 import；PATCH 新 install-config + verify version
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

### 场景 C2：换 OCP 大版本（4.20 → 4.21）

新大版本 = 新 RHCOS discovery agent 版本 = mirror 里没相应 image = 必须重 build mirror。

```bash
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=mirror \
  -e delete_mirror_snapshots=true \
  -e teardown_confirmed=true

# 改 group_vars/all.yml 里 openshift_version "4.21.x"
# 重 build mirror tarball
./scripts/build-mirror-tarball.sh   # 用对应 OPENSHIFT_VERSION 环境变量

# 全套重跑
ansible-playbook ansible/playbooks/01-prepare-iso.yml
ansible-playbook ansible/playbooks/02-import-image.yml
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml  # fresh path (no snapshot)
ansible-playbook ansible/playbooks/04-prepare-mirror.yml       # full import ~30 min
ansible-playbook ansible/playbooks/05-verify-mirror.yml        # 末尾会重新打 snapshot
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
```

### 场景 D：彻底清理云账户（demo 完结/给别人留干净环境）

```bash
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=both \
  -e delete_mirror_snapshots=true \
  -e teardown_confirmed=true

# 还要手工清理的（playbook 不管）：
#   - OSS bucket 里的 mirror tarball / installer / version markers
#     （不删的话每月几块钱存储费）
#   - state.yml 本地文件
#   - 本地 discovery ISO (/home/.../discovery-*.iso)
```

---

## 6. 安全 / 互锁

- `teardown_confirmed=false` (默认) 时会交互式 `Type 'yes' to proceed`，规模信息在提示里
- `teardown_confirmed=true` 跳过提示（CI / 脚本用）
- 互斥 assert 在 playbook 第一组 task 里跑，配置错了立刻挂掉不会动云
- 删除 stack 用 `--RetainAllResources false`，stack 里所有资源真删（不保留 disk/EIP 等长尾费用）
- AI cluster 删除走 `DELETE /clusters/{id}`，status_code 接受 `[202, 204, 404]`（404 = 已经被别人删了，也算成功）
- 所有 Delete 类操作 `failed_when: false`，部分资源已不存在不会中断 playbook

---

## 7. 故障排查

| 现象 | 可能原因 | 应对 |
|---|---|---|
| `teardown_preserves_ai=true 跟 delete_mirror_snapshots=true 互斥` assert 失败 | 同时设了两个 true | 选一个，看场景 B vs C |
| 03 fast-path 报 `InvalidSnapshotId.NotFound` | state 里 snapshot ID 在云上没了 | 升级到带防御性检查的版本（commit `22ef0e5` 之后）；旧版手工 `sed -i '/^mirror_snapshot_.*_id:/d' ansible/state.yml` 后重跑 03 |
| 拆 mirror 后想用 snapshot 但发现 image 也被删了 | 控制台手工删 image 时勾了"同时删除快照" | 不可逆，只能 fresh bring-up（场景 C） |
| `DeleteStack` 卡 `DELETE_IN_PROGRESS` 超过 30 min | NLB / ROS 后端慢，或某个资源依赖循环 | 看 Aliyun ROS 控制台 stack 事件日志；通常 EIP/NLB 卸载需要时间 |
| `DescribeImages --ImageName ...` 返回空但本地 state 仍有 `ecs_image_id` | 别的渠道删了 image 但没更新 state | Phase 02 现在带 invalidation task（commit `19fe4e0`），下次 02 会自动重 build；手动恢复：`sed -i '/^ecs_image_id:/d' ansible/state.yml` |
| 拆 cluster 后跑 06 报 stale `cluster_stack_id` | 99 拆 cluster 时 state 没清干净（很罕见） | 手工 sed 删 cluster_* 字段后再 06 |

---

## 8. 跟 build / OSS 的关系

99-teardown **不**碰：
- OSS bucket 里的对象（mirror tarball、isc sibling、mirror-registry installer、version markers）—— 这些是 build 产物，跨 cluster 复用
- 本地 ISO 文件（`/home/.../openshift-install/<cluster>/discovery-*.iso`）—— Phase 01 的产物
- 本地 mirror-build 目录（`mirror-build/imageset-config.yaml` 等）—— `scripts/build-mirror-tarball.sh` 的产物

要清这些**手工**操作：

```bash
# OSS：清整个 bucket
aliyun oss rm oss://openshift-iso-samzhu-test/ --recursive --force \
  --endpoint=oss-cn-wulanchabu.aliyuncs.com \
  --access-key-id=... --access-key-secret=...

# 本地
rm -rf /home/sam/openshift-install/aliocp1/
rm -rf mirror-build/openshift-mirror/  # 24 GB chunk + working-dir
```
