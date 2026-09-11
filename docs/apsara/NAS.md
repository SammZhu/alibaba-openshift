# NAS on Apsara — what it takes to turn `nas-rwx` from SKIP into a real test

`13-csi-smoke.yml`'s `[nas-rwx]` check has reported SKIP on every run since the
Apsara work began. That is deliberate, not broken: `nas_available`
(`ansible/inventory.yml`) is false whenever `cloud_env.ENDPOINT_NAS` is empty,
and on dyz7 it is empty. One judgement, three consumers — the CSI CR (deploy
the NAS driver at all), 08 (create the NAS StorageClass), 13 (run the RWX
check).

This document is what the Apsara Stack V3.18.6 *File Storage NAS Developer
Guide* says, with the parts that do not survive contact with a real Apsara
deployment marked as such.

## Read the guide with one caveat

The guide is genuinely the Apsara Stack edition — Apsara Uni-manager console,
the `x-acs-organizationid` / `x-acs-resourcegroupid` tenancy headers, the STS
flow. But its **examples are copied from the public-cloud documentation and
were never substituted**: `nas.cn-hangzhou.aliyuncs.com` appears as the sample
`RegionEndpoint`, `sts.aliyuns.com` as the STS host, and the request syntax is
given as `https://nas.regionid.example.com/?Action=<Action>`.

So:

| Trust | Do not trust |
|---|---|
| API version `2017-06-26` (matches `scripts/apsara/cloudcli`) | any concrete hostname in the guide |
| the four `x-acs-*` tenancy headers | `nas.<region>.aliyuncs.com` |
| "obtain the endpoint from OpenAPI Explorer" | `nas.regionid.example.com` |
| `DescribeRegions` returning a `RegionEndpoint` **field** | the **value** shown for it |

## Stop probing for the endpoint

`scripts/apsara/probe-endpoints.sh` tries five naming patterns
(`%s.%s`, `%s-internal.%s`, `%s-vpc.%s`, `%s-pop.%s`, `%s.pop.%s`) against the
deployment domain. An earlier probe run on dyz7 recorded every NAS candidate
failing DNS (re-run it to confirm before acting on that) — and the guide
explains why the approach cannot work in the first place: each Apsara deployment's endpoints are assigned
at deployment time and read from the console, not derived. The endpoints that
*do* work here have no shared shape:

```
ros.cloud.ste3.com          vpc.cloud.ste3.com
slb-vpc.cloud.dyz7.com      dns-control.pop.cloud.ste3.com
```

`-vpc` suffix, `pop.` infix, two different base domains. Nothing to extrapolate
from.

Two ways to get the real value:

1. **Apsara Uni-manager console** — the guide's own method.
   `Products > Application Services > OpenAPI Explorer`, pick NAS, pick any
   operation, read the **Endpoint** field.
2. **Tianji** — how `ENDPOINT_CLOUDDNS` was found: the service's container
   carries it as `pop_out_endpoint`. Not covered by the developer guide (it is
   an operations-side route), but it worked for CloudDns.

`cloudcli` already routes `nas` as product `Nas` at version `2017-06-26` and its
comment already anticipates the answer: where NAS exists at all it answers on
the shared POP/ASCM gateway, routed by Product, the same shape as CloudDns.
Point `ENDPOINT_NAS` at that gateway.

## Then: is it actually usable?

A reachable endpoint is not the same as a usable service.

```
1. DescribeRegions   confirm reachability; returns the authoritative RegionEndpoint
2. DescribeZones     which file system types each zone offers   <-- the real go/no-go
3. CreateFileSystem  -> FileSystemId
4. CreateMountTarget -> MountTargetDomain
```

**Step 2 is the decision point.** `DescribeZones` reports, per zone, whether
`Performance` and/or `Capacity` file systems are offered. dyz7 presents a single
zone (all three masters sit on three different vSwitches yet report
`cn-wulan-dyz7-amtest11001-a`), so if that zone offers neither type, the service
is present but unsellable — which for our purposes is the same answer as "not
deployed".

### CreateFileSystem

| Parameter | Required | Value |
|---|---|---|
| `ProtocolType` | yes | `NFS` (what RWX needs; `SMB` also exists) |
| `StorageType` | yes | `Performance` or `Capacity` |
| `ZoneId` | no | set it, and to the ECS's zone — cross-zone adds latency |
| `EncryptType` | no | the guide states it is **not supported** |

### CreateMountTarget

| Parameter | Required | Value |
|---|---|---|
| `FileSystemId` | yes | from the previous call |
| `NetworkType` | yes | `Vpc` |
| `AccessGroupName` | yes | `DEFAULT_VPC_GROUP_NAME` |
| `VpcId`, `VSwitchId` | yes in a VPC | the cluster's |
| `SecurityGroupId` | no | |

Returns `MountTargetDomain` — the hostname pods mount. (The sample value is
public-cloud shaped; the real one will not look like it.)

## The part that is easy to miss

The NAS StorageClass this repo creates is a **dynamically provisioning** one —
it needs vpc / vSwitch / zone because the **CSI driver itself** calls
`CreateFileSystem` and `CreateMountTarget`.

So setting `ENDPOINT_NAS` for the ansible layer is **not sufficient**. The
driver pods resolve `nas.<region>.aliyuncs.com` by default, and that name does
not exist here. The endpoint has to be injected into the driver as well.

This is the same shape as the CCM's SLB problem, already solved once:

> `ENDPOINT_SLB` is only needed with `ccm_enabled`: the CCM resolves
> `slb.<region>.aliyuncs.com` otherwise, which does not exist here.

Miss the driver-side half and the symptom is: StorageClass created, PVC stuck
`Pending`, DNS failures in the driver log — configured but not in effect, with
the cause several layers from the symptom.

## If there is no NAS in this deployment

That is a legitimate outcome and should be recorded as one. Today `[nas-rwx]`
reports SKIP, which reads as "not checked yet" and invites the reader to think
it might pass some day. If the console has no NAS product, or `DescribeZones`
offers no file system type, say so in the check's output — "this deployment
does not provide NAS" is information; an indefinite SKIP is not.
