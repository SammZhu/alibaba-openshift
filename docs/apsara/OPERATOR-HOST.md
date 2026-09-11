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
| OS | any dnf-based distro | `redhat_9_4_x86_64_20G_alibase_20240711.vhd` |
| ansible-core | see below | 2.14.18 (el9), Python 3.9.25 |

Three things that table does not say on its own.

**The system disk must be set at creation.** That image ships a 20 GB disk — the
`20G` in its name. dyz7's Helper runs on 200 GB because it was expanded when the
instance was created. Accept the default and the mirror build dies partway
through with no space.

**60 GB is the floor, not the plan.** It is what `mirror-build.yml` needs to
*complete*. A worked-through environment consumed 93 GB. Size for the 200 GB
that is known to be enough.

**Any RHEL 9-family distro works — but match the ansible version.**
`bootstrap-operator.sh` detects `dnf`/`yum`/`apt-get`/`zypper` and falls back to
pip when ansible is not packaged, and 00a installs through the generic
`ansible.builtin.package`. CentOS Stream 9, Rocky and Alma carry every package
00a needs (`skopeo`, `podman`, `golang`) under the same names.

The catch is version drift, not packaging. dyz7 runs **ansible-core 2.14.18 on
Python 3.9**, and that is what every playbook here has been exercised against.
CentOS Stream tracks ahead of RHEL 9, so it may hand you 2.17+ — and this repo
still has `until` conditions wrapped in `{{ }}`, which newer ansible-core
treats progressively less kindly. A failure from that looks like a problem with
the new environment and is not one.

If you take Stream, check `ansible-playbook --version` before the first run and
clear those `until` templates first. On a new environment there are already
enough unknowns without adding the automation's own runtime to the list.

(`docs/E2E-RUNBOOK.md` claims ansible-core 2.16+. The working Helper runs
2.14.18, so treat that as a floor nobody has verified rather than a real
requirement.)

**A CentOS Stream image will fail `dnf` on first use, and it looks like "no
internet".** Alibaba's CentOS Stream 9 image ships repos pointing at
`mirrors.cloud.aliyuncs.com` — the *public cloud's* internal mirror. No Apsara
deployment provides that host (dyz7 resolves it to 100.100.2.148 and then times
out on TCP 80, same as everywhere else). The symptom:

```
Errors during downloading metadata for repository 'baseos':
  - Curl error (28): Timeout was reached for
    http://mirrors.cloud.aliyuncs.com/centos-stream/9-stream/...
```

`Connection timed out`, not `Could not resolve host` — DNS answered, the route
did not. Easy to read as a missing EIP and go hunting in the wrong place.

The fix is one word apart:

```sh
sudo sed -i 's|mirrors.cloud.aliyuncs.com|mirrors.aliyun.com|g' /etc/yum.repos.d/*.repo
sudo dnf clean all
```

`mirrors.aliyun.com` is the public mirror and needs ordinary egress;
`mirrors.cloud.aliyuncs.com` is public-cloud-internal and has none here. A
subscribed RHEL image sidesteps this entirely by using `cdn.redhat.com`.

**The instance type will differ per environment.** Three Helpers, three
families, all 8 vCPU / 16 GB:

| dyz7 | ste3 | ste2 |
| --- | --- | --- |
| `ecs.s7-k-c1m2.2xlarge` | `ecs.s7-hg-k-c1m2.2xlarge` | `ecs.g6x-hg-k10-c1m2.2xlarge` |

The public cloud shares none of these shapes either. What
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

### Egress differs per environment — measure it, do not assume

Three environments, three different answers. Measured 2026-09-11:

| | dyz7 | ste3 | ste2 |
| --- | --- | --- | --- |
| `quay.io` | ✓ | ✓ | ✓ |
| `mirror.openshift.com` | ✓ | ✓ | ✓ |
| `goproxy.cn` | ✓ | ✓ | ✓ |
| **`github.com`** | ✓ | **0 of 7 attempts** | **1 of 13** |
| `proxy.golang.org` | — | blocked | blocked |

Everything `00a` needs works everywhere: the `oc` clients come from
`mirror.openshift.com`, and `GOPROXY` already defaults to `goproxy.cn` because
`proxy.golang.org` is Google-hosted and unreachable from China.

**Only `git clone` breaks**, and only on some environments. Sample it several
times before concluding either way — a single probe told us `mirror.openshift.com`
was blocked on ste2 when it answers `302` reliably, and told us GitHub worked
there when it succeeds once in thirteen tries.

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
# 1. git + ansible only   (drop PROXY= entirely where egress is direct)
PROXY=http://<squid>:3128 ./scripts/bootstrap-operator.sh

# 2. the repo -- see the note below: `git clone` does not work everywhere
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

### Getting the repo in where GitHub is unreachable

On ste2 and ste3 `git clone` is not an option (see the table above). The repo,
and the sibling repos the playbooks reach for, have to be delivered from a
machine that can see both GitHub and the Helper.

Note **which** repos: `08`, `08b`, `08c` and `08d` resolve their sources as
`{{ playbook_dir }}/../../../<repo>` — the *parent* of alibaba-openshift. So the
working set is four directories side by side, not one:

```
<parent>/alibaba-openshift
<parent>/openshift-capi-alicloud
<parent>/alibaba-cloud-csi-operator
<parent>/cloud-provider-alibaba-cloud      # CCM, needed by 08d
```

Exclude each repo's `bin/` when copying: those are build tools a Makefile
downloaded for the *copying* machine's architecture. Including them shipped 723
MB of darwin/arm64 binaries where 45 MB of source was wanted.

That leaves one thing unsolved on a GitHub-less Helper: `make build` wants to
download `controller-gen` and friends into `bin/` itself. Copy a populated
`bin/` from a Helper that could, or arrange another source.

### Client downloads

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

## Writing this environment's `all.yml`

Start from a **working** environment's `all.yml`, not from `all.yml.example`.
The example is a generic template; a file that has carried a cluster to green
has months of iteration in its structure and comments — which keys a private
cloud actually needs, and what constrains each value.

Copying it wholesale is the trap. A half-updated `all.yml` is worse than a blank
one: it looks complete while still pointing at the other environment's stacks,
VPC and mirror, so a playbook runs the new environment's credentials against the
old environment's resources.

```sh
scripts/apsara/adapt-all-yml.sh <reference all.yml> [output]
```

Run it on the target Helper. It reads the target's identity from the metadata
service, rewrites what it can determine, blanks what it cannot to
`CHANGEME-<key>`, and — the point of the exercise — **refuses to emit anything
if a single reference-environment value survives**.

Endpoints are derived by taking the shapes the reference environment actually
uses and swapping the environment name, then checking DNS. That beats guessing
naming conventions, because there are none to guess: one Apsara deployment
carries `ros.cloud.X`, `ecs-internal.cloud.X`, `ram-vpc.cloud.X`,
`dns-control.pop.cloud.X` and `oss-<region>-a.cloud.X` side by side. On ste2 all
six of dyz7's shapes resolved on the first try.

What it cannot fill divides in two:

| Needs a human | Needs credentials first |
| --- | --- |
| `AK` / `SK`, `ORG_ID` / `RG_ID` | `zone` / `zone2` / `zone3` (`DescribeZones`) |
| `oss_bucket` (pre-created, same account) | the four instance types |
| `mirror_init_password` | `system_disk_category`, `apsara_image_id` |

So it is two passes: run it, fill the left column, then resolve the right one
with `playbooks/tools-list-instance-types.yml` and `DescribeZones`.

### What it structurally cannot find

The script substitutes values for keys the reference file already has. A
capability the **target** has and the reference does not will not appear, because
nothing tells it the key exists.

That is not hypothetical. dyz7 has no NAS, so its `all.yml` carries no
`ENDPOINT_NAS` line at all — and ste2's candidate came out without one, even
though ste2's NAS is live. Its endpoint is
`nas-pub.<region>.cloud.<env>`, a shape no other service on that deployment
uses (`ecs-pub`, `vpc-pub`, `ros-pub`, `slb-pub` all fail DNS), so neither the
shape-reuse pass nor a naming guess would have reached it.

After running the script, check what the target environment actually offers
against what the reference did, and add the difference by hand.

A second kind of leftover is easier to miss, because it does not look
environment-specific at all: an **absolute path into the reference environment's
layout**. `cloud_cli` and `oss_cli` are full paths to binaries in the repo, and
on a host where the repo lives somewhere else they point at nothing — or worse,
at a stale checkout that happens to exist. They carry no environment name or
region, so the residue scan cannot see them as residue.

The script now rewrites paths containing `.../alibaba-openshift` to the repo it
is running from, and the residue scan flags any that still point elsewhere. The
failure it prevents:

```
fatal: [local]: FAILED! => "[Errno 2] No such file or directory:
  b'/root/alibaba-openshift/scripts/apsara/cloudcli'"
```

Loud here, because the path did not exist. It would have been silent on a host
that still had an old checkout at that path.

## Driving the phases with `sudo`

The playbooks install `oc` and `openshift-install` into `/usr/local/bin` and
then invoke them by name — about 114 `argv: [oc, ...]` call sites across the
phases. That works when ansible runs as root with a normal login PATH.

`sudo ansible-playbook` is not that. RHEL-family sudoers ships

```
Defaults secure_path = /sbin:/bin:/usr/sbin:/usr/bin
```

with no `/usr/local/bin`, so every one of those calls fails to resolve. The
first symptom is 00a reporting `oc MISSING` while `oc` sits exactly where 00a
installed it — a check that is now fixed, but the 114 call sites behind it are
not, and they fail later and less clearly.

Pick one, once:

```sh
# make sudo's PATH match what the phases expect
sudo sed -i 's|^\(Defaults[[:space:]]\+secure_path.*\)$|\1:/usr/local/bin|' /etc/sudoers
sudo visudo -c
```

```sh
# or use a root login shell, which already has the wider PATH
sudo -i
```

This comes up when the repos live outside root's home (see below), because then
reaching for `sudo` is the natural thing to do.

## If the checkout is not owned by the user running ansible

Keeping the repos somewhere other than root's home — a shared path, an operator
account's home — works, but git will not let root operate on a checkout it does
not own:

```
fatal: detected dubious ownership in repository at '<path>'
```

Grant it once per repo, for the account that runs the playbooks:

```sh
sudo git config --global --add safe.directory <parent>/alibaba-openshift
sudo git config --global --add safe.directory <parent>/openshift-capi-alicloud
sudo git config --global --add safe.directory <parent>/alibaba-cloud-csi-operator
```

This is a prerequisite of the layout, not a workaround for one task. `08b` and
`08c` fetch and check out inside the sibling repos, so they hit the same wall —
and later, where the cause is further from the symptom.

The failure it produces first is the least obvious one. `go build` stamps
binaries with VCS metadata, so it runs `git status`; refused, it exits with

```
error obtaining VCS status: exit status 128
    Use -buildvcs=false to disable VCS stamping.
```

after the module downloads have already succeeded — which reads as a Go problem.
00a now passes `-buildvcs=false` for its own two tools, but that does not cover
the git operations in 08b/08c.

## Two Apsara quirks worth knowing before you touch anything

**`--DryRun` is not honoured.** The asapi gateway really performs the operation.
Never use it as a safe probe.

**Endpoints cannot be guessed.** They are assigned per deployment and read from
the console; the ones in use across two environments share no shape
(`ros.cloud.ste3.com`, `slb-vpc.cloud.dyz7.com`,
`dns-control.pop.cloud.ste3.com`). `probe-endpoints.sh` finds the ones that
happen to match a naming pattern and will silently miss the rest — see
`docs/apsara/NAS.md` for a worked example of that failure.
