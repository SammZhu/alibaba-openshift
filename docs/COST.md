# 成本规则与降费指南 (Cost Optimization Reference)

针对该项目在阿里云的实际消费模式，沉淀一份"哪些做法贵 / 哪些不贵 /
为什么"的速查表，避免后续重复踩费用坑。

最后更新：2026-06-05
当前月度账单基线（2026-05 实测）：~¥800/月 → P3 优化后目标 ~¥250-350/月

---

## 0. 2026-06-05 全量成本审计结论

完整扫了一遍云端资源（ECS / image / snapshot / disk / EIP / NAT / SLB / OSS），
结论：**P3 优化后浪费点基本归零**。对照月初 ¥848 画像：

| 月初浪费点 | 现状 | 修复 |
|:-|:-|:-|
| OSS 公网下行 ¥150 | 走 internal endpoint；fast-path 连 download 都 skip | #35 + #45 |
| ECS 超配（3×g7.xlarge master）| SNO 单节点 + mirror 用 g7.large | #36 + #39 |
| snapshot 累积（曾 17 个孤儿）| 清零 + teardown 自动 dedup/cascade/sweep | #38 #44 #47 |
| NAT base 费 | PayByLcu，闲时几乎免费 | 既有设计 |

审计当时唯一的"非零"项：mirror 快照 ×2 对（旧对被 live ECS 磁盘锁住，
~¥1.3/天临时），属 #47 已知的"锁定中"状态，下次 mirror teardown 自动清。
**不是漏洞。**

### ⚠️ 关键运维习惯：teardown 用 `teardown_target=both`

mirror 快照的"旧对"只有在 **mirror ECS 被删之后**才能删（aliyun
`SnapshotCreatedDisk` 锁：被磁盘引用的源快照删不掉）。所以：

- `teardown_target=cluster`（只删集群，留 mirror）→ 旧快照对一直锁着，
  每次 restore-rebuild 多攒一对（~¥1.3/天/对累积）
- `teardown_target=both`（删集群 + mirror）→ #47 stale-sweep 在删 ECS
  后触发 → 旧快照清掉 → 名下只剩 active 对（或 0）

**结论：日常用 `teardown_target=both`**，除非你明确要保 mirror ECS 在线
（如连续多次只重建集群）。保 mirror 省 ~10 min 重建时间，但代价是
快照累积 + mirror ECS 全程计费（g7.large ¥0.54/h）。权衡：超过 1 天不
重建就该 both。

---

## 1. 账单结构（2026-05 实测）

| 产品 | 金额 | 占比 | 主要消费场景 |
|:-|:-:|:-:|:-|
| ECS | ¥380 | 48% | 3 master + jumphost + mirror，dev session 时跑 |
| OSS | ¥220 | 28% | 镜像 tarball 上下传 + ECS image import |
| NAT 网关 | ¥150 | 19% | Enhanced NAT，CU Fee ¥117 + Instance Fee ¥33 |
| 其他（PrivateZone / 快照 / EIP / NLB） | ~¥50 | 5% | 杂项 |

NAT 细分（关键）：
- **CU Fee ¥117**：按 LCU 处理流量/连接，每 LCU·h ¥0.18
- **Instance Fee ¥33**：Enhanced NAT 实例 base，¥0.32/h × ~100 h

---

## 2. 致命陷阱：OSS public endpoint 双重收费

**最容易被忽略的成本黑洞。**

aliyun OSS 提供两个 endpoint：

| Endpoint | 适用场景 | 流量费 | NAT 影响 |
|:-|:-|:-:|:-:|
| `oss-${region}.aliyuncs.com` | 跨 region / 公网访问 | ¥0.50/GB 出网 | **经 NAT，吃 CU Fee** |
| `oss-${region}-internal.aliyuncs.com` | 同 region aliyun 内网 | **0** | **不经 NAT** |

→ 同 region 的 ECS 走 public endpoint = **同时被收 OSS 公网费 + NAT CU Fee**。

实测：一次 mirror 30 GB tarball download
- public endpoint: ¥15 OSS + ~¥3-5 NAT = **~¥18-20 浪费**
- internal endpoint: ¥0

5 月-至今 ~10 次 mirror import 操作 = **~¥200 白付**。

### 解决：endpoint 自动检测

`ansible/tasks/oss_endpoint.yml` 通过 aliyun metadata service
(http://100.100.100.200/) 检测当前运行环境：
- 在 aliyun ECS 内 → `_oss_endpoint = oss-${region}-internal.aliyuncs.com`
- 在 on-prem / 外部 → `_oss_endpoint = oss-${region}.aliyuncs.com`

`scripts/build-mirror-tarball.sh` 的 `_detect_oss_endpoint()` 函数实现
同样逻辑。

调用方一律用 `{{ _oss_endpoint }}` 或 `$OSS_ENDPOINT`，不要 hardcode。

---

## 3. 其他持续注意事项

### 3.1 mirror snapshot 不要轻易删
快照存储 ¥0.12/GB/月（cn-wulanchabu），240 GB 的两个 mirror 快照
≈ ¥29/月，几乎可忽略。但**一旦删了**，下次 mirror import 要重新跑
oc-mirror，~25 GB 跨境 quay 下载 = ¥20-30 一次性 NAT 流量费。

99-teardown.yml 默认 `delete_mirror_snapshots: false` — 保持这个默认。

### 3.2 NAT 是 Enhanced 型 (pay-by-LCU) 不要改 pay-by-spec
mirror-stack ROS 用的是 Enhanced NAT — 按 LCU 用量算钱，闲置近乎免费。
如果改成 pay-by-spec 固定带宽，always-on 一个月 ¥230+，比当前贵很多。

### 3.3 dev 集群优先 SNO
3-master 一天大约比 SNO 多花 ~¥30 ECS。除非真在测 HA / 多节点专属
功能（如 NAS RWX 热迁移、MachineSet 横扩），dev/test 都该用 SNO
（template: `ros-templates/cluster-stack-sno.yaml`，P3-COST.2 落地后）。

### 3.4 mirror ECS 跟 cluster 共生
mirror ECS 必须在线，集群才能装/起 pod（要么不在 IDMS 内的镜像就拉
不下来，要么 CatalogSource 抓不到）。结论：cluster 在 → mirror 必须在。
所以减少 cluster 时长 = 减少 mirror 时长 = 减少 NAT 实例时长。

### 3.5 预算告警先设了
aliyun 控制台 → 费用中心 → 预算管理 → 设月度 ¥400/¥500 阈值告警，
防止再次失控（这次 ¥800 是观察晚了）。

---

## 4. 决策树：要花一笔大钱前的自查

在执行下面动作之前，先核对预估成本：

```
要新增/重建 mirror tarball?
├─ on-prem build + 上传 → ~¥15 OSS 公网（不可避免）
├─ aliyun jumphost build + 上传 → ~¥0（推荐）
└─ 重建 mirror ECS 时让它下载 tarball → 必须 internal endpoint

要测一个 P3 改动?
├─ controller 代码改 → kind + envtest 本地搞，¥0
├─ 部署/运维改 → SNO 集群 1h，~¥3
└─ 真生产验证 → HA 集群最多 4h，~¥10

要开新 NAT/SLB/EIP?
└─ 先在 99-teardown.yml 加对应清理逻辑，否则忘了关一周 = ¥几十
```

---

## 5. 月报与预算告警

### 5.1 月报脚本

`scripts/aliyun-billing-report.sh` 调 `bssopenapi QueryBillOverview`
拉指定月（默认上月）账单，按 ProductCode 汇总 + 跟前一月对比 + 输出
Markdown：

```bash
# 默认上月报告
scripts/aliyun-billing-report.sh

# 指定月份
scripts/aliyun-billing-report.sh 2026-05

# 加 --detail 钻入前 3 大产品的 instance 级明细
scripts/aliyun-billing-report.sh --detail

# 归档到 git
scripts/aliyun-billing-report.sh > docs/billing/2026-05.md
git add docs/billing/2026-05.md && git commit -m 'docs(billing): 2026-05'
```

调用 QueryBillOverview API 是**免费**的（不收钱）。

可加 cron / systemd-timer 月初自动跑：
```
0 9 1 * * cd /home/sam/work/alibabacloud/openshift-alibaba/alibaba-openshift && \
  scripts/aliyun-billing-report.sh > docs/billing/$(date -d 'last month' +\%Y-\%m).md
```

### 5.2 预算告警（最关键 — 5 分钟一次性配置）

aliyun 控制台 → 费用中心 → 预算管理 → 新建预算：
- 名称：`openshift-alibaba 月度预算`
- 类型：成本预算
- 周期：每月
- 金额：¥500（上限）
- 告警阈值：80%（¥400 时触发短信 + email + 控制台通知）
- 通知对象：你自己 + 任何相关 owner

**这是防再失控的兜底**。这次 ¥800 是没设告警导致月底才发现。

也可以用 aliyun CLI 设（一次性）：
```bash
aliyun bssopenapi CreateCostUnit \
  --UnitName "openshift-alibaba" --OwnerUid "$(aliyun --profile openshift-test sts GetCallerIdentity | jq -r .AccountId)"
# 然后用 Budget API 关联...（详见 aliyun 文档，控制台更直观）
```

---

## 6. 改动历史

| 日期 | 改动 | 节省 |
|:-|:-|:-:|
| 2026-06-02 | OSS endpoint dual-mode (commit 4f2db64) | ~¥150-200/月 |
| 2026-06-02 | SNO ROS 模板 + cluster_topology 开关 (commit d45f16a) | ~¥150/月 dev 集群 |
| 2026-06-02 | 月报脚本 + 预算告警文档（task #37）| 防失控 |
| 2026-06-04 | snapshot 去重 + image cascade 删除 (#38) | ~¥30/月 |
| 2026-06-04 | mirror_instance_type 按 flow 自适应 g7.large (#39) | ~¥135-194/月 |
| 2026-06-05 | snapshot fast-path marker→vdb 真生效 (#40 #45) | ~25 min/install |
| 2026-06-05 | 99-teardown post-cleanup sanity asserts (#44) | 防泄漏 |
| 2026-06-05 | teardown orphan sweep PageSize + 源锁定快照补清 (#47) | ~¥38/月 |
| 2026-06-05 | 手工清 18 个历史孤儿快照 + 1 个 image backing 孤儿 | -¥40/月 一次性 |
