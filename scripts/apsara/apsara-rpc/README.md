# apsara-rpc

A tiny Go shim that calls Apsara Stack RPC-style OpenAPIs, standing in for
`aliyun <product> <action>` when the target is a private cloud.  The stock
`aliyun` CLI / SDK does not handle Apsara's proxied, header-stamped, per-service
gateway out of the box; this does exactly what mirror-stack / cluster-stack need
and nothing more.

## Build

```sh
cd scripts/apsara/apsara-rpc
go build -o apsara-rpc .
```

## Usage

```
apsara-rpc <Product> <Version> <Action> [--Key val | --Key=val | --Key=@file] ...
```

- `--Key=@file` reads the value from a file (used for `--TemplateBody=@stack.yaml`).
- Large values and `TemplateBody` / `PolicyDocument` are sent in the request body;
  everything else goes in the query string.

Example:

```sh
export AK=... SK=... REGION=cn-wulan-ste3-d01 APSARA_PROXY=http://squid:3128
export ORG_ID=org-... RG_ID=rs-... ENDPOINT_ROS=ros.cloud.ste3.com
./apsara-rpc ROS 2019-09-10 GetStack --StackId <id>
```

## What it handles (and why)

| Concern | How |
| ------- | --- |
| Requests must egress via an in-network proxy | `APSARA_PROXY` -> `SetHttpProxy` **and** `SetHttpsProxy` (HTTPS tunnels via CONNECT) |
| Apsara org / resource-group headers | `ORG_ID` / `RG_ID` -> `x-acs-organizationid` / `x-acs-resourcegroupid` (plus `x-acs-regionid`, `x-acs-caller-sdk-source`) |
| Per-service scheme differs (ROS/ECS/VPC = HTTP, RAM = HTTPS) | `SCHEME=https` selects HTTPS; default HTTP |
| Internal HTTPS uses a self-signed / internal CA | `INSECURE=1` -> `SetHTTPSInsecure(true)` (or install the CA) |
| Endpoint per service | `ENDPOINT` or `ENDPOINT_<PRODUCT>` (e.g. `ENDPOINT_ROS=ros.cloud.ste3.com`) |
| STS instead of long-lived AK | set `STS_TOKEN` alongside `AK` / `SK` |

See `../../../docs/apsara/QUICKSTART.md` for the endpoint/scheme table and the
end-to-end mirror-stack create/delete flow.
