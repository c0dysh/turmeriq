# TurmerIQ sync — reconciliation report

Generated against `/admin/_truth`. Overall: **ALL CHECKS PASS**

## What was fetched and written

| item | value |
|---|---|
| bulk orders loaded (authoritative) | 9040 |
| bulk line items loaded | 12620 |
| orders reachable by paginated listing | 9004 |
| customers loaded | 9300 |
| events received / distinct | 1644 / 1439 |
| HTTP requests made | 108 |

## Checks against ground truth

| check | got | truth | verdict |
|---|---|---|---|
| orders_total_all_time | 9040 | 9040 | ✅ OK |
| orders_reachable_by_listing | 9004 | 9004 | ✅ OK |
| orders_cancelled | 927 | 927 | ✅ OK |
| customer_records | 9300 | 9300 | ✅ OK |
| exchange_pairs | 240 | 240 | ✅ OK |
| hidden = total - listable | 36 | 36 | ✅ OK |

## Derived figures (the two /admin/_truth left for us)

- **Distinct real people who have bought: 3094** (distinct customer_id on non-cancelled orders). Including cancelled-only buyers it is 3179.
- **2023-onward net revenue, exchanges counted once: ₹20,529,284.00**.
  - For contrast, the naive number (trust `created_at`, count only `paid`) is ₹21,084,985.00 — *higher*, because all 2,600 migrated orders carry the 2023 import stamp and get counted as 2023 revenue.
- Repeat-purchase rate (all-time, live): 75.3% of buyers have >1 order.

## What was rejected / could not be resolved

- **36 orders are hidden from the paginated list** (DEFECT 6). Recovered via the complete bulk export. Sample ids: [900000111, 900000243, 900000342, 900000758, 900000980]. Had we synced from listing alone we would have silently undercounted every metric.
- **6 duplicate rows** from overlapping pages were discarded (deduped by id).
- **0 orphan line items** (parent order absent from the export) — none, every child resolved to a parent. 0 orders had no line items.
- **205 duplicate events** re-delivered were discarded (deduped by event id).
- **3 listing responses had emails silently nulled** (200 OK, reason in body). Emails are sourced from `/admin/customers` instead, so no email was lost; nulled values were never trusted as truth.
- **300 customer records are email/phone shadows** (DEFECT 5, id ≥ 7000000, zero orders). Distinct real people = 9000 (records minus shadows). They carry no orders, so buyer/revenue figures are unaffected; they are flagged, not merged, because contact fields don't reliably link a shadow to its original.

## Transport hazards handled this run

- OAuth token refreshes (90s TTL): 1
- 429 rate-limit retries: 16 (of which 6 with no Retry-After header — blind backoff)
- 5xx / network retries: 0
- Bulk jobs issued / failed-and-reissued: 1 / 0
