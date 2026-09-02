#!/usr/bin/env bash
# probe-endpoints.sh — discover a new Apsara Stack's OpenAPI endpoints.
#
# Every Apsara deployment uses its own domain and its own per-product endpoint
# naming, and getting those wrong looks exactly like a broken service (503s,
# InvalidAction.NotFound, TLS errors).  This probes the usual patterns so you
# stop guessing.  Two phases:
#
#   phase 1 (no credentials): DNS + TCP reachability for <svc>[-suffix].<domain>
#   phase 2 (needs AK/SK):    make a real read-only call and classify the answer
#
#   ./probe-endpoints.sh cloud.example.com                 # phase 1 only
#   AK=... SK=... REGION=cn-x-d01 ORG_ID=org-.. RG_ID=rs-.. \
#     ./probe-endpoints.sh cloud.example.com --call        # phase 1 + 2
#
# Reading phase 2:
#   asapiSuccess:true / a normal result -> endpoint + product + version are right
#   InvalidAction.NotFound              -> endpoint reachable, wrong action name
#   InvalidVersion                      -> endpoint reachable, wrong API version
#   SignatureDoesNotMatch / NeedSsl     -> endpoint right, credentials/scheme issue
#   503 / connect error / no such host  -> wrong endpoint
set -uo pipefail

DOMAIN="${1:?usage: probe-endpoints.sh <domain-suffix, e.g. cloud.ste3.com> [--call]}"
DO_CALL="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RPC="$HERE/apsara-rpc/apsara-rpc"

# product -> "<read-only action> <version>"  (the pairs verified on ste3)
declare -A API=(
  [ecs]="DescribeRegions 2014-05-26"
  [vpc]="DescribeVpcs 2016-04-28"
  [ros]="DescribeRegions 2019-09-10"
  [ram]="ListRoles 2015-05-01"
)
# Product name casing apsara-rpc/the gateway expect.
declare -A PROD=([ecs]=Ecs [vpc]=Vpc [ros]=ROS [ram]=Ram)
# Endpoint name patterns seen in the wild.
# NOTE: POP services live under a separate ".pop." label (dns-control.pop.<domain>),
# not a hyphen — that cost us hours on ste3.  OSS carries region (+zone) in the name.
PATTERNS=("%s.%s" "%s-internal.%s" "%s-vpc.%s" "%s-pop.%s" "%s.pop.%s")

probe_tcp() {  # host -> prints "DNS=<ip> 80:OK 443:OK"
  local h="$1" ip
  ip=$(getent hosts "$h" 2>/dev/null | awk '{print $1}' | head -1)
  [ -z "$ip" ] && { echo "DNS-FAIL"; return 1; }
  printf 'DNS=%-15s ' "$ip"
  (timeout 5 bash -c "</dev/tcp/$h/80"  2>/dev/null && printf '80:OK  ') || printf '80:--  '
  (timeout 5 bash -c "</dev/tcp/$h/443" 2>/dev/null && printf '443:OK') || printf '443:--'
  echo
}

echo "=== phase 1: DNS + TCP  (domain: $DOMAIN) ==="
# nas/slb matter for the CSI driver and for LoadBalancer Services; a product the
# environment simply does not deploy shows up here as every pattern failing DNS,
# which is a real answer rather than a gap in this list.
for svc in ecs vpc ros ram oss dns dns-control nas slb; do
  for pat in "${PATTERNS[@]}"; do
    # shellcheck disable=SC2059
    host=$(printf "$pat" "$svc" "$DOMAIN")
    printf '  %-45s ' "$host"
    probe_tcp "$host"
  done
done

# OSS is named oss-<region>[-<zone-letter>].<domain> — probe those when REGION is known.
if [ -n "${REGION:-}" ]; then
  echo "  -- OSS (region-qualified) --"
  for h in "oss-$REGION.$DOMAIN" "oss-$REGION-a.$DOMAIN" "oss-$REGION-b.$DOMAIN"; do
    printf '  %-45s ' "$h"; probe_tcp "$h"
  done
fi

[ "$DO_CALL" = "--call" ] || { echo; echo "(add --call plus AK/SK/REGION[/ORG_ID/RG_ID] for phase 2)"; exit 0; }
[ -x "$RPC" ] || { echo "!! $RPC not built — run 00a-prepare-operator.yml first"; exit 1; }
: "${AK:?AK required for --call}"; : "${SK:?SK required}"; : "${REGION:?REGION required}"

echo
echo "=== phase 2: real API calls ==="
for svc in "${!API[@]}"; do
  read -r action version <<<"${API[$svc]}"
  for pat in "${PATTERNS[@]}"; do
    # shellcheck disable=SC2059
    host=$(printf "$pat" "$svc" "$DOMAIN")
    getent hosts "$host" >/dev/null 2>&1 || continue     # skip non-resolving
    for scheme in http https; do
      printf '  %-42s %-5s %-8s ' "$host" "$scheme" "$svc"
      out=$(SCHEME=$scheme INSECURE=1 ENDPOINT="$host" AK="$AK" SK="$SK" REGION="$REGION" \
            ORG_ID="${ORG_ID:-}" RG_ID="${RG_ID:-}" \
            "$RPC" "${PROD[$svc]}" "$version" "$action" --RegionId "$REGION" 2>&1 | tr -d '\n')
      case "$out" in
        *asapiSuccess\":true*|*'"Regions"'*|*'"Vpcs"'*|*'"Roles"'*) echo "✓ WORKS" ;;
        *InvalidAction.NotFound*)   echo "~ endpoint OK, action not found" ;;
        *InvalidVersion*)           echo "~ endpoint OK, wrong version" ;;
        *NeedSsl*)                  echo "~ endpoint OK, needs https/SecureTransport" ;;
        *SignatureDoesNotMatch*|*InvalidAccessKeyId*) echo "~ endpoint OK, credential issue" ;;
        *Forbidden*|*NoPermission*) echo "~ endpoint OK, no permission" ;;
        *"Service Unavailable"*)    echo "✗ 503 (gateway has no route)" ;;
        *"no such host"*)           echo "✗ DNS" ;;
        *timeout*|*"connection refused"*) echo "✗ unreachable" ;;
        *) echo "? $(echo "$out" | cut -c1-70)" ;;
      esac
    done
  done
done
