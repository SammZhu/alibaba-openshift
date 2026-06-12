# CAPA multi-AZ worker pools (B2)

Declarative, self-healing worker lifecycle that spreads workers across
availability zones for HA — the production successor to Phase 11's single
hand-crafted Machine.

> Artifacts:
> - [`custom_manifests/capa-worker-machinedeployment.yaml`](../custom_manifests/capa-worker-machinedeployment.yaml) — the multi-AZ chain (Jinja template)
> - [`ansible/playbooks/12-capa-machinedeployment.yml`](../ansible/playbooks/12-capa-machinedeployment.yml) — ensure per-zone vSwitches + apply + validate
> - [`ansible/tasks/ensure_vswitch.yml`](../ansible/tasks/ensure_vswitch.yml) — create a vSwitch per zone if missing

---

## 1. Why per-AZ MachineDeployments

The CAPI failure-domain mechanism: the infra cluster publishes its zones to
`AlibabaCloudCluster.spec.failureDomains` → the controller projects them to
`status.failureDomains` (each with its `vSwitchID` attribute) → the CAPI Cluster
controller copies them to `Cluster.status.failureDomains` → MachineSet assigns
`Machine.spec.failureDomain` → **our controller's `resolveFailureDomain` maps it
to the zone + vSwitch** at RunInstances time. All of this is already built.

Two topologies sit on top of that:

| | **A — one MachineDeployment per AZ (chosen)** | B — single MachineDeployment, spread |
|---|---|---|
| Placement | each MD pins `failureDomain: <zone>` → all its replicas land there | MachineSet round-robins replicas across `status.failureDomains` |
| Per-pool scale / autoscaler | yes — scale each zone independently (B3 scale-from-zero) | no — one knob for all zones |
| Fault / capacity isolation | explicit per zone | implicit |
| Matches | OpenShift machine-api (one MachineSet per AZ) | simplest |

**Topology A** is the default here: it matches the OpenShift convention, gives the
cluster autoscaler (B3) a per-zone pool to scale, and isolates a zone's capacity
exhaustion to its own pool. The shipped manifest creates `caworkers-a/b/c`, one
MachineDeployment per zone, over a shared `AlibabaCloudMachineTemplate`.

B3 per-pool autoscaling (cluster-autoscaler `clusterapi` provider, scale-from-zero)
is wired by the same manifest behind an opt-in flag — see
[CAPA-AUTOSCALER.md](CAPA-AUTOSCALER.md).

## 2. How it composes with the rest

- **B1 (CSR auto-approval)** — every scaled-out node joins unattended.
- **Idempotent create (adopt-by-tag)** — a lost status write never doubles an ECS.
- **PR-B capacity terminal** — a sold-out zone marks the Machine Failed; the
  MachineSet recreates it (in another zone if you let it spread).
- **Hardening (v0.1.12)** — IMDSv2 on, disks die with the instance, tags on
  disks+ENIs, oversized Ignition offloads to OSS — all inherited by every pool.

## 3. Prerequisite — a vSwitch per zone

The SNO cluster ships one vSwitch in `cn-wulanchabu-a`. Phase 12 ensures the other
target zones have one too: it inventories the VPC's vSwitches, and for any target
zone lacking one, creates `capa-worker-<zone>` with a free `/24` carved from the
VPC CIDR (assumes a `/16` VPC with `/24` vSwitches — the cluster's layout). The
three zone→vSwitch pairs are then written into `spec.failureDomains`.

## 4. Self-healing — MachineHealthCheck (#69)

`caworkers-mhc` remediates a worker whose Node is `Ready=False/Unknown` for 5 min
(or never starts within 20 min). Combined with the controller's
`InstanceDisappeared` terminal — set when `DescribeInstances` can't find the ECS
(console release / host failure) — a worker that vanishes out-of-band is marked
Failed and rebuilt by its MachineSet. `maxUnhealthy: 40%` caps concurrent
remediation so a control-plane blip can't drain every pool at once.

## 5. Run + validate

```bash
ansible-playbook -i ansible/inventory.yml ansible/playbooks/12-capa-machinedeployment.yml \
  [-e replicas_per_zone=1] [-e worker_instance_type=ecs.g7.xlarge]
```

The playbook asserts, on-cluster:
1. `Cluster.status.failureDomains` is populated (spread prerequisite).
2. all worker MachineDeployments reach desired replicas (nodes Ready, CSRs auto-approved).
3. workers occupy **distinct zones** (HA spread) — distinct `AlibabaCloudMachine.spec.zoneID`.
4. scaling `caworkers-a` up by 1 converges (declarative scale-up).

### Unified-validation checklist (one fast-path cluster run)
The procedures, pass criteria, and gotchas for all of these now live in
[CAPA-DAY2-OPS.md](CAPA-DAY2-OPS.md). Status (2026-06-11):
- [x] 3 pools × N replicas Ready, spread across a/b/c
- [x] scale up / down a pool converges; rolling replace on a template change
- [x] `oc delete machine <one>` → drained + ECS released → MachineSet recreates
- [x] **#69**: stop one ECS (aliyun) → node NotReady → MHC remediates → MachineSet
      rebuild *(needed the MHC v1beta2 field fix — see CAPA-DAY2-OPS §4)*
- [ ] capacity: force a sold-out zone → PR-B terminal → rebuild elsewhere
- [x] hardening: post-boot IMDSv2 flip (G14, httpTokensAfterBoot) *(code v0.1.19,
      live-verify pending)*; deleting a Machine leaves no orphan ECS/disk/ENI
      (G8 — verified by the cost audit, see CAPA-DAY2-OPS §5)

> Cost: multi-replica stress (e.g. 50) is intentionally **not** run. A few
> replicas across a/b/c is enough to prove the logic; teardown sweeps the pools.
