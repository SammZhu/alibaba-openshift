# End-to-End Install Runbook

Operational playbook for taking a brand-new Aliyun account from zero to a
running OpenShift cluster via this repo's Ansible flow.  Distilled from the
P0-3 verification run on 2026-05-31 — 8 real bugs found and fixed during
that run are listed under [Known gotchas](#known-gotchas-2026-05-31) below.

For architecture / design rationale see:
- [README.md](../README.md) — top-level overview, two-flow choice
- [QUICKSTART.md](../QUICKSTART.md) — current split-stack flow quick reference
- [CCM.md](CCM.md), [MIRROR.md](MIRROR.md), [POST-INSTALL.md](POST-INSTALL.md)
  — component deep dives
- Drive doc *"阿里云OpenShift — 预研、设计与开发总结 (v2.3)"* — project-level
  decision record

---

## 1. Audience and scope

You should be reading this if you are about to:

- Run `site.yml` from scratch against a freshly cleaned Aliyun account,
- Re-launch a cluster after `99-teardown.yml`,
- Resume a half-finished install (Stage 1/2/3 partial),
- Debug a stuck install.

Assumed configuration:

- `mirror_enabled: true` (disconnected/restricted-network path — the
  documented and tested production path on China-region accounts)
- `installation_method: Assisted` (this runbook covers the Assisted path via
  `site.yml`).  The **Agent-based Installer (ABI)** is also supported via
  `site-agent.yml` (`installation_method: Agent-based`) — fully air-gapped, no
  assisted-service dependency.

  **ABI network model — ENI-first (MAC↔hostname binding).**  The cluster-stack
  is built in two phases so each node gets a deterministic name (including the
  rendezvous/node-zero, which an earlier DHCP-only + empty-`hosts[]` attempt
  left named after its MAC):
    1. **06 phase-1** creates the SGs/NLB/DNS **plus 3 fixed-IP control-plane
       ENIs** (`ImageId` empty → instances deferred via the template's
       `HasImage` condition) and harvests the ENI MACs into `state.yml`.
    2. **01-agent** builds the agent ISO with `agent-config` `hosts[]` binding
       each MAC → `<cluster>-master-N` (+ `rootDeviceHints: /dev/vda`), then
       imports it to an ECS image.
    3. **06b** `UpdateStack`s the cluster-stack with the real `ImageId` → the 3
       masters materialise, each attaching its pre-created ENI as the **primary
       NIC** (confirmed supported on Alibaba via RunInstances `--DryRun`).
    4. **07-agent** waits for the install and harvests the kubeconfig.
  `rendezvous_ip` must be a master-subnet IP (e.g. `10.0.32.5`, in
  `PrivateSubnetCidr2`).  Masters install **straight to `/dev/vda`** (no `vdb`,
  no clone hook): the minimal agent ISO runs its rootfs from RAM.

  **Air-gap.** The External-platform agent ISO is always *minimal* and fetches
  its ~1 GB rootfs over HTTP; `agent-config bootArtifactsBaseURL` points at an
  in-VPC HTTP server on the mirror ECS (`:8080`), so no public rhcos mirror.
  The ISO is built **on the mirror ECS** (in-VPC): the operator host only
  renders configs + orchestrates over ssh; the mirror ECS runs
  `openshift-install agent create image` (release pulled from its own
  `localhost:8443`), uploads to OSS (internal endpoint) + `ImportImage` via its
  instance RAM role.  So the **mirror** must be up before ABI runs.

  **Live monitor.** `agent wait-for` runs on the mirror ECS under ansible
  (buffered); run `scripts/abi-monitor.sh` in a second terminal for live
  progress.  Requires the cluster-stack SG to allow `:8090` from the VPC CIDR
  (the template adds this for Agent-based).  Live HA validation in progress.
- compact 3-node (`compute_count: 0`) — workers schedule on masters

---

## 2. Operator-box prerequisites

Run on a Linux box with reachable network egress to:

- `api.openshift.com` (Assisted Installer)
- `sso.redhat.com` (offline-token exchange)
- Aliyun OpenAPI endpoints for the target region

Tools (all must `--version` clean):

```
aliyun  (>= 3.3.x)
curl
jq
skopeo               # needed by build-mirror-tarball.sh
ansible-playbook     # ansible-core 2.16+
openshift-install    # matches openshift_version in group_vars/all.yml
oc                   # only used post-install to validate
```

Credentials and keys (paths can be overridden in `group_vars/all.yml`):

| File | Purpose | Source |
|---|---|---|
| `~/work/alibabacloud/pull-secret.txt` | OCP image pulls | <https://console.redhat.com/openshift/install/pull-secret> |
| `~/work/alibabacloud/offline-token` | refresh-token for AI API | <https://console.redhat.com/openshift/token> |
| `~/work/alibabacloud/sshkey/20231118_ed25519{,.pub}` | SSH into ECS | `ssh-keygen -t ed25519` |
| `aliyun configure` profile (`openshift-test` by default) | Aliyun OpenAPI calls | RAM user with full ECS / VPC / SLB / NLB / ROS / RAM / PVTZ / OSS / NAS |

Aliyun account requirements (00-preflight will fail with actionable
messages if missing):

- NLB service-linked role `AliyunServiceRoleForNlb` (created once per
  account; preflight prints the `aliyun resourcemanager
  CreateServiceLinkedRole` command if absent).
- OSS bucket matching `oss_bucket` in `all.yml` (globally unique name).
- ECS instance type stock in `region`/`zone`/`zone2` for control plane,
  mirror, jump host.

`group_vars/all.yml` (gitignored — copy from `all.yml.example`) — critical
fields:

```
cluster_name:        aliocp1
base_domain:         example.local
openshift_version:   "4.20.22"       # pin X.Y.Z, NOT channel
region:              cn-wulanchabu
zone, zone2:         cn-wulanchabu-a, cn-wulanchabu-b
mirror_enabled:      true
mirror_oss_object:   "mirror-tarballs/{{ cluster_name }}-{{ openshift_version }}.tar"
mirror_private_ip:   "10.0.16.4"
aliyun_profile:      openshift-test
oss_bucket:          openshift-iso-samzhu-test
enable_jump_host:    true            # REQUIRED for the *.apps DNS rewrite
```

> **Jump host is mandatory.**  Phase 07 runs `oc` from the jump host to
> discover router-default pod hostIPs (the kubeconfig's `api.<cluster>.<base>`
> only resolves on PrivateZone, and the API NLB is `AddressType=Intranet`
> — operator box cannot reach it).  Without a jump host the DNS rewrite
> task fails fast.

---

## 3. Mirror tarball — one-time build

Skip if `oss://$BUCKET/mirror-tarballs/$cluster_name-$openshift_version.tar`
already exists with companion files (`.imageset-config.yaml`,
`.tag-mapping.tsv`).

```bash
# On a host with good quay.io / registry.redhat.io / registry-cn-hangzhou
# connectivity (typically NOT inside China — run from a US/EU operator box
# or pin OPENSHIFT_VERSION+rebuild on a properly-routed VPS).
OSS_BUCKET=openshift-iso-samzhu-test \
REGION=cn-wulanchabu \
CLUSTER_NAME=aliocp1 \
OPENSHIFT_VERSION=4.20.22 \
PULL_SECRET=~/work/alibabacloud/pull-secret.txt \
  ./scripts/build-mirror-tarball.sh
```

The script (since 2026-05-31 commit `7054bfe`) automatically pins:

- OCP release imageset for the requested version
- 3 × rhai/* Assisted Installer images by digest
- Alibaba CCM `registry-cn-hangzhou.ack.aliyuncs.com/acs/cloud-controller-manager:v2.14.0`
- CAPA `quay.io/samzhu/openshift-capi-alicloud:v0.1.0`

Tarball is ~25 GB.  Upload finishes by writing the OSS triplet (tarball +
imageset-config.yaml + tag-mapping.tsv).

---

## 4. Three-stage run (recommended)

Run each stage in `tmux`/`screen`, validate before advancing.  All
playbooks are individually idempotent.

### 4.0 tmux invocation template — ALWAYS set PYTHONUNBUFFERED

When `ansible-playbook` runs under tmux + `tee` (no TTY), Python's
stdout switches to block-buffering (~4 KB).  In `retries:N delay:60`
polling tasks (Phase A, Phase B, ROS poll, etc.) each retry emits
~80 bytes; buffering means up to 50 retries' worth of output sits
unflushed.  Result: log mtime appears frozen for 30+ minutes while
the playbook is actually progressing normally.  Observed 2026-06-05
P3-COST.2 verification run — appeared "hung" at Phase A for 30 min
when ansible was healthily retrying.

**Always launch tmux+ansible with `env PYTHONUNBUFFERED=1`**:

```bash
tmux new -d -s install \
  "env PYTHONUNBUFFERED=1 \
   ansible-playbook -i inventory.yml playbooks/site.yml \
   2>&1 | tee /tmp/install.log"

# Then tail.  Output now flushes line-by-line.
tail -f /tmp/install.log
```

Alternative: `python3 -u`-invoke ansible explicitly, but PYTHONUNBUFFERED
is the cleanest fix.  Affects only Python's own buffering; tee is
line-buffered to a file by default.

### Stage 1 — preflight + ISO + image (10–30 min)

```bash
cd ansible/
env PYTHONUNBUFFERED=1 ansible-playbook -i inventory.yml \
  playbooks/00-preflight.yml \
  playbooks/01-prepare-iso.yml \
  playbooks/02-import-image.yml \
  2>&1 | tee logs/stage1.log
```

Three `PLAY RECAP` lines, all `failed=0 unreachable=0`.  Verify:

```bash
# state.yml gains: cluster_id, infra_env_id, ecs_image_id, iso_path
grep -E '^(cluster_id|infra_env_id|ecs_image_id|iso_path):' state.yml

# Aliyun custom image is Available
aliyun ecs DescribeImages --RegionId $region --ImageOwnerAlias self \
  | jq '.Images.Image[] | select(.ImageName | startswith("openshift-"))'
```

### Stage 2 — mirror-stack + content (30–60 min)

```bash
env PYTHONUNBUFFERED=1 ansible-playbook -i inventory.yml \
  playbooks/03-create-mirror-stack.yml \
  playbooks/04-prepare-mirror.yml \
  playbooks/05-verify-mirror.yml \
  2>&1 | tee logs/stage2.log
```

Three `PLAY RECAP` lines, all `failed=0`.  Verify:

```bash
# ROS stack is CREATE_COMPLETE
aliyun ros ListStacks --RegionId $region \
  | jq '.Stacks[] | select(.StackName | endswith("-mirror"))'

# Both mirror ECS + jump host Running
aliyun ecs DescribeInstances --RegionId $region \
  | jq '.Instances.Instance[] | {InstanceName, Status, PrivateIp: .VpcAttributes.PrivateIpAddress.IpAddress[0]}'

# state.yml gains: mirror_stack_id, mirror_init_password, mirror_snapshot_*,
# jump_host_ip, mirror_node_ram_role, etc.

# Phase 05 creates the data-disk+system-disk snapshots that 03 reuses
# next time (fast-path).  Persisted in state.yml as mirror_snapshot_{data,system}_id.
```

If 04 hits `unauthorized` while pushing CCM to mirror (HTTP 401), see
[Known gotchas — bug 1](#1-mirror_init_password-cleared-by-teardown-while-snapshots-survived).

### Stage 3 — cluster-stack + install (60–90 min)

```bash
env PYTHONUNBUFFERED=1 ansible-playbook -i inventory.yml \
  playbooks/06-create-cluster-stack.yml \
  playbooks/07-install-cluster.yml \
  2>&1 | tee logs/stage3.log
```

Two `PLAY RECAP` lines.  06 is straightforward (~10 min, builds cluster
SGs, ApiNLB, PrivateZone, 3 master ECS).  07 is the long-poll phase; by
the time it returns:

- AI cluster `status: installed`
- All 34 cluster operators `Available=True Progressing=False Degraded=False`
- `~/openshift-install/<cluster>/auth/{kubeconfig,kubeadmin-password}` on disk
- kubeconfig copied to jump host at `/root/kubeconfig`
- PrivateZone `*.apps` rewritten from the bogon placeholder to a real
  router-default pod hostIP

Validate (run via the jump host since api-int is intranet-only):

```bash
ssh -i $sshkey root@$jumphost \
  'oc --kubeconfig=/root/kubeconfig get nodes -o jsonpath="{range .items[*]}{.metadata.name}{\"\t\"}{.spec.providerID}{\"\n\"}{end}"'
```

Every line should show ProviderID in `alicloud://<region>.<instanceID>`
form — proves the MachineConfig providerid injection + CCM init worked.

```bash
ssh -i $sshkey root@$jumphost \
  'oc --kubeconfig=/root/kubeconfig get co --no-headers' | awk '$3$4$5 != "TrueFalseFalse"'
```

Empty output ⇒ all cluster operators healthy.

### (Stage 4) — Post-install: CAPA / CSI / OADP

Out of scope for this runbook; see [POST-INSTALL.md](POST-INSTALL.md).
Phase 08 runs on the jump host inventory and is intentionally not
included in `site.yml`.

---

## 5. Idempotent re-run

Every phase is safe to re-run.  Specifically, **after a successful install,
re-running `playbooks/07-install-cluster.yml` is a 30-second no-op**
(commit `42d6493`):

- Pre-install block is gated `when: cluster_pre.json.status != 'installed'`
  → skipped entirely.
- Phase A poll exits immediately (cluster is already `installed`).
- Kubeconfig is re-downloaded fresh.
- DNS rewrite re-runs (idempotent UpdateZoneRecord).
- Phase B poll skipped via `when:`.

This is useful when you just want to refresh `kubeconfig`, sync DNS to a
new router pod after node reshuffle, or recover the kubeconfig on a
different operator box.

---

## 6. Known gotchas (2026-05-31)

Eight real failures hit during the P0-3 verification run, all fixed in
main on the same day.  If a future run reproduces any of these, the linked
commits show how the fix is shaped — useful for diagnosis if the fix
regresses.

### 1. `mirror_init_password` cleared by teardown while snapshots survived
- **Symptom**: `04-prepare-mirror.yml` task *"Ensure Alibaba CCM image is
  on mirror (pre-pull + push if missing)"* fails with `podman push ...
  unauthorized: access to the requested resource is not authorized`.
- **Root cause**: previous `99-teardown.yml` unconditionally cleared
  `mirror_init_password` in state.yml while preserving mirror snapshots.
  Next 03 fast-path restores Quay DB from snapshot (old password baked
  in) but cloud-init writes the freshly generated password into
  `/root/.docker/config.json`.  podman uses fresh value, Quay DB rejects.
- **Fix**: commit `4d22cc2` — preserve `mirror_init_password` whenever
  `delete_mirror_snapshots=false`.  Adds an auth sentinel
  (`podman login`) in 04 to fail-fast with an actionable message if a
  drift ever recurs.
- **One-shot recovery**: `ansible-playbook playbooks/99-teardown.yml -e
  teardown_target=mirror -e delete_mirror_snapshots=true -e
  teardown_preserves_ai=false -e keep_image=true -e
  teardown_confirmed=true` then re-run from Stage 1.

### 2. IDMS task fails with censored `no_log: true` error
- **Symptom**: 04 fails at *"Build install_config_overrides JSON
  (imageDigestSources + CA)"* with `{"censored": "the output has been
  hidden due to the fact that 'no_log: true' was specified"}`.
- **Root cause**: a `# CAPA controller image ...` comment was inside the
  YAML `>-` folded scalar holding a Jinja `{{ ... }}` expression.
  Newlines fold to spaces, so `#` ended up inside the Jinja expression
  where it is not a valid token.
- **Fix**: commit `77019c1` — comments moved above the task as YAML
  comments; in-block warning notes the constraint.

### 3. Auth sentinel uses wrong Quay endpoint
- **Symptom**: 04 fails at *"Auth sentinel — verify state.yml password
  actually works against Quay"* with HTTP 401 even when the password is
  correct.
- **Root cause**: probe hit `/api/v1/user/` which is Quay's UI API
  (session cookie / OAuth required) — basic auth returns 401 for valid
  registry credentials.
- **Fix**: commit `dc95d96` — switch probe to `podman login`, the same
  token-auth flow `podman push/pull` use later.

### 4. CCM template `vars:` block self-references
- **Symptom**: 07 fails at *"Render Jinja manifests (01-alibaba-ccm.yaml)"*
  with `recursive loop detected in template string: {{ cluster_name }}`.
- **Root cause**: a `vars:` block on the template task bound
  `cluster_name: "{{ cluster_name }}"` — Ansible recurses resolving the
  binding into itself.
- **Fix**: commit `b1726c3` — drop the `vars:` block; play-scope vars
  from `group_vars/all.yml` are visible to the template module
  automatically.

### 5. CCM render assert false-positive on legitimate cluster name
- **Symptom**: 07 fails at *"Assert rendered CCM has no template
  residue and FQDN matches cluster"* with `FAIL: old hardcode
  'aliocp1.example.local' present`.
- **Root cause**: naive grep flagged the historical demo value
  `aliocp1.example.local`, but for any operator whose `cluster_name`
  happens to be `aliocp1` + `base_domain` `example.local`, the
  legitimate rendered FQDN matches that substring.
- **Fix**: commit `de7eb2d` — replace with positive assertion
  (rendered file must contain `value: api-int.<cluster_name>.<base_domain>`
  exactly).

### 6. `*.apps` DNS chicken-and-egg
- **Symptom**: install reaches AI status `finalizing` and sits there
  indefinitely.  `oc get co console` shows
  `RouteHealthAvailable: failed to GET route (...): context deadline
  exceeded`.
- **Root cause**:
  - `cluster-stack.yaml` seeds `*.apps` PrivateZone record to the
    `IngressVip` placeholder (`10.0.16.6` bogon).
  - On `platform=external`, ingress operator defaults to HostNetwork —
    no LoadBalancer Service is auto-created.  router-default binds
    master:80/443.
  - `console` operator probes `console-openshift-console.apps.<cluster>.<base>`
    — wildcard must point at a master IP, otherwise it times out
    forever.
  - Old `07-install-cluster.yml` ran the DNS-rewrite post-hook only
    AFTER the `installed` poll returned, but `installed` never arrived
    because of console.
  - Even if the post-hook had run, it called `oc` on the operator box.
    kubeconfig server URL is api.<cluster>.<base> (intranet only) and
    ApiNLB is `AddressType=Intranet` → oc silently returned empty
    → `when: _apps_target_ip | length > 0` skipped all PATCH tasks
    silently.
- **Fix**: commit `5f5622f` — split the poll into Phase A (until
  `finalizing or installed`) and Phase B (until `installed`).  Between
  them: download kubeconfig (AI allows during finalizing), scp to jump
  host, discover router pod hostIPs via `ssh root@jumphost oc ...`,
  then PATCH `*.apps`.  Add a hard `fail` on empty discovery result so
  this can never silently regress again.
- **One-shot recovery** (if stuck right now):
  ```bash
  # Find *.apps RecordId
  aliyun pvtz DescribeZoneRecords --ZoneId <forward_zone_id> \
    | jq '.Records.Record[] | select(.Rr == "*.apps")'
  # PATCH to any master IP from state.yml
  aliyun pvtz UpdateZoneRecord --RecordId <id> --Rr '*.apps' \
    --Type A --Value <master_ip1> --Ttl 60
  ```

### 7. Idempotent re-run on installed cluster hangs in "Poll for hosts to discover"
- **Symptom**: re-running 07 against an already-installed cluster hangs
  at *"Poll for {{ expected_hosts }} hosts to discover"* for 30 min then
  fails.
- **Root cause**: poll waits for hosts in `['known', 'known-unbound',
  'insufficient']`, but installed hosts have moved past those states.
- **Fix**: commit `42d6493` — wrap the entire pre-install section
  (poll-for-known + host PATCHes + manifest upload + ready poll +
  mirror-CA inject + trigger-install) in a `when:
  cluster_pre.json.status != 'installed'` block.  Re-run on installed
  cluster now skips straight to Phase A.

### 8. `oc` over ssh argv mangles jsonpath with embedded spaces
- **Symptom**: 07 fails at *"Discover hostIPs of router-default pods
  (run via jump host, inside VPC)"* with `error: name cannot be
  provided when a selector is specified` (oc).
- **Root cause**: ssh joins argv elements after `user@host` with single
  spaces and sends the resulting string verbatim to the remote shell.
  jsonpath `{range .items[*]}{.status.hostIP}{"\n"}{end}` contains
  literal spaces → remote shell re-splits → `{range` goes to `-o` and
  `.items[*]}{.status.hostIP}{"\n"}{end}` becomes a positional arg →
  `oc get pods <name>` collides with `-l`.
- **Fix**: commit `01bd934` — collapse the entire remote command into
  ONE shell-quoted argv element AND switch to the simpler jsonpath
  `{.items[*].status.hostIP}` which has no internal spaces.  Updated
  `until:` and `_apps_target_ip` to use `.split()` since IPs come back
  space-separated.

---

## 7. Recovery patterns

### Cluster install hung in `finalizing`
- Most likely gotcha 6 above (pre-fix).  After commit `5f5622f` this is
  handled automatically; the one-shot manual PATCH listed in the gotcha
  unblocks any stale install.

### Stage 2 mirror push failed (HTTP 401)
- Gotcha 1.  Run the recovery teardown command, then start from Stage 1.

### Ansible polling something forever
- Always check the live AI cluster + operators state via the jump host
  before assuming Ansible is hung — it usually isn't, but on long polls
  (`Wait for install-complete`, `Poll for {{ expected_hosts }} hosts to
  discover`) no output is printed between attempts.
- Process check:
  ```bash
  ps -o pid,wchan:30,cmd -p $(pgrep -f ansible-playbook)
  # hrtimer_nanosleep = sleeping in the delay between retries (healthy)
  # __x64_sys_poll    = blocked on a network I/O (HTTP/SSH)
  ```

### Need a fresh kubeconfig
- Re-run `playbooks/07-install-cluster.yml` (30-second no-op against an
  installed cluster — see [Idempotent re-run](#5-idempotent-re-run)).

### Clean slate (everything)
- `ansible-playbook playbooks/99-teardown.yml -e teardown_target=both -e
  delete_mirror_snapshots=true -e teardown_confirmed=true`.
- See [TEARDOWN.md](TEARDOWN.md) for finer-grained scoping
  (cluster-only, mirror-only, preserve-AI variants).

---

## 8. Version history

| Date | Change |
|---|---|
| 2026-05-31 | Initial — distilled from P0-3 verification run (commits 7054bfe → 01bd934).  Eight bugs found and fixed during the same run; all documented under "Known gotchas". |
