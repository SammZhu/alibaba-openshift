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

## 1. Get the repo onto the operator (git via proxy)

The operator reaches GitHub only through the proxy:

```sh
cd /root/alibaba-openshift
git config http.proxy "http://<squid-host>:3128"   # same APSARA_PROXY
git config http.version HTTP/1.1                    # avoid HTTP/2 flakiness
git fetch origin && git checkout main && git pull
```

## 2. Build the local tools

`cloudcli` is Python (already executable). `apsara-rpc` and `apsara-oss` are Go —
build them where they live (cloudcli resolves apsara-rpc relative to itself, and
`oss_cli` points at the apsara-oss binary):

```sh
cd /root/alibaba-openshift/scripts/apsara/apsara-rpc && go build -o apsara-rpc .
cd /root/alibaba-openshift/scripts/apsara/apsara-oss && go build -o apsara-oss .
# go.sum is committed; if module download is needed it goes via the proxy:
#   go env -w GOPROXY=... ; or export the proxy for `go`
```

## 3. Config (group_vars/all.yml — single file, gitignored)

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

## 4. OSS specifics (the non-obvious part)

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

### mirror-ECS side (phase 04)

Phase 04 runs the large OSS downloads **on the mirror ECS** over SSH with
`--mode=EcsRamRole` (the instance RAM role). That requires `apsara-oss` present on
the mirror ECS and OSS reachable from inside the VPC — still to be wired into the
mirror-stack cloud-init bootstrap (public cloud installs the `aliyun` CLI there).

## 5. Run

```sh
cd /root/alibaba-openshift/ansible
ansible-playbook playbooks/00-preflight.yml
ansible-playbook playbooks/03-create-mirror-stack.yml     # validated: builds the mirror-stack
# ansible-playbook playbooks/04-prepare-mirror.yml        # needs the mirror-ECS OSS deploy above
# ... 06 / 07 ...
```

Teardown of the persistent (mirror) stack needs RAM delete permissions the
sub-user may lack — see the teardown note in `QUICKSTART.md`.
