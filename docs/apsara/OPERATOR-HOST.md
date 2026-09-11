# Provisioning the operator host (the "Helper") on a new Apsara environment

Everything in this repo runs from one Apsara-internal ECS: ansible, the Apsara
Go tools, the container builds, and the mirror content build. Standing it up is
the first step on a new environment — `scripts/probe-endpoints.sh` and every
playbook need a machine inside the Apsara network to run on, so nothing else can
be discovered until this box exists.

`docs/apsara/DEPLOY.md` covers the run. This covers getting the box.

## What it does, which is why the requirements are what they are

| Job | Consequence |
| --- | --- |
| Runs ansible and `oc` | modest CPU |
| Builds the CAPA / CSI / CCM images (08b/08c/08d) with podman | needs podman + disk |
| Builds the mirror content (`mirror-build.yml`, ~1-2 h) | **the disk driver**: oc-mirror v2 keeps `~/.oc-mirror` as a blob cache |
| Renders every manifest (`delegate_to: local`) | the repo checkout lives here, and only here |
| SSHes to the mirror ECS at its **private** IP | must have a route to the stack VPC |

## Sizing

| | Documented minimum | Measured on the completed dyz7 Helper |
| --- | --- | --- |
| Instance type | — | **`ecs.s7-k-c1m2.2xlarge`** (8 vCPU / 16 GB) |
| Disk | ~60 GB **free** for the mirror build | **200 GB system disk, 93 GB used** after a full run; `~/.oc-mirror` alone 22 GB |
| Image | dnf-based (00a uses `ansible.builtin.package`) | `redhat_9_4_x86_64_20G_alibase_20240711.vhd` |

Three things that table does not say on its own.

**The system disk must be set at creation.** That image ships a 20 GB disk — the
`20G` in its name. dyz7's Helper runs on 200 GB because it was expanded when the
instance was created. Accept the default and the mirror build dies partway
through with no space.

**60 GB is the floor, not the plan.** It is what `mirror-build.yml` needs to
*complete*. A worked-through environment consumed 93 GB. Size for the 200 GB
that is known to be enough.

**The instance type will differ per environment.** `ecs.s7-k-c1m2.2xlarge` is
what dyz7 offers; ste3 and the public cloud share none of its shapes. What
transfers is the *size* — 8 vCPU / 16 GB — so pick whatever the new environment
sells at that size. On a brand-new environment you have to read the console for
this, because `playbooks/tools-list-instance-types.yml` needs a working Helper
and `cloudcli` to run, which is the thing you are trying to create. Use it for
the *cluster* node types later, once this box is up.

Note the Helper is a plain RHEL box, not a cluster node, so the constraint that
binds the masters — `NvmeSupport` must not be `required`, because the RHCOS
agent image is virtio-only — does not apply here. The choice is wider than it is
for `control_plane_type`.

## Network

Three separate requirements; a box that satisfies two of them still blocks.

**1. Apsara OpenAPI + OSS — through the Squid proxy.** The AK/SK calls, the OSS
uploads, and `git clone` all go through it. Get the proxy address with the
environment.

**2. Registry pulls — DIRECT, not through the proxy.** On dyz7 the Squid proxy is
throttled to ~17 KB/s while a direct connection reaches quay.io at ~7 MB/s. The
mirror build therefore pulls direct and uploads through `apsara-oss`, and
`cloud_env` deliberately carries `APSARA_PROXY` but **not** `http_proxy`. If the
Helper on a new environment has no direct egress at all, the mirror build
strategy has to change — establish this early, because ~30 GB at 17 KB/s is not
a plan.

**3. A route to the mirror ECS's private IP.** Phases from 04 on SSH there
directly; a public jump-host EIP is not the model in a private cloud. Two
supported shapes (see DEPLOY.md §2):

- **Option B (default intent)** — the Helper keeps its own VPC and phase 03 peers
  the mirror VPC to it via a Router Interface pair. Set
  `apsara_peer_operator_vpc_id`. On dyz7 the Helper's VPC is `192.168.0.0/16`
  (its vSwitch `192.168.33.0/24`) while the cluster VPC is `10.0.0.0/16` — two
  non-overlapping ranges, which peering requires.
- **Option A (BYO-VPC)** — the mirror ECS is created inside the Helper's existing
  VPC. Set `existing_vpc_id` + `existing_vswitch_id`.

Option B needs the AK/SK to hold `vpc:CreateRouterInterface`,
`vpc:ConnectRouterInterface`, `vpc:CreateRouteEntry` and
`ecs:AuthorizeSecurityGroup`.

**4. Inbound SSH** for whoever operates it.

## Obtain from whoever hands over the environment

- [ ] The ECS itself, sized as above, with a dnf repo it can reach
- [ ] Squid proxy address (`http://<host>:3128`)
- [ ] **One** account's AccessKey pair — see the warning below
- [ ] Organization ID and Resource Group ID (the `x-acs-*` tenancy headers)
- [ ] Region ID
- [ ] An OSS bucket, **pre-created in the account that owns the AccessKey**
- [ ] SSH access

### The one-account rule

**ROS, ECS, VPC, RAM and OSS must all use the same account's AccessKey.** The
`cloud_env` AK/SK builds the ROS stacks and the ECS instances, so the OSS bucket
must belong to that same account. Mixing them returns

```
AccessDenied: The bucket you access does not belong to you
```

with the endpoint, TLS and proxy all working correctly — which sends you looking
in the wrong place.

The bucket must be created in the owning account's console. `apsara-oss` cannot
create it: the gateway does per-bucket TLS vhosts, so a not-yet-existing bucket's
SNI is rejected before anything else happens.

## Bootstrap

One manual step, then a playbook.

```sh
# 1. git + ansible only
PROXY=http://<squid>:3128 ./scripts/bootstrap-operator.sh

# 2. the repo (00a configures git for the proxy; the first clone predates it)
export https_proxy=http://<squid>:3128
git clone <repo-url> /root/alibaba-openshift

# 3. everything else
cd /root/alibaba-openshift/ansible
ansible-playbook -i inventory.yml playbooks/00a-prepare-operator.yml \
  -e operator_proxy=http://<squid>:3128 -e cloud_platform=apsara
```

00a installs `git curl jq tar gzip rsync openssh-clients python3 python3-pip
skopeo podman` plus `golang` on Apsara, generates the SSH keypair, builds the
Apsara Go tools (`apsara-rpc`, `apsara-oss`), and fetches `oc` /
`openshift-install`. It is idempotent.

`mirror.openshift.com` is slow from China and the installer archive is much the
larger of the two. `-e operator_oc_only=true` fetches only `oc`; 06a extracts a
version-matched `openshift-install` from the mirror release later.

## Ready when

```sh
which oc skopeo podman git ansible-playbook
ls scripts/apsara/apsara-rpc/apsara-rpc scripts/apsara/apsara-oss/apsara-oss
df -h /            # expect the mirror build's ~60 GB to be available
curl -sI -x http://<squid>:3128 https://github.com  | head -1   # proxy path
curl -sI --max-time 10 https://quay.io              | head -1   # direct path
```

Then, and only then, `scripts/apsara/probe-endpoints.sh <domain-suffix>` has
somewhere to run, and the environment survey can start.

## Two Apsara quirks worth knowing before you touch anything

**`--DryRun` is not honoured.** The asapi gateway really performs the operation.
Never use it as a safe probe.

**Endpoints cannot be guessed.** They are assigned per deployment and read from
the console; the ones in use across two environments share no shape
(`ros.cloud.ste3.com`, `slb-vpc.cloud.dyz7.com`,
`dns-control.pop.cloud.ste3.com`). `probe-endpoints.sh` finds the ones that
happen to match a naming pattern and will silently miss the rest — see
`docs/apsara/NAS.md` for a worked example of that failure.
