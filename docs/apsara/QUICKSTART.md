# Running mirror-stack on Apsara Stack (private cloud)

Public Aliyun Cloud is the default target; **Apsara support is an overlay** — the
`ros-templates/*.yaml` sources stay public-cloud-first and are transformed for
Apsara by `scripts/apsara/apsara_ize.py`, then driven by the `apsara-rpc` shim
(`scripts/apsara/apsara-rpc/`) instead of the `aliyun` CLI.  No public-cloud
path changes.

Validated 2026-08 on the `ste3` private cloud: a full mirror-stack (VPC /
vSwitch / SG / NAT+EIP+SNAT / RAM / mirror ECS) creates **and** deletes as a
single stack with zero residue.

## Why a shim instead of the `aliyun` CLI

Apsara's OpenAPI gateway differs from public cloud in ways the stock SDK/CLI
does not handle out of the box:

- Requests go through an **operator host inside the Apsara network** and an HTTP
  proxy (`APSARA_PROXY`, e.g. a Squid on `:3128`).
- Every request must carry Apsara org/resource-group headers
  (`x-acs-organizationid`, `x-acs-resourcegroupid`).
- **Per-service scheme differs**: ROS / ECS / VPC speak HTTP; **RAM requires
  HTTPS** (`InvalidProtocol.NeedSsl` otherwise).  HTTPS goes through the proxy
  via CONNECT, so the HTTPS proxy must be set too (`SetHttpsProxy`).
- Internal HTTPS uses a **self-signed / internal CA** — Go rejects it with
  `x509: certificate signed by unknown authority`; use `INSECURE=1`
  (`SetHTTPSInsecure`) or install the internal CA.

## Endpoints and scheme (ste3)

| Service | Product / Version    | Endpoint (env)                    | Scheme |
| ------- | -------------------- | --------------------------------- | ------ |
| ROS     | `ROS` `2019-09-10`   | `ENDPOINT_ROS=ros.cloud.ste3.com` | HTTP   |
| ECS     | `ECS` `2014-05-26`   | `ENDPOINT_ECS=ecs.cloud.ste3.com` | HTTP   |
| VPC     | `Vpc` `2016-04-28`   | `ENDPOINT_VPC=vpc.cloud.ste3.com` | HTTP   |
| RAM     | `Ram` `2015-05-01`   | `ENDPOINT_RAM=ram.cloud.ste3.com` | HTTPS + `INSECURE=1` |

## Environment (on the operator host)

```sh
export AK=<ram-user-access-key> SK=<ram-user-secret>
export REGION=cn-wulan-ste3-d01
export APSARA_PROXY=http://<squid-host>:3128
export ORG_ID=org-...        # x-acs-organizationid
export RG_ID=rs-...          # x-acs-resourcegroupid
export ENDPOINT_ROS=ros.cloud.ste3.com
```

## 1. Transform the template

```sh
python3 scripts/apsara/apsara_ize.py ros-templates/mirror-stack.yaml /tmp/mirror-stack.apsara.yaml
```

`apsara_ize.py` handles, in one pass (no manual `sed`):

- short intrinsic tags -> long form;
- DataSource image lookups -> explicit-id parameters (`MirrorImageId`, `JumpHostImageId`);
- strip properties Apsara rejects (`ALIYUN::PVTZ::Zone` Tags);
- value substitutions (`NatType: Enhanced -> Normal`, disk `cloud_essd -> cloud_pperf`).

## 2. Parameters (Apsara value set)

These are passed at CreateStack (they vary per environment — the values below
are the ste3 POC set; confirm images/instance-types/disk categories against
`DescribeImages` / `DescribeImageSupportInstanceTypes` ∩ `DescribeAvailableResource`
for your zone):

| Parameter                       | ste3 value                                | Source |
| ------------------------------- | ----------------------------------------- | ------ |
| `ClusterName`                   | ≥3 chars (`[a-z0-9][a-z0-9-]{1,20}[a-z0-9]`) | — |
| `Region`                        | `cn-wulan-ste3-d01`                       | — |
| `ZoneId` / `ZoneId2`            | `cn-wulan-ste3-amtest11001-a` (single-AZ POC) | — |
| `MirrorImageId` / `JumpHostImageId` | `aliyun_4_x86_64_20G_alibase_...vhd`  | `DescribeImages` (ImageId == vhd name) |
| `MirrorInstanceType`            | `ecs.g7x-k10-c1m2.2xlarge`                | image ∩ zone intersection |
| `RamRoleName`                   | environment-unique (e.g. `poc-node-role`) | account-scoped name — must differ across concurrent stacks |

Baked by the transformer (not parameters): NAT type `Normal`, disk `cloud_pperf`.

## 3. Create

```sh
./apsara-rpc ROS 2019-09-10 CreateStack --StackName mirror --TimeoutInMinutes 60 \
  --TemplateBody=@/tmp/mirror-stack.apsara.yaml \
  --Parameters.1.ParameterKey ClusterName        --Parameters.1.ParameterValue poc \
  --Parameters.2.ParameterKey Region             --Parameters.2.ParameterValue cn-wulan-ste3-d01 \
  --Parameters.3.ParameterKey ZoneId             --Parameters.3.ParameterValue cn-wulan-ste3-amtest11001-a \
  --Parameters.4.ParameterKey ZoneId2            --Parameters.4.ParameterValue cn-wulan-ste3-amtest11001-a \
  --Parameters.5.ParameterKey MirrorImageId      --Parameters.5.ParameterValue aliyun_4_x86_64_20G_alibase_20260407.vhd \
  --Parameters.6.ParameterKey JumpHostImageId    --Parameters.6.ParameterValue aliyun_4_x86_64_20G_alibase_20260407.vhd \
  --Parameters.7.ParameterKey MirrorInstanceType --Parameters.7.ParameterValue ecs.g7x-k10-c1m2.2xlarge \
  --Parameters.8.ParameterKey RamRoleName        --Parameters.8.ParameterValue poc-node-role
```

Poll: `./apsara-rpc ROS 2019-09-10 GetStack --StackId <id>` until `CREATE_COMPLETE`.

## 4. Delete (single stack, resource-inclusive)

```sh
./apsara-rpc ROS 2019-09-10 DeleteStack --StackId <id>   # no RetainResources
```

Goes to `DELETE_COMPLETE` with **no residue** — the source template's
`NatSNATEntry* DependsOn: [EIPAssociation, PrivateVSwitch*]` makes ROS delete the
SNAT entries before their VSwitches (otherwise `DependencyViolation.Snat`).

### Teardown identity note

Apsara's RAM authorization is **asymmetric across identities** in a way public
cloud is not: a RAM sub-user granted `CreateRole` / `AttachPolicyToRole` is not
automatically granted `DeleteRole` / `DetachPolicyFromRole`.  For a single
create+delete lifecycle with one identity, that identity needs the **matching
delete actions** too:

```
ram:DetachPolicyFromRole   ram:DeleteRole   ram:DeletePolicy   ram:ListPoliciesForRole
```

Without them, `DeleteStack` stalls on the RAM resources — either grant the four
actions to the sub-user, run teardown as an admin identity, or delete with
`--RetainResources <RAM logical ids>` and clean the roles/policies separately.

## Known public-cloud vs Apsara differences

- Enhanced NAT is absent on ste3 — only `Normal` (and Enhanced is what needs the
  `AliyunServiceRoleForNatgw` service-linked role a sub-user can't create).
- `cloud_essd` disk category unavailable on the ste3 zones — use `cloud_pperf`.
- System images are referenced by their `.vhd` **name** as the ImageId.
- Instance type must be in `DescribeImageSupportInstanceTypes` ∩ zone availability.
- `ClusterName` min length 3.
- RAM roles/policies are account-scoped — name uniquely for concurrent stacks.
