# QUICKSTART — split flow (recommended)

从零部署一套 OpenShift 集群到阿里云，**端到端约 90 分钟**，全程 Ansible
驱动。包含 CCM、（可选）私有 mirror、（post-install）CSI / CAPA。

> **想看每一步细节 / 故障排查 / 命令解释**：[`ansible/README.md`](ansible/README.md)
> **想理解架构 / 为什么是双栈**：[`README.md`](README.md)
> **手动 / 控制台方式（不跑 Ansible）**：[`QUICKSTART-LEGACY.md`](QUICKSTART-LEGACY.md)

---

## 0. 一次性准备（~10 min）

```sh
# Ansible 本体
pip install --user ansible-core>=2.16     # 或者 dnf install ansible-core

# Red Hat offline token（一次性，长期有效）
# 浏览器打开 https://console.redhat.com/openshift/token → Load token → 复制
mkdir -p ~/.openshift
read -s X && echo "$X" > ~/.openshift/offline-token && chmod 600 ~/.openshift/offline-token && unset X

# 配置（改 oss_bucket / aliyun_profile / cluster_name / base_domain 等）
cp ansible/group_vars/all.yml.example ansible/group_vars/all.yml
vi ansible/group_vars/all.yml
```

阿里云一次性开通项目（00-preflight 会替你检查并报错）：

- 控制台开通：OSS / ECS / VPC / NLB / SLB / ROS / RAM / PrivateZone
- NLB service-linked role：
  ```sh
  aliyun resourcemanager CreateServiceLinkedRole \
    --ServiceName nlb.aliyuncs.com \
    --endpoint resourcemanager.aliyuncs.com \
    --profile <your-profile>
  ```

---

## 1. 端到端跑（~90 min）

```sh
cd ansible
ansible-playbook playbooks/site.yml     # Phase 00 → 07
```

`site.yml` 跑完后 cluster 已 install-complete，kubeconfig 已 scp 到跳板。
Phase 08（CAPA / CSI / OADP 等 post-install）需在跳板上跑：

```sh
ssh -i ~/.ssh/openshift_ed25519 root@$(yq '.jump_host_ip' ansible/state.yml)
cd /root/openshift-alibaba/alibaba-openshift
ansible-playbook ansible/playbooks/08-deploy-post-install.yml
```

## 2. 分步跑（调试时）

```sh
cd ansible
ansible-playbook playbooks/00-preflight.yml             # 工具 + 权限自检
ansible-playbook playbooks/01-prepare-iso.yml           # Discovery ISO + 注入 clone-vdb-to-vda
ansible-playbook playbooks/02-import-image.yml          # OSS 上传 + ImportImage
ansible-playbook playbooks/03-create-mirror-stack.yml   # 持久 mirror-stack（VPC+RAM+跳板+mirror ECS）
ansible-playbook playbooks/04-prepare-mirror.yml        # oc-mirror d2m → Quay（mirror_enabled 时）
ansible-playbook playbooks/05-verify-mirror.yml         # 健康检查 + 拍 vda/vdb 快照
ansible-playbook playbooks/06-create-cluster-stack.yml  # 短命 cluster-stack（SG+NLB+DNS+masters/workers）
ansible-playbook playbooks/07-install-cluster.yml       # 驱动 AI 装到 install-complete
```

任何一步失败重跑同一条命令即可（所有 playbook 幂等，`state.yml` 透传 ID）。

---

## 3. 销毁

cluster-stack 销毁后，mirror-stack 保留 → 下次重建直接从 Phase 06 起跑，省 30 min：

```sh
cd ansible
ansible-playbook playbooks/99-teardown.yml -e teardown_target=cluster -e teardown_confirmed=true
```

清整套（mirror + cluster + jump host）：

```sh
ansible-playbook playbooks/99-teardown.yml -e teardown_target=both -e teardown_confirmed=true
```

完整 teardown 模式矩阵（`teardown_target × teardown_preserves_ai × delete_mirror_snapshots`）见
[`docs/TEARDOWN.md`](docs/TEARDOWN.md)。

---

## 常见踩坑

| 现象 | 看 |
|---|---|
| 节点装到一半挂住，怎么恢复？ | [`docs/bootstrap-reboot.md`](docs/bootstrap-reboot.md) |
| China region / quay.io 跨境不稳 | [`docs/MIRROR.md`](docs/MIRROR.md) |
| CCM Service LB / providerID / RAM | [`docs/CCM.md`](docs/CCM.md) |
| 销毁出问题（孤儿资源 / state.yml 字段） | [`docs/TEARDOWN.md`](docs/TEARDOWN.md) |
| ISO → 自定义镜像导入失败 | [`docs/boot-image-import.md`](docs/boot-image-import.md) |
| 想看完整手动操作（含每个命令的预期输出，LEGACY 单栈） | [`docs/legacy/test-walkthrough.md`](docs/legacy/test-walkthrough.md) |
