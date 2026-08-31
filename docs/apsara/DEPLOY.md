# Deploying on Apsara Stack — operator host setup + run

This is the hands-on companion to `QUICKSTART.md` (which explains the transform +
parameters). It captures the manual setup done to run the install flow on the
`ste3` private cloud: building the local tools, wiring config, and the OSS/network
specifics that are not obvious.

Everything here is Apsara-only. Public cloud is unaffected (defaults keep every
call on the `aliyun` CLI).

## Topology

| Host | Role | Reaches |
| ---- | ---- | ------- |
| **operator** (Apsara-internal ECS, e.g. the "Helper") | runs ansible + the tools | Apsara OpenAPI + OSS **via the Squid proxy**; GitHub via the proxy |
| **mirror ECS** (in the stack VPC, private subnet) | hosts Quay; runs the OSS downloads over SSH | OSS from inside the VPC (egress via the stack NAT) |
| **jump host** (optional) | oc / in-cluster access | cluster API |

## One-time: account + credentials

**All of ROS, ECS, VPC, RAM, and OSS must use ONE account's AccessKey.** The
`cloud_env` AK/SK builds the ROS stacks and the ECS instances, so the OSS bucket
must belong to (or be accessible by) that **same** account — otherwise OSS returns
`AccessDenied: The bucket you access does not belong to you` even though the
endpoint/TLS/proxy are all fine. Don't mix a bucket created by a different console
account with a different account's AK/SK.

OSS bucket: create it in the **owning account's** console (apsara-oss cannot
create a bucket — the gateway does per-bucket TLS vhosts, so a not-yet-existing
bucket's SNI is rejected). Then set `oss_bucket` to it.

## 1. Prepare the operator host (automated)

On a brand-new box, one manual step installs git + ansible; everything else is a
playbook (`playbooks/00a-prepare-operator.yml`): OS packages (git/curl/jq/tar/
skopeo/podman/golang), the SSH keypair, the Apsara Go tools, and bootstrap
oc/openshift-install.

```sh
# 1. bootstrap: git + ansible only  (PROXY= the Squid proxy, if egress needs one)
PROXY=http://<squid-host>:3128 ./scripts/bootstrap-operator.sh

# 2. get the repo (git is configured for the proxy by the playbook; for the first
#    clone, export it here)
export https_proxy=http://<squid-host>:3128
git clone <repo-url> /root/alibaba-openshift

# 3. prepare everything else
cd /root/alibaba-openshift/ansible
ansible-playbook -i inventory.yml playbooks/00a-prepare-operator.yml \
  -e operator_proxy=http://<squid-host>:3128 -e cloud_platform=apsara
```

Re-runnable; already-satisfied steps no-op.  After `group_vars/all.yml` exists,
the `-e` flags are unnecessary (it reads `cloud_env.APSARA_PROXY` +
`cloud_platform`).  To prepare a REMOTE box, add it to the inventory and pass
`-e operator_hosts=<host>`.

What it does (was manual before):

| Step | Detail |
| ---- | ------ |
| dnf/git proxy | `/etc/dnf/dnf.conf` + `git config --global http.proxy`, `http.version HTTP/1.1` (HTTP/2 is flaky via Squid) |
| packages | git, curl, jq, tar, gzip, rsync, openssh-clients, python3(+pip), skopeo, podman, golang (apsara) |
| SSH keypair | `ssh-keygen -t ed25519 -f {{ ssh_priv_key_file }} -N ""` if absent |
| Go tools | `apsara-rpc` (dynamic) and `apsara-oss` (**CGO_ENABLED=0**, static — phase 04 pushes it to the mirror ECS, a different OS) |
| clients | bootstrap `oc` + `openshift-install` into /usr/local/bin (06a extracts the version-matched pair from the mirror release) |

## 2. Config (group_vars/all.yml — single file, gitignored)

The operator's `all.yml` holds the whole Apsara config; **no `-e` extra-vars file
is needed** (a separate file duplicating keys already in all.yml caused
duplicate-key surprises). Keep one definition per key.

```yaml
# ── dispatch machinery (Apsara) ──
cloud_platform: apsara
cloud_cli: /root/alibaba-openshift/scripts/apsara/cloudcli
oss_cli:   /root/alibaba-openshift/scripts/apsara/apsara-oss/apsara-oss
cloud_env:
  CLOUD_PLATFORM: apsara
  AK: "<AK>"
  SK: "<SK>"
  REGION: "{{ region }}"          # reference the top-level region — don't repeat the value
  APSARA_PROXY: "http://<squid-host>:3128"
  ORG_ID: org-...
  RG_ID:  rs-...
  ENDPOINT_ROS: ros.cloud.ste3.com
  ENDPOINT_ECS: ecs.cloud.ste3.com
  ENDPOINT_VPC: vpc.cloud.ste3.com
  ENDPOINT_RAM: ram.cloud.ste3.com
apsara_image_id:     "aliyun_4_x86_64_20G_alibase_20260407.vhd"   # DescribeImages (ImageId == vhd name)
apsara_oss_endpoint: "https://oss-cn-wulan-ste3-d01-a.cloud.ste3.com"

# Computed OSS endpoints — copy from group_vars/all.yml.example.  These are NOT
# optional: several playbooks (04/06/99-teardown) reference _oss_pub_endpoint /
# _oss_int_endpoint, and a missing one raises a fatal undefined-var error.  On
# apsara both resolve to apsara_oss_endpoint.
_oss_pub_endpoint: "{{ apsara_oss_endpoint if (cloud_platform | default('public')) == 'apsara' else 'oss-' + region + '.aliyuncs.com' }}"
_oss_int_endpoint: "{{ apsara_oss_endpoint if (cloud_platform | default('public')) == 'apsara' else 'oss-' + region + '-internal.aliyuncs.com' }}"

# ── base config (Apsara values) ──
region: cn-wulan-ste3-d01
zone:   cn-wulan-ste3-amtest11001-a
zone2:  cn-wulan-ste3-amtest11001-a
mirror_instance_type: "ecs.g7x-k10-c1m2.2xlarge"    # image ∩ zone intersection
oss_bucket: <bucket owned by the cloud_env account>
mirror_enabled: true
# ... cluster_name, ssh_*_key_file, CIDRs, openshift_version — cloud-agnostic, unchanged
```

Also generate the SSH keypair the playbooks expect if absent:
`ssh-keygen -t ed25519 -f /root/.ssh/openshift_ed25519 -N ""`.

## 3. OSS specifics (the non-obvious part)

Apsara OSS is **not** reachable the way public OSS is. `apsara-oss` handles all of
this (see `scripts/apsara/apsara-oss`), but for the record:

- **Endpoint**: `https://oss-<region>-<zone>.cloud.ste3.com` — note the trailing
  zone segment (e.g. `-a`) and **https**. Confirm the exact domain in the OSS
  console (bucket domain shown as `<bucket>.<endpoint>`).
- **Via the proxy**: a **direct** connection lands on a default vhost and fails the
  TLS handshake with `tls: unrecognized name`. Route through `APSARA_PROXY`
  (apsara-oss reads it and uses `oss.Proxy`).
- **Internal CA**: skip cert verification (apsara-oss uses `oss.InsecureSkipVerify`).
- **Virtual-host addressing** (SDK default) — do NOT force path-style; the gateway
  serves `<bucket>.<endpoint>` and the wildcard cert matches that, not the bare
  endpoint.
- Quick check from the operator:
  ```sh
  AK=.. SK=.. APSARA_PROXY=http://<squid>:3128 \
    scripts/apsara/apsara-oss/apsara-oss ls "oss://<bucket>/" \
      --endpoint=https://oss-<region>-<zone>.cloud.ste3.com
  ```
  A real OSS reply (object list, or `AccessDenied`/`NoSuchBucket`) means the path
  works; `tls: unrecognized name` means it went direct / wrong endpoint.

### Reaching the mirror ECS (phase 04+) — two Apsara options

Phases from 04 on SSH into the mirror ECS at its **private** IP
(`mirror_private_ip`). `_ssh_root_mirror` proxies through `jump_host_ip` — but in
a private cloud we do **not** use a public jump-host EIP (unreliable / not the
model). The operator host must reach the mirror ECS's private IP directly. Pick
one (public cloud uses neither; both default off):

| | Option B — new VPC + auto peering | Option A — BYO-VPC |
| --- | --- | --- |
| mirror VPC | its own (identical to public cloud) | the operator's existing VPC |
| operator↔mirror | Router Interface peering (auto) | same VPC, direct |
| config | `apsara_peer_operator_vpc_id` | `existing_vpc_id` + `existing_vswitch_id` |
| teardown | 99-teardown unwinds the peering | delete the stack |
| isolation | mirror self-contained, doesn't touch operator VPC | mirror ECS/SG live in the operator VPC |

#### Option B — new VPC + automatic peering (default intent)

mirror-stack builds its own VPC (public-cloud-identical). Phase 03 then peers it
to the operator VPC — creates the Router Interface pair, connects it, adds the
routes both ways, and opens the mirror SG to the operator CIDR — via
`tasks/apsara_peer_vpc.yml`. 99-teardown reverses it (`tasks/apsara_unpeer_vpc.yml`)
before DeleteStack, so the RI never blocks VPC deletion.

```yaml
# leave existing_vpc_id / existing_vswitch_id EMPTY (that selects the new-VPC path)
apsara_peer_operator_vpc_id: vpc-xxxxxxxx    # the operator (Helper) VPC
# apsara_peer_operator_cidr: ""              # optional; derived from the VPC if empty
# apsara_peer_ri_spec: "Large.1"            # RI spec (Large.1 verified on ste3)
jump_host_ip: ""                             # no jump host; operator routes via the peering
```

Requires the AK/SK to have `vpc:CreateRouterInterface`/`ConnectRouterInterface`/
`CreateRouteEntry`/`ecs:AuthorizeSecurityGroup` (verified present on ste3
2026-08-09). **Apsara quirk:** the asapi gateway does **not** honour `--DryRun` —
it really performs the operation, so never use DryRun as a safe probe.

#### Option A — BYO-VPC (mirror in the operator's existing VPC)

Skip a new VPC entirely and **place the mirror stack in the operator's EXISTING
VPC** — same VPC, direct private connectivity, no peering. mirror-stack supports
this via two parameters (both default `""` → create a new VPC, so public cloud is
unchanged):

| Parameter | Meaning |
| --------- | ------- |
| `ExistingVpcId`     | when set, skip creating the VPC/subnets/NAT/EIP/SNAT |
| `ExistingVSwitchId` | the existing VSwitch (in that VPC) to place the mirror ECS in |

When `ExistingVpcId` is set the `CreateNetwork` condition turns off every network
resource; the mirror ECS's SG binds to `ExistingVpcId` and the ECS launches in
`ExistingVSwitchId`, reusing that VPC's existing NAT egress. The stack outputs
(`VpcId` / `PrivateVSwitchId` / ...) resolve to the existing ids so cluster-stack
still wires up correctly.

Config on the operator (`group_vars/all.yml`) — the operator is IN this VPC/subnet:

```yaml
existing_vpc_id:      vpc-xxxxxxxx           # the operator/Helper's VPC
existing_vswitch_id:  vsw-xxxxxxxx           # a VSwitch in it (mirror ECS lands here)
mirror_private_ip:    192.168.34.4           # a free IP in that VSwitch's CIDR
vpc_cidr:             192.168.0.0/16         # the existing VPC's CIDR — the mirror SG
                                             # allows 8443/8080/22 from VpcCidr, so this
                                             # must cover the operator's subnet for SSH
jump_host_ip:         ""                     # no jump host; operator has a direct route
```

Verify `ssh -i <key> root@<mirror_private_ip>` from the operator before running 04.
With a direct route, `_ssh_proxy_args` should be blank (no ProxyCommand needed).

> Public cloud: leave `existing_vpc_id`/`existing_vswitch_id` unset — mirror-stack
> creates its own VPC exactly as before.

### mirror-ECS side OSS (phase 04)

Phase 04 runs the large OSS downloads **on the mirror ECS** over SSH with
`--mode=EcsRamRole` (the instance RAM role). That requires:

1. `apsara-oss` present **on the mirror ECS**. Public cloud installs the `aliyun`
   CLI via cloud-init; for Apsara, **04 pushes the operator's `apsara-oss` to the
   mirror automatically** (task "(apsara) ensure apsara-oss is present on the
   mirror ECS", scp to the `oss_cli` path). Build it `CGO_ENABLED=0` (above) so the
   one binary runs on the mirror's OS too.
2. The mirror-ECS RAM role (`<cluster_name>-mirror-role`) attached with OSS read
   perms + the ECS metadata service reachable (STS). Verify:
   `curl -s http://100.100.100.200/latest/meta-data/ram/security-credentials/<cluster>-mirror-role`.
3. OSS reachable **from the mirror ECS**. The remote calls pass
   `--endpoint={{ _oss_int_endpoint }}` but do NOT inherit the operator's
   `APSARA_PROXY` (the operator's `cloud_env` isn't exported into the SSH session).
   If the mirror reaches OSS VPC-internally, nothing more is needed; if it needs
   the proxy (same `tls: unrecognized name` symptom as the operator did), the
   remote heredocs must export `APSARA_PROXY` — surfaces on 04's first OSS call.

## 3b. Build + ship the mirror content to OSS (prerequisite for 04)

04 downloads the OpenShift release + AI images (~25-30 GB) and the mirror-registry
installer from OSS. Those must be built + uploaded first — the bucket starts empty.

On the **operator/Helper** the Squid proxy is heavily throttled (~17 KB/s), but a
**direct** connection reaches quay.io at ~7 MB/s — usable. So build on the Helper
with registry pulls going **direct** and the OSS upload going through `apsara-oss`.

Run it via ansible (assembles the env from `group_vars`, dispatches OSS per
platform) — no hand-crafted env line:

```sh
cd /root/alibaba-openshift/ansible
ansible-playbook playbooks/mirror-build.yml
# ~1-2 h; follow progress with the log path it prints:  tail -f ../mirror-build-*.log
```

It reads `oss_bucket` / `openshift_version` / `cluster_name` / `region` /
`pull_secret_file` / `offline_token_file` from `all.yml`, points `OSS_CLI` at
`apsara-oss` and `OSS_ENDPOINT` at `apsara_oss_endpoint`, and passes the AK/SK from
`cloud_env`. `cloud_env` carries `APSARA_PROXY` for the OSS upload but not
`http_proxy`, so oc-mirror/skopeo pull direct. (Equivalent manual form:
`OSS_CLI=… OSS_ENDPOINT=… APSARA_PROXY=… AK=… SK=… OSS_BUCKET=… REGION=… CLUSTER_NAME=… OPENSHIFT_VERSION=… OFFLINE_TOKEN_FILE=… PULL_SECRET=… ./scripts/build-mirror-tarball.sh`,
with `http_proxy`/`https_proxy` unset.)

Needs on the Helper: Red Hat pull-secret + offline token (paths in `all.yml`),
`skopeo` (the script installs oc-mirror from mirror.openshift.com — slow via the
CDN but ~180 MB one-time), and ~60 GB free disk. oc-mirror v2 keeps `~/.oc-mirror`
as a blob cache, so a dropped run resumes cheaply on re-run.

The mirror-registry installer + oc-mirror binary are staged into OSS by
`02-import-image` (task `mirror_stage_artefacts`); run 02 before 04.

## 4. Run

```sh
cd /root/alibaba-openshift/ansible
ansible-playbook playbooks/00-preflight.yml
ansible-playbook playbooks/03-create-mirror-stack.yml     # validated: builds mirror-stack + auto-peers to the operator VPC
# ansible-playbook playbooks/04-prepare-mirror.yml        # after 3b (OSS content) + 02 (mirror-registry staged)
# ... 06 / 07 ...
```

Teardown of the persistent (mirror) stack needs RAM delete permissions the
sub-user may lack — see the teardown note in `QUICKSTART.md`.

## 5. Known limitation: no cloud-controller-manager

**The Alibaba CCM is not deployed on Apsara** (`ccm_enabled` defaults to false
there). This is deliberate — deploying it deadlocks the install — but it costs
you some cloud integration, so it is worth understanding.

### Why it cannot work as-is

Two problems, one of which is fundamental:

1. **Endpoints are built in the public-cloud shape.** The CCM composes
   `ecs-vpc.<region>.aliyuncs.com`; Apsara serves `ecs-internal.cloud.<env>.com`.
   Different structure, not a string swap. This one is *probably* solvable —
   some CCM versions accept a custom endpoint in cloud-config (not investigated).

2. **Apsara requires `x-acs-organizationid` / `x-acs-resourcegroupid` on every
   request.** The CCM uses the stock Alibaba Go SDK, which never sends them.
   This is exactly why `scripts/apsara/apsara-rpc` exists — it adds those headers
   (plus `x-acs-regionid`, `x-acs-caller-sdk-source`) to each call. Without them
   the gateway either rejects the request or returns an empty result set (a
   `DescribeImages` returning `TotalCount:0` looks like "no images exist" rather
   than "wrong scope" — that cost us an afternoon once).

So even with the endpoint fixed, the CCM would get no data back. Making it work
means patching the CCM's SDK usage — maintaining a fork, not setting a config
value.

### Why leaving it out is safe here

Nothing this deployment does depends on it:

| CCM provides | Why it isn't needed |
| ------------ | ------------------- |
| `LoadBalancer` Services (SLB/NLB) | api / api-int / `*.apps` resolve through CloudDns straight to the node IP |
| ProviderID on nodes | SNO, no Machine API managing it |
| zone/region labels | single availability zone |
| Node lifecycle (ECS deleted → node removed) | single node, managed by hand |

### Why leaving it IN is actively harmful

`install-config` declaring `cloudControllerManager: External` makes kubelet run
with `--cloud-provider=external`, which taints every node
`node.cloudprovider.kubernetes.io/uninitialized:NoSchedule` until a CCM clears
it. The CCM never can, so the taint never lifts: nothing schedules, OVN never
starts, the node stays `NotReady`, and the install stalls at ~50% with no error
pointing anywhere near the CCM. Two install attempts died this way before we
traced it.

### When this becomes a problem again

- You need `type: LoadBalancer` Services → wire up SLB yourself, or use
  NodePort / MetalLB
- You add CAPA-managed workers → node lifecycle and ProviderID start to matter
- CSI turns out to need topology labels → verify; the CSI driver here uses
  explicit AK/SK rather than the cloud provider, so it likely does not

The real fix, if it is ever needed, is to give the CCM the same treatment
`apsara-rpc` gives our own calls: custom endpoints plus the org/resource-group
headers. That is a project of its own and should not block installing clusters.

