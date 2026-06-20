# Single Node OpenShift (SNO) Mode

Run a one-ECS OpenShift cluster instead of the 3-master HA flavor.
Targets dev / smoke / iteration workflows where HA isn't being
tested — saves ~67% of the cluster's ECS cost (~¥150-200/month at
current usage).

For when to use which, see §4.  For pricing detail, see [COST.md](COST.md).

---

## 1. Quick start

```yaml
# ansible/group_vars/all.yml
cluster_topology: sno      # default is 'ha'
```

Everything else stays the same.  Run the normal flow:

```bash
ansible-playbook -i inventory.yml playbooks/site.yml
```

Site.yml drives 00 → 07.  The `cluster_topology` switch is read by:
- `01-prepare-iso.yml`: sets AI `high_availability_mode=None` +
  `control_plane_count=1`
- `06-create-cluster-stack.yml`: picks `ros-templates/cluster-stack-sno.yaml`
  instead of `cluster-stack.yaml`
- `07-install-cluster.yml`: derives `expected_hosts=1` for the host-discovery
  poll and master-IP sanity assert

No other changes needed — the ROS template's outputs are name-compatible
with the HA template (MasterIp2/MasterIp3/WorkerSecurityGroup return
empty strings).

### Agent-based (ABI) SNO

SNO also works air-gapped via the Agent-based Installer — set both knobs and
run `site-agent.yml` instead of `site.yml`:

```yaml
# ansible/group_vars/all.yml
cluster_topology:    sno
installation_method: Agent-based
rendezvous_ip:       10.0.16.5      # default is fine — see below
```

```bash
ansible-playbook -i inventory.yml playbooks/site-agent.yml
```

Same ENI-first/reimage flow as ABI HA, just one node: 06 boots the single master
from a placeholder image and harvests its primary-NIC MAC, 06a bakes a one-host
`agent-config.yaml` (`<cluster>-master-1`), 06b reimages it to the agent image in
place. The count is driven by `cluster_topology` end to end
(`_eff_control_plane_count=1`, `abi_master_macs` has one entry, the SNO template
emits one `RendezvousInstance` + `Master1PrimaryNic`).

The **ABI SNO master lands in zone1** (`PrivateVSwitchId`, 10.0.16.x) to match
the HA multi-AZ master-1 and the default `rendezvous_ip: 10.0.16.5`, so the fixed
rendezvous IP falls in the master's subnet with no extra config. (AI SNO keeps
zone2 with an auto-allocated IP — unchanged.)

---

## 2. What SNO actually is

OpenShift Single Node ("SNO") collapses control-plane + worker into
one node:

- **1 ECS** instead of 3 masters + N workers
- **Single etcd member** (no Raft quorum; etcd runs degraded-by-design)
- **No NLB** for the API — the api-int endpoint A-records straight to
  the master's private IP
- **Router-default** (ingress) runs on this same node via hostNetwork
- **bootstrap-in-place**: no separate bootstrap node; the single master
  bootstraps itself

What you give up:
- HA (the obvious one — node loss = cluster loss)
- Multi-zone tolerance
- Rolling upgrade safety (no spare master to drain to)
- Some workload types that require ≥2 nodes (e.g. NAS RWX
  live-migration test in P3-CSI.3)

What still works:
- All OCP operators that don't need ≥2 nodes (CCM, CSI disk driver,
  OADP, CAPA, the bulk of P3 testing)
- platform=external + Alibaba CCM integration
- cri-o + mirror registry (IDMS rules unchanged)
- Discovery ISO + Assisted Installer flow (AI handles SNO directly)

---

## 3. ROS template differences

`ros-templates/cluster-stack-sno.yaml` (this template) vs
`ros-templates/cluster-stack.yaml` (HA):

| Resource | HA | SNO |
|:-|:-:|:-:|
| ControlPlaneSecurityGroup | ✓ | ✓ |
| WorkerSecurityGroup | ✓ | — (master SG covers all roles) |
| Cross-SG ingress (CPSGFromWorkers, etc.) | ✓ | — |
| SSH from jump host | ✓ | ✓ |
| ApiNLB + ServerGroups + Listeners | ✓ (2 listeners) | — |
| McsServerGroup + Listener | ✓ | — |
| PrivateZone | ✓ | ✓ |
| api / api-int records | CNAME → NLB | A → master IP |
| `*.apps` record | A → IngressVip | A → master IP |
| etcd-0 / etcd-1 / etcd-2 records | 3 records | etcd-0 only |
| ReverseZone1 / ReverseZone2 | ✓ | ✓ |
| RendezvousInstance (master-1) | ✓ | ✓ |
| ControlPlaneInstance2 / 3 | ✓ | — |
| WorkerInstanceGroup | conditional | — |

Outputs (kept name-compatible):
- `ApiLBEndpoint`: master IP on SNO (vs NLB DNS on HA)
- `MasterIp1`: master IP (both)
- `MasterIp2`, `MasterIp3`: empty string on SNO (vs IPs on HA)
- `WorkerSecurityGroup`: empty string on SNO (vs SG ID on HA)

---

## 4. When to use SNO vs HA

| Scenario | Use |
|:-|:-:|
| P3-CAPA controller code changes + smoke (single Machine create/delete) | **SNO** |
| P3-CSI disk driver E2E (PVC bind / mount / snapshot / restore) | **SNO** |
| P3-CSI NAS RWX live-migration test (needs ≥2 nodes) | **HA** |
| OpenShift Virtualization VM live-migration | **HA** |
| Validating CCM behaviour on real LoadBalancer Services | either |
| Mirror registry / IDMS verification | **SNO** |
| Pre-production / customer-facing reference cluster | **HA** |
| Anything HA-specific (etcd quorum loss, master eviction, etc.) | **HA** |

Rough cost per day (ECS portion only, pay-by-use, cn-wulanchabu, masters
on ecs.g7.xlarge ≈ ¥0.81/h):

- **HA (3 masters + 1 jumphost)**: ~¥78/day at 24h uptime, ~¥20/day
  for typical 6h dev session + teardown
- **SNO (1 master + 1 jumphost)**: ~¥26/day at 24h uptime, ~¥7/day
  for typical 6h dev session + teardown

Plus on HA: NLB intranet base ~¥1.44/day, gone on SNO.

---

## 5. Switching between topologies

The topology is decided at `01-prepare-iso.yml` (AI cluster create) +
`06-create-cluster-stack.yml` (ROS stack create) time.  **You cannot
flip a running cluster between sno and ha** — the AI cluster object's
high_availability_mode is immutable after creation, and the ROS stack
schemas are entirely different (NLB present/absent, etcd records 1 vs
3, etc.).

To switch:
1. Teardown the existing cluster: `ansible-playbook playbooks/99-teardown.yml -e teardown_target=cluster -e teardown_confirmed=true`
2. Edit `group_vars/all.yml` to flip `cluster_topology`
3. Re-run `playbooks/site.yml`

The mirror-stack is topology-agnostic — keep it (snapshot-restore
fast-path applies as usual, see [E2E-RUNBOOK.md](E2E-RUNBOOK.md)).

---

## 6. Known caveats

### 6.1 No `worker_sg` for downstream consumers
HA's `worker_sg` output is consumed by:
- P3-CAPA smoke tests (AlibabaCloudMachine.spec.securityGroupIDs)
- Any future MachineDeployment templates

On SNO this output is `""`.  Consumers that need an SG to attach
future CAPA-created workers to should use `ControlPlaneSecurityGroup`
(the master SG, which already allows kubelet + ingress + NodePort).

The smoke-test YAML in `docs/CAPI-CORE.md` §3.4 hardcodes a specific
SG ID from a prior HA run (`sg-0jl5x011zk06q6vu94kj`).  When running
that smoke on SNO, look up the actual master SG with:
```
aliyun ecs DescribeSecurityGroups --SecurityGroupName cluster1-master-sg
```

### 6.2 etcd quorum loss = cluster loss
Single etcd member.  Master ECS restart = brief API outage (~30 s).
Master ECS data disk loss = unrecoverable cluster (no quorum to
restore from).  This is by design for SNO — don't run anything that
matters on it.

### 6.3 Install timing
SNO install is faster than HA (no bootstrap-node-to-3-master handoff):
- HA: ~30-40 min from "Installing" to "installed"
- SNO: ~20-25 min

`07-install-cluster.yml`'s Phase A / B poll loops handle both
transparently — they wait for status transitions, not wall-clock time.

### 6.4 *.apps DNS still PATCHed by Phase 07
Phase 07 has a task that rewrites the `*.apps` PrivateZone record to
the router-default Pod's host IP after install completes (because
the install-time record points at IngressVip, which on SNO doesn't
exist — but the ROS template seeds the A record at the master IP, so
the PATCH is a no-op on SNO anyway).  Costs nothing; leave as-is.

---

## 7. Version history

| Date | Note |
|:-|:-|
| 2026-06-02 | Initial — `cluster-stack-sno.yaml` + `cluster_topology` toggle added (commit TBD).  Saves ~¥150-200/mo on dev clusters. |
