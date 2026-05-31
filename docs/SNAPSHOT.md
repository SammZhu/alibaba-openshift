# Mirror snapshot lifecycle

阿里云 ECS snapshot 是这套自动化里**省时间最多**的一环：保留得当，
mirror 重建从 ~70 min 压到 ~10 min。但快照逻辑分散在 03/04/05/99 四个
playbook 里，又跟自定义 image、AI cluster、ECS image 互相耦合 ——
本文集中讲清楚生命周期、互斥规则、典型恢复路径。

> **配套阅读**：
> - [`docs/TEARDOWN.md`](TEARDOWN.md) — 销毁矩阵 + 各 flag 组合的下次重建路径
> - [`docs/MIRROR.md`](MIRROR.md) — mirror registry 整体架构、成本、oc-mirror 故障排查
> - `ansible/playbooks/05-verify-mirror.yml` — snapshot 创建实现（含 sanity check）
> - `ansible/playbooks/03-create-mirror-stack.yml` — fast-path 恢复实现
> - `ansible/playbooks/99-teardown.yml` — snapshot / image / state 联动删除

## TL;DR

- **两份 snapshot**：vda（system）+ vdb（data），都在 Phase 05 末尾创建。
  名字固定为 `<ClusterName>-mirror-system` / `-mirror-data`。
- **生命周期**：05 创建 → 03 fast-path 用 → 99 按 flag 选择性删。**幂等**：
  05 重跑会先删同名旧 snapshot 再建新的；03 fast-path 缺失会自动降级。
- **fast-path 起 ECS**：03 把 vda snapshot 通过 `CreateImage` 转成自定义
  image，传给 ROS 当 `MirrorOverrideImageId`；vdb snapshot 直接通过 ROS 的
  `MirrorDataDiskSnapshotId` 参数附到 DiskMappings。
- **`mirror_snapshot_enabled=false`** 跳过 05 的创建步骤（不推荐，除非
  你确定下次会从 tarball 全量重建）。
- **`delete_mirror_snapshots=true`**（99 teardown）连同 vda+vdb snapshot
  和派生 image 一起删。**跟 `teardown_preserves_ai=true` 互斥**。

## 两份 snapshot 各装什么

| 名字 | 来源盘 | 内容 | 大小 |
|---|---|---|---|
| `<cluster>-mirror-system` | `/dev/vda` (40 GB cloud_essd) | RHEL OS + mirror-registry installer 树 (`/opt/mirror-registry`) + Postgres 数据卷 + `/etc/quay-config/ssl.cert` (Quay CA) + Quay 容器 layer 缓存 | ~10–15 GB used |
| `<cluster>-mirror-data` | `/dev/vdb` (200 GB cloud_essd) | Quay 的 blob 存储（镜像 layer） + oc-mirror v2 d2m 暂存 | ~80–100 GB used after import |

关键点：**CA 在 vda 里**。这是为什么 `teardown_preserves_ai=true` 能成立 ——
snapshot 恢复后 mirror IP 不变（`MirrorPrivateIp` 在 ROS 模板里写死
`10.0.16.4`），CA 也不变 → AI cluster 引用的 `additionalTrustBundle` /
`ImageDigestMirrorSet` 仍然有效。

## 生命周期时序

```
Phase 05 verify-mirror
  ├── 严格 sanity check（见下节）—— 任一项缺失就拒绝拍快照
  ├── 找 mirror ECS 的 vda + vdb disk-id
  ├── DescribeSnapshots --SnapshotName <new-name>   # 找同名旧 snap
  ├── CreateSnapshot vda   → state.mirror_snapshot_system_id
  ├── CreateSnapshot vdb   → state.mirror_snapshot_data_id
  ├── 轮询 Progress=100% (最长 30 min)
  └── 删旧同名 snapshot（仅在新的 100% 之后；失败死在前面 → 旧的保留）

Phase 03 create-mirror-stack（下次重建时）
  ├── if state.mirror_snapshot_*_id 都存在 且 -e mirror_restore_from_snapshot != false:
  │     ├── DescribeSnapshots 验证云上 snapshot 仍存在
  │     │     └── 缺一个 → 降级到 fresh-path（自动），打印降级原因
  │     ├── DescribeImages by-name <cluster>-mirror-system-from-snap
  │     │     └── 有就复用，没有则 CreateImage --SnapshotId <vda-snap>
  │     ├── 等 image Available（最长 15 min）
  │     └── ROS Parameters：
  │           MirrorOverrideImageId      = <派生 image id>
  │           MirrorDataDiskSnapshotId   = <vdb snapshot id>
  └── else: 走 fresh bring-up，ROS 用 mirror_base_image_id + 空 vdb

Phase 04 prepare-mirror（fast-path 后）
  └── 检测到 mirror-registry 已就绪（systemd unit active + Quay
      /health/instance 200）→ 整段 download/install/import skip。

Phase 99 teardown（mirror 或 both）
  ├── 总是删派生的 mirror-system-from-snap image
  │     （ROS 用完一次就没人引用了；下次 fast-path 会重新 CreateImage）
  ├── if delete_mirror_snapshots=true:
  │     ├── DeleteSnapshot <vda>
  │     ├── DeleteSnapshot <vdb>
  │     └── state.mirror_snapshot_*_id 清空
  └── else (默认):
        snapshot 保留 + state 保留 → 下次 03 自动走 fast-path
```

## Phase 05 的 sanity check（防止把坏状态固化）

历史教训（2026-05）：曾出现 snapshot 名字标的是 "system (Postgres + cert
+ Quay container layers)"，但实际 vda 上 `/opt/mirror-registry` 不存在、
Postgres 数据卷不存在、systemd unit 不存在 —— 起源是 `/health/instance`
+ 仓库列表 HTTP 200 检查只能证明 Quay 容器活着，**不能证明 mirror-registry
installer 还在**。这种残缺 snapshot 覆盖了之前的好 snapshot，下次 fast-path
起 ECS、cloud-init 完成后系统盘是空壳，必须全量重做 04 (~70 min)。

修复后的检查项（任一缺失 → 拒绝拍快照，原 snapshot 完好保留）：

1. `/opt/mirror-registry/` 存在（mirror-registry installer 树，升级/重装要用）
2. `/var/lib/quay-postgres-storage/` 存在（Postgres 数据卷）
3. `quay-app.service` 存在且 systemd 知道（管理层）
4. Quay `/health/instance` 200（容器层活着）
5. mirror-registry 仓库列表 API 200（应用层活着）

实现在 `05-verify-mirror.yml` 的 "严格文件树 sanity" block。

## Snapshot ↔ Image 关系

派生 image `<cluster>-mirror-system-from-snap` 是 03 fast-path 通过
`CreateImage --SnapshotId <vda-snap>` 临时产物，被 ROS 当
`ALIYUN::ECS::Instance` 的 `ImageId` 使用一次后就没人引用了。

**Aliyun 把这个 image 跟原 vda snapshot 视为强关联**：

- 控制台手工删 image 时勾"同时删除快照" → 会把原 vda snapshot 也带走。
- CLI `DeleteImage --Force true` 默认**不**级联删 snapshot（这是我们的
  代码依赖的行为）。
- `DeleteImage` 必须用 `--ImageId`，不接受 `--ImageName` → 代码先
  `DescribeImages --ImageName` 查 ID，再删。

teardown 删除顺序总是：先 `DeleteImage`（image-only），后
`DeleteSnapshot`（如果 `delete_mirror_snapshots=true`）。

## 互斥规则：`teardown_preserves_ai` × `delete_mirror_snapshots`

```
                       │ delete_mirror_snapshots=false │ delete_mirror_snapshots=true
───────────────────────┼───────────────────────────────┼──────────────────────────────
preserves_ai=false     │ ✅ 默认                       │ ✅ 全清 + fresh 重建 (~70 min)
preserves_ai=true      │ ✅ 最快 fast-path (~10 min)  │ ❌ assert 拒绝
```

为什么右下格被拒绝：`preserves_ai=true` 假设你下次走 snapshot 恢复
（AI 的 install-config / `additionalTrustBundle` / `ImageDigestMirrorSet`
都引用旧 mirror 的 CA + IP，snapshot 恢复后这些值不变 → AI 可以继续
用）。但 `delete_snapshots=true` 把恢复源也删了 ——
AI 留着也对不上新 mirror 的（重新生成的）CA。

## 典型场景

### 场景 1 — 频繁重装 cluster，mirror 不动（最常见）

```sh
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=cluster -e teardown_confirmed=true
# mirror-stack / snapshot 都没动
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
# ~30 min 重装完成
```

### 场景 2 — 拆整套但下次想快速重建（snapshot 留着）

```sh
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=both -e teardown_confirmed=true
# snapshot 默认保留；ECS / VPC / RAM 都删

# 下次：
ansible-playbook ansible/playbooks/00-preflight.yml      # ~1 min
ansible-playbook ansible/playbooks/01-prepare-iso.yml    # ~5 min
ansible-playbook ansible/playbooks/02-import-image.yml   # ~5 min
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml  # fast-path ~12 min
ansible-playbook ansible/playbooks/04-prepare-mirror.yml       # skip 整段 ~1 min
ansible-playbook ansible/playbooks/05-verify-mirror.yml        # 重打 snapshot ~10 min
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
# 总 ~70 min（vs fresh 全做 ~3 h）
```

### 场景 3 — 拆 mirror，下次还想用同一个 AI cluster（最快）

```sh
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=mirror -e teardown_preserves_ai=true -e teardown_confirmed=true
# mirror-stack ECS 删，但 snapshot + AI cluster + infra-env + ECS image 全留

# 下次（cluster 也是新建）：
ansible-playbook ansible/playbooks/03-create-mirror-stack.yml  # fast-path ~12 min
# 跳过 01/02/04 全部
ansible-playbook ansible/playbooks/06-create-cluster-stack.yml
ansible-playbook ansible/playbooks/07-install-cluster.yml
# 总 ~30 min
```

### 场景 4 — 彻底清理（迁 region / 换 OCP 版本 / 不再用）

```sh
ansible-playbook ansible/playbooks/99-teardown.yml \
  -e teardown_target=both -e delete_mirror_snapshots=true -e teardown_confirmed=true
# 所有云资源都删，snapshot + image 全清
```

## 故障排查

| 现象 | 原因 / 解法 |
|---|---|
| Phase 05 跑到 "严格文件树 sanity" 失败，旧 snapshot 没动 | 设计如此 —— mirror ECS 状态不健康，拒绝把坏状态固化。修好 `/opt/mirror-registry` 等再重跑 05 |
| Phase 03 报 `InvalidSnapshotId.NotFound` | state.yml 里 snapshot ID 在云上没了。当前版本会自动降级到 fresh-path；老版本需手工 `sed -i '/^mirror_snapshot_.*_id:/d' ansible/state.yml` 后重跑 03 |
| 想用 snapshot 但 image 也被一起删了 | 控制台手工删 image 时勾了"同时删除快照"。不可逆 —— snapshot 没了只能 fresh bring-up（场景 4） |
| `teardown_preserves_ai=true 跟 delete_mirror_snapshots=true 互斥` assert | 见上文互斥矩阵右下格，二选一 |
| 同名 snapshot 多份共存 | 05 在轮询 100% 期间被打断，下次跑会先扫名字一并清理。手工清：`aliyun ecs DescribeSnapshots --SnapshotName <name>` 后 `DeleteSnapshot --SnapshotId <id>` |
| `mirror_snapshot_enabled=false` 跑过 05 后想补 snapshot | 直接重跑 `05-verify-mirror.yml`（不带 `-e`），sanity check 过了就建 |

## 成本

阿里云 ESSD snapshot 按实际占用空间计费（typical ~¥0.16/GB·月）：

- vda snapshot：~10–15 GB → ~¥2/月
- vdb snapshot：~80–100 GB（取决于 ImageSetConfig 体积）→ ~¥15/月

每个 cluster 一对 snapshot ≈ ¥17/月，换 ~60 min 重建时间。绝大多数场景
值得保留；只在彻底放弃这个 cluster 时才用 `delete_mirror_snapshots=true`。
