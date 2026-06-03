#!/bin/bash
# Aliyun billing report — pull a month's bill, summarise by product code,
# diff vs previous month, print Markdown to stdout.
#
# Usage:
#   scripts/aliyun-billing-report.sh                    # report last full month
#   scripts/aliyun-billing-report.sh 2026-05            # report a specific month
#   scripts/aliyun-billing-report.sh --detail           # last month + drill into top 3 products
#   scripts/aliyun-billing-report.sh --detail 2026-05   # specific month with detail
#
# Append `> docs/billing/YYYY-MM.md` to archive in repo.
#
# Requirements:
#   - aliyun CLI configured.  Set ALIYUN_PROFILE env to override default
#     (script picks the same profile group_vars/all.yml uses; default
#     'openshift-test' matches the existing project convention).
#   - jq
#
# Notes on endpoints:
#   bssopenapi is a centralised service (not region-scoped).  We pin
#   --endpoint business.aliyuncs.com explicitly because RAM profiles
#   configured with cn-wulanchabu (or other regions without a BSS
#   endpoint) error out with InvalidRegionId otherwise.
#
# Cost: bssopenapi QueryBillOverview is FREE to call.
#
# Notes:
#   - QueryBillOverview returns per-product subtotals for an entire billing
#     cycle.  For per-instance detail, use QueryBill or DescribeInstanceBill
#     (used in --detail mode).
#   - PretaxAmount is the displayed amount (before adjustments).  We use
#     PretaxGrossAmount instead — closer to what the bill page shows.

set -euo pipefail

ALIYUN_PROFILE="${ALIYUN_PROFILE:-openshift-test}"

# ── Pick reporting month ──────────────────────────────────────────────────
if [[ "${1:-}" == "--detail" ]]; then
  DETAIL=1; shift
else
  DETAIL=0
fi

if [[ -n "${1:-}" ]]; then
  if [[ ! "$1" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
    echo "ERROR: month must be YYYY-MM, got: $1" >&2; exit 1
  fi
  MONTH="$1"
else
  # Default: previous month.  Works on GNU date and BSD/macOS date.
  if date -d 'last month' '+%Y-%m' >/dev/null 2>&1; then
    MONTH="$(date -d 'last month' '+%Y-%m')"
  else
    MONTH="$(date -v-1m '+%Y-%m')"
  fi
fi

# Previous month for the diff column
if date -d "$MONTH-01 -1 month" '+%Y-%m' >/dev/null 2>&1; then
  PREV_MONTH="$(date -d "$MONTH-01 -1 month" '+%Y-%m')"
else
  PREV_MONTH="$(date -v-1m -j -f '%Y-%m-%d' "$MONTH-01" '+%Y-%m' 2>/dev/null || echo '')"
fi

# ── Fetch ─────────────────────────────────────────────────────────────────
fetch_overview() {
  local month="$1"
  aliyun --profile "$ALIYUN_PROFILE" bssopenapi QueryBillOverview \
    --endpoint business.aliyuncs.com \
    --BillingCycle "$month" 2>/dev/null || echo '{}'
}

cur_raw="$(fetch_overview "$MONTH")"
prev_raw="$(if [[ -n "$PREV_MONTH" ]]; then fetch_overview "$PREV_MONTH"; else echo '{}'; fi)"

# ── Validate ──────────────────────────────────────────────────────────────
cur_items="$(echo "$cur_raw" | jq '.Data.Items.Item // []' 2>/dev/null || echo '[]')"
if [[ "$(echo "$cur_items" | jq 'length')" == "0" ]]; then
  echo "ERROR: no bill items returned for $MONTH (or QueryBillOverview failed)." >&2
  echo "Raw response:" >&2; echo "$cur_raw" | head -20 >&2
  exit 1
fi

# ── Render Markdown ───────────────────────────────────────────────────────
total_cur="$(echo "$cur_items" | jq '[.[].PretaxGrossAmount] | add | . * 100 | round / 100')"
total_prev="$(echo "$prev_raw" | jq '([.Data.Items.Item[]?.PretaxGrossAmount] | add // 0) | . * 100 | round / 100')"

# Helper: lookup previous-month amount for a given ProductCode
prev_amount_for() {
  local pcode="$1"
  echo "$prev_raw" | jq -r --arg pc "$pcode" \
    '[.Data.Items.Item[]? | select(.ProductCode == $pc) | .PretaxGrossAmount] | add // 0'
}

# Helper: percentage delta string (e.g. "+15%", "-30%", "(new)")
delta_str() {
  local cur="$1" prev="$2"
  if [[ "$prev" == "0" ]] || [[ -z "$prev" ]]; then
    echo "(new)"
  else
    awk -v c="$cur" -v p="$prev" \
      'BEGIN{ d = (c-p)/p*100; printf("%+.0f%%", d) }'
  fi
}

cat <<EOF
# 阿里云账单月报 — $MONTH

> Total this month: **¥${total_cur}**  (vs $PREV_MONTH: ¥${total_prev}, $(delta_str "$total_cur" "$total_prev"))
> Profile: \`$ALIYUN_PROFILE\`  •  Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

| 产品 (ProductCode) | 本月 ¥ | 上月 ¥ | 变化 |
|:-|--:|--:|:-:|
EOF

# Group by ProductCode + ProductName, sum PretaxGrossAmount, round to 2 decimals.
# Some products (e.g. SLB classic + ALB) come back as separate items under the
# same ProductCode — collapse them so the table doesn't show duplicate rows.
echo "$cur_items" \
  | jq -r 'group_by(.ProductCode + "|" + .ProductName)
           | map({pc: .[0].ProductCode, pn: .[0].ProductName,
                  amt: ([.[].PretaxGrossAmount] | add | . * 100 | round / 100)})
           | sort_by(-.amt)
           | .[] | [.pc, .pn, .amt] | @tsv' \
  | while IFS=$'\t' read -r pcode pname amount; do
      prev="$(prev_amount_for "$pcode")"
      prev_rounded="$(printf '%.2f' "$prev")"
      delta="$(delta_str "$amount" "$prev")"
      printf '| %s (%s) | %s | %s | %s |\n' "$pname" "$pcode" "$amount" "$prev_rounded" "$delta"
    done

# ── Detail mode: drill into top 3 products ───────────────────────────────
if [[ "$DETAIL" == "1" ]]; then
  cat <<EOF

---

## 明细 — 当月前 3 大产品

EOF
  top3="$(echo "$cur_items" | jq -r 'sort_by(-.PretaxGrossAmount) | .[0:3] | .[].ProductCode')"
  for pc in $top3; do
    pname="$(echo "$cur_items" | jq -r --arg pc "$pc" '.[] | select(.ProductCode == $pc) | .ProductName')"
    echo "### $pname (\`$pc\`)"
    echo
    echo '| InstanceID | BillingItem | Amount ¥ |'
    echo '|:-|:-|--:|'
    # QueryBill returns per-instance detail; can be slow + paginated.
    # MaxResults=20 keeps this snappy.  For full detail use DescribeInstanceBill.
    aliyun --profile "$ALIYUN_PROFILE" bssopenapi QueryBill \
      --endpoint business.aliyuncs.com \
      --BillingCycle "$MONTH" --ProductCode "$pc" --PageSize 20 2>/dev/null \
      | jq -r '.Data.Items.Item[]? | [.InstanceID // "-", .BillingItem // "-", .PretaxGrossAmount] | @tsv' \
      | sort -t$'\t' -k3 -rn \
      | while IFS=$'\t' read -r iid bitem amt; do
          printf '| `%s` | %s | %s |\n' "$iid" "$bitem" "$amt"
        done
    echo
  done
fi

cat <<EOF

---

## 与预算对照

- 月度预算 (\`docs/COST.md\` §1)：~¥400 目标 / ¥500 上限
- 本月：¥${total_cur} → $(awk -v c="$total_cur" -v t=400 -v l=500 \
    'BEGIN{ if (c > l) print "🚨 超上限"; else if (c > t) print "⚠️  超目标"; else print "✅ 在预算内" }')

## 看了之后做什么

1. 如果某产品 vs 上月涨 >30%，去 aliyun 控制台 → 费用中心 → 用量分析
   按 instance 维度看是哪台 ECS / 哪个 NAT 在涨
2. ECS 涨 → 看是哪台 instance 跑得久 (StopInstance 替代 teardown 可省 70%)
3. OSS 涨 → 看是不是又有人在 on-prem 直接走 public endpoint (P3-COST.1
   修过，确认没回退)
4. NAT CU Fee 涨 → 看 mirror import / cluster image pull 频率，跨境流量
   能不能改用 aliyun 内的 ACR 镜像替代 (ACK images already on
   registry-cn-hangzhou.ack.aliyuncs.com — see docs/MIRROR.md)
EOF
