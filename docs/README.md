# Documentation index

Reference docs for the OpenShift-on-Alibaba deploy automation (ansible). Grouped
by topic; each links the authoritative doc.

## Install & end-to-end

Two installation methods, switched by `installation_method` in `group_vars`:
**Assisted Installer (AI)** → `site.yml`, and **Agent-based Installer (ABI)** →
`site-agent.yml` (fully air-gapped; ENI-first/reimage MAC↔hostname binding). Both
support HA (multi-AZ) and SNO, then share `site-post.yml` for the worker plane.

| Doc | What |
|-----|------|
| [E2E-RUNBOOK](E2E-RUNBOOK.md) | End-to-end install runbook — **both AI (`site.yml`) and ABI (`site-agent.yml`)**, then `site-post`. |
| [COMPONENTS-AND-ORDER](COMPONENTS-AND-ORDER.md) | What gets deployed and in what order (incl. the `08a → 08 → 10 → 12` site-post chain). |
| [POST-INSTALL](POST-INSTALL.md) | Post-install components (CAPA / CSI / OADP). |
| [SNO-MODE](SNO-MODE.md) | Single-Node OpenShift (AI **and** ABI SNO). |
| [TEARDOWN](TEARDOWN.md) | `99-teardown.yml` modes (cluster / mirror / both). |

## Air-gap mirror & boot image
| Doc | What |
|-----|------|
| [MIRROR](MIRROR.md) | Disconnected private mirror registry (architecture / cost / workflow). |
| [SNAPSHOT](SNAPSHOT.md) | Mirror snapshot lifecycle (create / fast-path / delete). |
| [mirror-vdb-persistence](mirror-vdb-persistence.md) | Converge mirror state onto vdb; snapshot vdb only. |
| [boot-image-import](boot-image-import.md) | Import the RHCOS boot image into Alibaba Cloud. |

## CAPA — Cluster API worker provisioning (day-2)
| Doc | What |
|-----|------|
| [CAPA-DAY2-OPS](CAPA-DAY2-OPS.md) | **Day-2 ops master reference**: resource ownership, external CP, scale / rolling / drain, MHC, delete-safety (G8), IMDS hardening (G14), air-gap image strategy. |
| [CAPA-MULTI-AZ](CAPA-MULTI-AZ.md) | Multi-AZ worker pools — one MachineDeployment per zone (B2). |
| [CAPA-AUTOSCALER](CAPA-AUTOSCALER.md) | Cluster Autoscaler (clusterapi provider) + scale-from-zero (B3 / #63). |
| [CAPA-WORKER-JOIN](CAPA-WORKER-JOIN.md) | Worker join *mechanism* (boot image → Ignition → CSR → providerID). Route B (`11`) itself is **legacy** — superseded by Phase 12. |
| [CAPA-SMOKE](CAPA-SMOKE.md) | CAPA smoke-test runbook. |
| [CAPI-CORE](CAPI-CORE.md) | Self-bundled Cluster API core (CRDs + controller). |

> The CAPA **provider** repo (`openshift-capi-alicloud`) has its own docs:
> clusterctl, OLM bundle, RAM policy, integration tests — see its `docs/`.

## Platform integration
| Doc | What |
|-----|------|
| [CCM](CCM.md) | Alibaba Cloud Controller Manager (platform=external contract). |
| [csi-driver-design](csi-driver-design.md) | Alibaba Cloud CSI driver integration. |
| [bootstrap-reboot](bootstrap-reboot.md) | Boot/reboot recovery (clone-vdb-to-vda, ReplaceSystemDisk). |

## Reference
| Doc | What |
|-----|------|
| [COMPATIBILITY-MATRIX](COMPATIBILITY-MATRIX.md) | Version / skew matrix + fork contingency. |
| [COST](COST.md) | Cost rules & savings guide. |
| [P3-ROADMAP](P3-ROADMAP.md) | Roadmap (smoke-test → production-grade). |
| [billing/](billing/) · [legacy/](legacy/) | Billing notes · legacy manual-console walkthroughs. |
