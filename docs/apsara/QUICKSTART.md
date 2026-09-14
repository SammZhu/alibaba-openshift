# 专有云快速上手 —— 在 Apsara Stack 上从零装出一套 OpenShift

这份文档面向**刚拿到一套专有云环境、并不熟悉 OpenShift** 的人。目标是:把仓库
拉下来,填一个配置文件,跑一条命令,得到一套可用的集群。

跑完你会得到:

- 一套 OpenShift 集群(默认 3 master + 2 worker),集群内所有镜像来自环境内部的
  私有 mirror,不依赖跨境网络
- 块存储和(环境支持时)文件存储,都能真实创建/挂载/快照
- 需要时可用的 LoadBalancer Service(CCM)
- 一个 worker 池,改一个数字就能扩缩容

**时间量级**(首次,单套环境实测的量级,不是保证值):准备 helper 半小时,填配置
半小时,其余全自动约 5–7 小时,其中构建镜像内容(mirror-build)独占 1–2 小时。
同一个 bucket 里已经有同版本内容时,重建约 3 小时。

> 这条链在 `ste2` 专有云上从空环境完整跑通过(装机 → 存储 → CCM → worker 全绿)。
> 换一套环境时,**配置值几乎没有一个能照抄**——这份文档里凡是标 ⚠ 的地方,都是
> 在真实环境上因为「抄了另一套环境的值」而付出过代价的。

---

## 0. 你需要先拿到什么

### 向环境交付方要这些

| 要什么 | 用来干什么 | 没有会怎样 |
| --- | --- | --- |
| 一台 **helper 机器**(环境内网的 ECS,RHEL 9 / Alibaba Cloud Linux 3,≥4 vCPU / 16 GiB / 100 GB 空闲盘) | 跑所有自动化,也是唯一需要你登录的机器 | 什么都开始不了 |
| 一组 **RAM 子账号 AK/SK** | 建云资源 | — |
| **组织 ID + 资源集 ID**(ASCM 里的 `org-…` / `rs-…`) | 每个 API 请求都要带 | 所有调用 Forbidden |
| 一个 **OSS bucket**,归属上面那个 AK/SK 的账号 | 存镜像内容 | ⚠ 用别的账号建的 bucket,端点全对也会 AccessDenied |
| helper 能不能直接访问 OpenAPI 端点,还是要走代理 | 决定要不要设 `APSARA_PROXY` | — |

helper 这台机器本身怎么申请、要开哪些网络,单独写在
[OPERATOR-HOST.md](OPERATOR-HOST.md)。**新环境先看那份**,这台机器不存在的话下面
一步都做不了。

### 你自己要准备的两个 Red Hat 凭据

都免费,注册一个 Red Hat 账号即可:

```sh
mkdir -p ~/.openshift
# 1. pull secret:https://console.redhat.com/openshift/install/pull-secret
#    下载后放到 ~/.openshift/pull-secret.json
# 2. offline token:https://console.redhat.com/openshift/token
#    页面上 Load token,复制,存成 ~/.openshift/offline-token
chmod 600 ~/.openshift/*
```

这两个文件要放在 **helper 上**(不是你的笔记本上)。

---

## 1. 准备 helper(约 30 分钟,大部分是等下载)

在 helper 上,以 root 执行。第一步只装 git 和 ansible,剩下的全是 playbook 干的:

```bash
# 1) 最小引导:只装 git + ansible + python3,其余一概不做
#    这台机器还没有 git,所以先把这一个脚本弄上去(scp,或从仓库 raw 地址 curl)
PROXY=http://<代理地址>:3128 ./bootstrap-operator.sh
```

```bash
# 2) 把仓库拉下来(需要代理时先 export https_proxy)
git clone <仓库地址> /root/alibaba-openshift
```

```bash
# 3) 其余全部自动:装包、生成 SSH 密钥、编译专有云用的 Go 工具、
#    clone 三个配套仓库、装 oc/openshift-install
cd /root/alibaba-openshift/ansible
ansible-playbook -i inventory.yml playbooks/00a-prepare-operator.yml \
  -e cloud_platform=apsara -e operator_proxy=http://<代理地址>:3128
```

这一步可以反复跑,已经做好的会自动跳过。填好 `all.yml` 之后就不用再带 `-e` 了,
它会自己从配置里读。

> 第 3 步也包含在后面的一键入口里(阶段 `00a`)。这里单独先跑一次,是因为它同时
> 验证了「helper 能不能装包、能不能连 GitHub」——这两件事不成立的话,后面的
> 失败会以各种不相干的面目出现。

---

## 2. 填配置

### 先让脚本把能探的都探出来

这套环境卖什么规格、什么盘型、端点长什么样、有没有 NAS —— 这些都是可以问出来的,
不必手查:

```bash
cd /root/alibaba-openshift
AK=<你的AK> SK=<你的SK> ORG_ID=org-xxxx RG_ID=rs-xxxx \
  python3 scripts/apsara/discover.py -o ansible/group_vars/all.yml
```

它在 helper 上跑,做这几件事:

1. 从元数据服务读 region、helper 自己的 VPC 和网段
2. 按见过的**十种域名形状**逐个试每个产品的端点,每个候选都**真的发一次只读调用**
   ——不是猜,是打通了才算
3. 用探到的 ECS 端点查:可用区、可用镜像、可用实例规格 ∩ 镜像支持的规格、可用盘型
4. 有 NAS 的话,查哪个盘类**真的**能开 NFS(列出来但 Protocol 为空的不算)
5. 看 CPU 厂商,决定要不要 `bootimage_force_tcg`
6. 检查 helper 的 VPC 网段和默认的 `10.0.0.0/16` 有没有重叠

然后以 `all.yml.apsara.example` 为底稿写出一份填好的 `group_vars/all.yml`(权限
600,里面有 AK/SK,该路径已被 gitignore)。

**探不出来的值一律写成 `CHANGEME` 并说明原因,不猜。** 猜出来的值会一路跑到很深
的地方才暴露,而那时症状通常指向别的东西。

不加 `-o` 就只打印不写文件(AK/SK 会打码),可以先看一眼再决定。

### 再手工补三项

脚本问不出来的,是那些「你想要什么」而不是「环境有什么」的:

```bash
vi ansible/group_vars/all.yml
```

- `cluster_name` —— 集群名,小写字母数字,≥3 字符
- `base_domain` —— 域名后缀,专有云内部域名即可
- `openshift_version` —— ⚠ 必须是 `X.Y.Z`,不能写 `4.20`

再把脚本标了 `CHANGEME` 的那几项处理掉(通常是端点没探到,见下面「端点怎么探」)。

模板里每一项上面都写了**这个值从哪来**,想手工核对时照着查。下面几段是最贵的几个坑。

### ⚠ 核心原则:值不可移植

专有云和公有云最大的差别不是「API 不一样」,而是**每套环境卖的东西不一样**。
下面这些在另一套环境上成立的事,在你这套上可能完全不成立:

| 这一项 | 为什么不能抄 |
| --- | --- |
| API 端点 | 没有命名规律。同一套环境里能同时出现 `ros.cloud.X`、`ecs-internal.cloud.X`、`slb-vpc.cloud.X`、`dns-control.pop.cloud.X`、`nas-pub.<region>.cloud.X` 五种形状 |
| 有没有 NAS | 有的环境根本没部署 |
| NAS 盘类 | 有的环境 `Performance` 是空的,只有 `Capacity` 能用 |
| ECS 镜像 / 实例规格 / 盘型 | 公有云的 `g7` 系列、`cloud_essd` 通常一个都没有 |
| 节点的网关和 DNS | 见下面那条 |
| CPU 厂商 | 决定 phase 10 能不能用 KVM(海光 C86 不行,必须软件模拟) |

完整索引在 [OPERATOR-HOST.md](OPERATOR-HOST.md) 的
「What does not travel between environments」。

### 端点怎么探(discover.py 没探到时)

`discover.py` 已经把十种形状都试过一遍了。它报 `CHANGEME` 的产品,要么这套环境
真的没有,要么域名形状是个新的。手工再看一遍:

```bash
# 只看 DNS 和端口通不通
scripts/apsara/probe-endpoints.sh cloud.<你的环境域名>
```

```bash
# 再发真实调用,确认端点+产品+版本都对
AK=.. SK=.. REGION=.. ORG_ID=.. RG_ID=.. \
  scripts/apsara/probe-endpoints.sh cloud.<你的环境域名> --call
```

读结果:

- `asapiSuccess:true` → 这个端点对
- `InvalidAction.NotFound` / `InvalidVersion` → 端点通,动作名或版本不对
- `503` / `no such host` → 端点不对,换一个形状
- 某个产品**所有形状全部 DNS 失败** → 这套环境没部署这个产品。这是一个**真实答案**,
  不是探测失败

探出了新东西,请顺手加回 `scripts/apsara/discover.py` —— 下一套环境就不用再撞一次。
那里有**两张表**,别加错:

- `PATTERNS` 是**形状**(`{svc}.{domain}` / `{svc}-pub.{region}.{domain}` …)
- `PROBES` 里每个产品的第一项是**域名标签**,它和 API 产品名是两回事:CloudDns
  的产品名是 `CloudDns`,主机名却是 `dns-control`(ste2 实测 `dns-control.pop.cloud.ste2.com`
  在,`clouddns.*` 十种形状一个都不在)

### ⚠ 节点 DNS:填错了最贵的一项

`apsara_node_dns` **默认不要填**。不填时,06a 会从 mirror 机器(和集群节点同网段)
自动读出可用的解析器。

手填的代价:这两个地址会成为节点**唯一**的解析器。填了一个不应答的地址,每台节点
都会停在「已注册但未验证」,而报错完全不指向 DNS。这件事真实发生过——五台机器卡了
一小时,原因是配置从另一套环境抄了过来。

### helper 怎么够到 mirror:用 (B)

专有云里没有公网跳板机,helper 必须能直连 mirror 机器的私网 IP。模板里给了两条路,
**填 (B)**:`apsara_peer_operator_vpc_id` 设成 helper 自己的 VPC id,03 会自动建
对等连接、双向路由、放行安全组,99 拆的时候反向解开。

(A)「把 mirror 建进 helper 的 VPC」只有 mirror-stack 支持,集群栈和私有 DNS 没有
对应接线,**没有实测过**——除非你有特别的理由,别走那条。

唯一要留意的:(B) 新建的那个 VPC 网段(默认 `10.0.0.0/16`)**不能和 helper 自己的
VPC 网段重叠**。查一下:

```bash
curl -s http://100.100.100.200/latest/meta-data/vpc-cidr-block
```

### 没有 NAS 的环境怎么办

**什么都不用做**:把 `cloud_env.ENDPOINT_NAS` 留空就行。

留空时 `nas_available` 自动变成 false,于是:CSI 不部署 NAS 驱动、08 不建 NAS
StorageClass、13 的 `nas-rwx` 检查报 SKIP 而不是 FAIL。整条链正常跑完,块存储照常
可用。**不要为了「看起来完整」随便填一个端点**——那会让链条去用一个不存在的服务。

---

## 3. 跑(一条命令)

```bash
cd /root/alibaba-openshift/ansible
ansible-playbook -i inventory.yml playbooks/site-apsara.yml
```

开跑前它会先打印这次要执行的阶段,并在几秒钟内检查配置是否完整——**配置缺项在
这里就会报错,不会让你等到半小时后**。

它按这个顺序走:

| 阶段 | 做什么 | 量级 |
| --- | --- | --- |
| `00a` | 准备 helper(已经跑过就基本是空转) | 分钟 |
| `00` | preflight:工具、凭据、规格库存 | 分钟 |
| `mirror-build` | 拉取 25–30 GB 镜像内容并上传到 OSS | **1–2 小时** |
| `03` | 建持久层:VPC / NAT / RAM 角色 / mirror 机器 | ~10 分钟 |
| `04` | 在 mirror 机器上起 Quay,把内容导进去 | 40–60 分钟 |
| `05` | mirror 健康检查 + 拍快照 | 分钟 |
| `08b` `08c` `08d` | 在 helper 上编译三个专有云版镜像,推回 mirror | ~15 分钟 |
| `06` `06a` `06b` | 建集群栈 → 造 agent ISO → 换镜像重启 master | ~30 分钟 |
| `07` | 等集群装完 | 60–90 分钟 |
| `08a` `08` | CAPI core、CAPA provider、CSI、存储类 | ~20 分钟 |
| `10` `12` | 重压 worker 启动镜像 → 建 worker 池 | ~30 分钟 |
| `13` `14` `15` | 验收:存储、LoadBalancer、数据面 | ~15 分钟 |

### 断了怎么续

每个阶段都是幂等的,**从断掉的那个阶段接着跑**:

```bash
ansible-playbook -i inventory.yml playbooks/site-apsara.yml -e apsara_from=06
```

其它两个开关:

```bash
# 只跑到某一步为止(例如只把 mirror 建好,先不装集群)
ansible-playbook ... playbooks/site-apsara.yml -e apsara_to=05
```

```bash
# 跳过某几步。最常用的是 mirror-build:OSS 里已经有同版本内容时,
# 没必要再花一两个小时重传
ansible-playbook ... playbooks/site-apsara.yml -e '{"apsara_skip":["mirror-build"]}'
```

阶段名就是上表第一列。想全程看进展:

```bash
tail -f /root/alibaba-openshift/ansible/logs/ansible.log
```

### 关于顺序的三件事(想改顺序之前先读)

1. **`08b/08c/08d` 在装机之前,不在装机之后。** 这三个镜像带着专有云需要的修改,
   是在 helper 上编译、层叠到 mirror 里已有的那层再推回去的。其中 `08d` 产出的
   CCM 镜像 digest 会被 `06a` **烤进 agent ISO**——装机时第一个节点开机就要用它,
   那时集群还不存在,没有任何机制能事后重定向。放到装机后,就只能靠 `08e` 补一次。
2. **`08a` 在 `08` 之前。** CAPI core 不在,`08` 装出来的 provider 直接 CrashLoop,
   而报的错看起来是 provider 自己的问题。
3. **`10` 在 `12` 之前。** `12` 建 worker 用的是 `10` 重压出来的镜像。

---

## 4. 怎么知道真的装成了

**别看退出码,看产物。** 一条 play 因为没匹配到主机而被整段跳过,退出码也是 0。

```bash
export KUBECONFIG=/root/openshift-install/<cluster_name>/auth/kubeconfig

oc get nodes                    # 期望:master 和 worker 都 Ready
oc get co                       # 期望:34 个,AVAILABLE=True,DEGRADED=False
oc get machinedeployment -A     # 期望:worker 数 = 期望值
```

`13`/`14`/`15` 三步会自己打印结论:

- `13` 存储:`disk-fs` / `disk-block` / `snapshot` 必须 PASS;`nas-rwx` 在有 NAS
  的环境应该 PASS,没有 NAS 的环境显示 SKIP(**不是失败**)
- `14`/`15` CCM:LoadBalancer Service 拿到 EXTERNAL-IP,并且流量真的能从那个地址
  打到 Pod 里

登录控制台:

```bash
oc whoami --show-console
# 密码在 /root/openshift-install/<cluster_name>/auth/kubeadmin-password
```

---

## 5. 常见症状对照

| 你看到的 | 真正的原因 | 怎么办 |
| --- | --- | --- |
| 节点全部「已注册但未验证」,卡很久 | 节点 DNS 不应答(多半是从别的环境抄来的) | 清空 `apsara_node_dns`,让 06a 自动探;从 `06a` 重跑 |
| 装机停在 ~50%,节点 NotReady 且带 `uninitialized` 污点 | CCM 连不上云,永远清不掉污点 | `-e ccm_enabled=false` 重装,或确认 `08d` 在 `06a` 之前跑过 |
| LoadBalancer Service 永远 Pending | `cloud_env.ENDPOINT_SLB` 没填 | 探出来填上,重跑 `08e` |
| 存储三项一起 FAIL,但卷看着是 Bound | 不是存储坏了,是 kubelet 证书过期导致 `oc exec` 失效 | 重跑 `08`(开头会批 CSR) |
| phase 10 卡住四分钟后超时 | libguestfs 在这台 CPU 上起不来(海光 C86) | `-e bootimage_force_tcg=true` |
| mirror 机器起不来,cloud-init 装不上 podman | 镜像默认的软件源在这个 VPC 里到不了 | 填 `apsara_pkg_repo_mirror_host` / `_ip` |
| 每个云调用都返回 Squid 的 ERR_DNS_FAIL | 代理只有公网 DNS,解析不了内部域名 | 不要设 `APSARA_PROXY`;确认 `HTTP_PROXY`/`HTTPS_PROXY` 在 `cloud_env` 里被清空 |

更细的排查思路(「慢 vs 挂」怎么判断、日志看哪里)在 [DEPLOY.md](DEPLOY.md)。

---

## 6. 拆除

拆除永远是显式的,不包含在任何一键入口里。

```bash
# 只拆集群,保留 mirror(下次重建省掉一两个小时)
ansible-playbook -i inventory.yml playbooks/99-teardown.yml \
  -e teardown_target=cluster -e teardown_confirmed=true
```

```bash
# 全部拆掉
ansible-playbook -i inventory.yml playbooks/99-teardown.yml \
  -e teardown_target=both -e teardown_confirmed=true
```

⚠ 专有云常常是**多租户共用**的。拆除只会动本次部署建出来、并且**有归属证据**的
资源;归属不明的一律留着并打印出来,请人工确认。这个约束是有来历的:一次
NAS 清理曾经差点按名字删掉另一个租户的文件系统。

拆除的完整模式矩阵见 [../TEARDOWN.md](../TEARDOWN.md)。

---

## 7. 已知边界

- **多可用区没验过。** 目前手上三套专有云都是单可用区,`worker_zone_count` 只能是
  1。代码里有多 AZ 的路径(公有云上验过),但在专有云上**没有任何环境能证明它**。
- **只支持 Agent-based 安装(ABI)。** Assisted 需要节点连到 `api.openshift.com`,
  专有云里到不了。
- **不同环境的 CCM 覆盖面不同。** 目前只测过 `DescribeLoadBalancers` 这一个调用的
  响应格式;别的调用可能有各自的差异,用 `scripts/apsara/ccmprobe shapecheck` 去
  测,不要猜。

---

## 继续读

| 想知道 | 看 |
| --- | --- |
| helper 这台机器怎么申请、要开哪些网 | [OPERATOR-HOST.md](OPERATOR-HOST.md) |
| 每一步到底做了什么、为什么是这个顺序 | [DEPLOY.md](DEPLOY.md) |
| ROS 模板为什么要转换、端点/协议对照表 | [ROS-AND-RPC.md](ROS-AND-RPC.md) |
| NAS 那条线的完整闭环路径 | [NAS.md](NAS.md) |
| 公有云怎么装 | [../../QUICKSTART.md](../../QUICKSTART.md) |
