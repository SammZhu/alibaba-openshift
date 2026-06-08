# Components & deployment order

Running OpenShift on Alibaba Cloud (via `platform: external`) is a collaboration
of **three integration components** on a shared foundation. They are intentionally
decoupled — each owns one layer and none imports another — so they are deployed,
versioned, and (eventually) packaged independently. This page explains *who does
what*, *how they cooperate*, and *the order to install them in*.

---

## 1. The three components (+ foundation)

| Component | Role | Maintained by | Packaging direction |
|---|---|---|---|
| **CAPA** — Cluster API provider | Creates/deletes ECS for each Machine; writes the Machine `providerID`; declarative multi-AZ worker lifecycle | **this project** | **→ becoming its own Operator** (in progress; OLM bundle prepared) |
| **CCM** — cloud-controller-manager | Initializes Nodes (removes the `uninitialized` taint, sets Node `providerID`/addresses/zone labels); provisions `Service` load balancers (SLB) | **upstream community** (`cloud-provider-alibaba-cloud`) | **stays as the upstream static manifest** — deliberately *not* re-packaged, to track upstream |
| **CSI** — storage driver | Dynamic Disk/NAS persistent volumes | upstream community (mirrored here) | **→ will be packaged as a separate Operator** (future) |

> **Foundation** (must exist first): an OpenShift cluster on `platform: external`,
> an aliyun-platform RHCOS boot image (Phase 10), and Alibaba Cloud credentials.

Three components, three independent lifecycles. CAPA and CSI evolve toward their
own Operators; CCM stays a thin upstream-maintained manifest on purpose.

---

## 2. How they cooperate

```
            ┌──────────────────────────────────────────────┐
            │                 CAPI core                     │  the framework
            │      Cluster · Machine · MachineDeployment     │
            └───────────────────┬──────────────────────────┘
                                │ derives a Machine (+ failureDomain)
                                ▼
   ┌──────────┐  creates ECS  ┌──────────────┐  initializes Node  ┌──────────┐
   │   CAPA   │ ────────────▶ │  ECS / Node   │ ◀───────────────── │   CCM    │
   │ (ours)   │ writes Machine│  (worker)     │ removes uninit taint│(upstream)│
   └──────────┘  providerID   └──────┬───────┘ Node providerID/    └──────────┘
                                      │         addresses/zone + SLB
                                      │ mounts PV
                                      ▼
                               ┌──────────┐
                               │   CSI    │  Disk/NAS volumes
                               └──────────┘
```

The handoff in words:
1. **CAPI core** decides a worker is wanted and creates a `Machine`.
2. **CAPA** turns that Machine into an **ECS instance** and stamps the Machine's
   `providerID`. *CAPA never touches the Node object.*
3. The node boots RHCOS, reads its Ignition, and the kubelet **joins** — but it
   registers with the `node.cloudprovider.kubernetes.io/uninitialized` taint.
4. **CCM** removes that taint and finishes Node init (providerID, addresses, zone
   labels), and wires up any `Service type=LoadBalancer`. *Only CCM does this.*
5. **CSI** gives workloads on that node dynamic Disk/NAS volumes.

Each layer is independently useful and independently replaceable.

---

## 3. Deployment order (and why)

Install in dependency order. The rule of thumb: **a node is only usable once CCM
has initialized it**, and **CAPA only works once CAPI core is present**.

| # | Step | Why this order |
|---|---|---|
| 0 | Foundation: cluster on `platform: external`, RHCOS aliyun image, credentials | everything below assumes these |
| 1 | **CCM** (Phase 01) | must be present from node bring-up — *every* node (control plane included) comes up tainted `uninitialized` and is unusable until CCM clears it |
| 2 | **CAPI core** (`clusterctl init`, Phase 11) | the framework CAPA plugs into; without it there are no Machine/MachineDeployment objects to drive CAPA |
| 3 | **CAPA** (Phase 08/11) | provisions workers; consumes CAPI core, relies on CCM (step 1) to make those workers usable |
| 4 | **CSI** (P3-CSI) | independent; install any time after the cluster is up, before workloads that need persistent volumes |

**If the order is wrong / a piece is missing:**

| Missing or late | Symptom |
|---|---|
| CCM | workers (and even control-plane nodes) come up but stay unschedulable — `uninitialized` taint never clears, Machines never reach `Running`, `Service` load balancers stay `Pending` |
| CAPI core | CAPA has nothing to reconcile against — no Machines, no declarative scaling |
| CSI | the cluster and nodes are fine, but PVC-backed workloads stay `Pending` |

> CCM being missing is the most deceptive failure: the node looks `Ready` but
> nothing schedules on it.

---

## 4. Safety net — the provider tells you when a dependency is missing

You do not have to diagnose the "Ready but unusable" trap by hand. The CAPA
controller runs a **preflight** (P3-CAPA.27): if newly provisioned workers stay
`uninitialized`-tainted past a threshold (CCM absent), or if CAPI core CRDs are
missing, it sets a clear degraded **Condition** (`CloudControllerManagerMissing` /
`ClusterAPICoreMissing`) and emits a **Warning Event** — instead of silently
leaving dead nodes behind. CAPA still creates the ECS (tolerating deploy-order
skew), but it tells you loudly what to fix.

---

## 5. Packaging roadmap (why three, not one)

- **CAPA** → its own Operator (OperatorHub community / clusterctl provider). In progress.
- **CSI** → its own Operator, later. (Heavier than CAPA: a privileged node
  DaemonSet + sidecars + SCC, so it earns a separate effort.)
- **CCM** → unchanged. It is upstream-owned and a thin manifest; re-packaging it
  would only add drift. We depend on it, we document it, we **don't fork it** —
  *in steady state*. The contingency for when upstream stalls / ships a CVE /
  breaks on a new OCP is spelled out in
  [COMPATIBILITY-MATRIX.md §4](COMPATIBILITY-MATRIX.md) (fork as a bridge, with an
  explicit exit criterion).

Keeping them as **three independent units** matches their independent lifecycles.
When both CAPA and CSI are Operators in the same catalog, OLM can declare an
inter-operator dependency so installing one pulls the other; until then, this page
is the contract — install the three in the order above.
