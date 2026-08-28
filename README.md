# TurmerIQ — commerce sync (Part A)

Pulls a brand's entire order/customer/event history out of the mock commerce API
into a local SQLite store you can compute revenue, repeat-purchase rate and
per-customer history from — then **proves it landed correctly** by reconciling
against the API's own ground-truth counts.

Standard library only. No dependencies.

## Run it

```bash
# 1. start the mock API (prints client_id / client_secret)
python3 mock_commerce_api.py --port 8090

# 2. in another shell, run the sync
python3 sync.py --base http://localhost:8090

#    -> builds turmeriq.db, prints the reconciliation report,
#       writes reconciliation_report.md and .json
#       exits 0 if every check passes, 1 if any mismatch
```

Optional — see the two derived figures worked out from first principles:

```bash
python3 derive_truth.py
```

## What lands

A SQLite file `turmeriq.db` with five tables:

| table | rows | what it is |
|---|---|---|
| `orders` | 9040 | one row per order, from the **bulk export** (the only complete source). Adds cleaned columns: `real_created_at`, `is_cod`, `is_exchange_twin`. |
| `line_items` | 12620 | products inside each order, reattached to parents by `__parentId` |
| `customers` | 9300 | one row per customer record; shadows flagged `is_shadow=1` |
| `events` | 1439 | webhook feed after deduping (1644 received) |
| `listing_ids` | 9004 | the ids the paginated list returned — kept only to prove which orders it hid |

Every headline number is one SQL query over these tables. The tricky ones lean
on the derived columns, which is where all the cleanup lives.

## Two source paths, on purpose

- **Orders come from the bulk export**, not the paginated list, because the list
  is structurally incomplete (see hazard #1 below). Bulk is the authoritative,
  complete order source.
- **The list is fetched anyway**, into `listing_ids`, purely so the report can
  subtract the two id-sets and *prove* exactly which orders the list was hiding.

## The reconciliation report

`reconciliation_report.md` is the "prove it landed" deliverable. It shows what
was fetched, what was written, what was rejected and why, and diffs every
computed figure against `/admin/_truth`. From one run:

- **All 5 ground-truth checks pass** (orders total, reachable-by-listing,
  cancelled, customer records, exchange pairs), plus `hidden = total − listable`.
- **36 orders** recovered that the paginated list never showed.
- Duplicates discarded: overlapping-page order rows and re-delivered events.
- 2 listing responses had emails silently nulled — counted, never trusted.

### The two derived figures (`/admin/_truth` withholds these)

| figure | value | how it's earned |
|---|---|---|
| **distinct real people who have bought** | **3,094** | `COUNT(DISTINCT customer_id)` on non-cancelled orders. Immune to the shadow-record split because shadows carry zero orders. |
| **2023-onward net revenue, exchanges counted once** | **₹20,529,284** | real date ≥ 2023 (recovered from note), exclude cancelled, count `paid` + COD-`pending`, exclude `refunded` (which makes each exchange net to one). |

For contrast the naive number — trust `created_at`, count only `paid` — is
**₹21,084,985**, i.e. *higher*, because all 2,600 migrated orders carry the 2023
import stamp and get counted as 2023 revenue.

`derive_truth.py` is an **answer key, not part of the sync**: it reads the
deterministic dataset directly to tell me what "correct" is. `sync.py` never
imports the dataset — it only talks HTTP, and has to reach the same numbers the
hard way. That separation is the point.

---

## What the API did to me, and what I did about each

**Transport (the fetch misbehaves):**

1. **~0.4% of orders never appear in the paginated list.** Syncing from the list
   would silently undercount everything. → Source orders from the **bulk export**
   (complete); use the list only to prove the 36 hidden ids.
2. **Auth token expires after 90 seconds**, mid-sync. → Client refreshes
   proactively (15s before expiry) and on any 401, then retries.
3. **Rate limiter returns 429, and omits `Retry-After` one third of the time.**
   → Honour the header when present; fall back to exponential backoff when it's
   absent.
4. **The list repeats its last two rows at the top of ~1-in-12 pages.** → Dedupe
   by order `id` while paging.
5. **~1-in-14 list responses null the emails** (HTTP 200, reason buried in body).
   → Count them, never trust a nulled value; emails are sourced from
   `/admin/customers` instead, so none is lost.
6. **Bulk export fails ~1-in-4 jobs** partway. → Poll to terminal status;
   re-issue on `FAILED`, up to 7 attempts.
7. **Bulk download is shuffled parent/child JSONL** — line-items don't follow
   their order. → Two-pass parse: collect orders, collect line-items by
   `__parentId`, then attach.
8. **Event feed is ~15% duplicates, shuffled.** → Dedupe by event id.

**Data (the mess inside):**

9. **Migrated orders carry the import timestamp, not the real order date**
   (real one is only in the free-text note). → Regex the note (`order placed …`)
   into `real_created_at`; fall back to `created_at` for native orders.
10. **Exchanges are recorded as a refunded original + a paid twin.** → Rule
    "exclude refunded, include paid" counts each exchange exactly once, no
    special-casing.
11. **COD orders are left at `pending` forever.** → Classify the payment string
    (8 spellings → COD) and treat COD-`pending` as realized revenue.
12. **300 buyers exist twice, split across email and phone.** → Flag shadow
    records (id ≥ 7,000,000, zero orders); buyer/revenue counts are unaffected
    because no order references a shadow.
13. **Order numbers are minted from two ranges across the migration.** The
    "overlap" the source warns about doesn't actually occur in this dataset (0
    collisions), but the shape is the hazard. → Key everything on `id`, which is
    provably unique (9040/9040).

## Judgement calls (where the data didn't tell me the answer)

1. **COD-pending counts as revenue.** COD is cash collected on delivery; the
   platform just never flips the status. *Alternative:* count only `paid` —
   which would drop ~2,200 real orders (~₹0.5M). Chose to count it because the
   money is real; a status the platform forgot to update isn't evidence of a
   non-sale.
2. **An exchange counts once, at the twin's value.** *Alternatives:* count both
   (double-counts a single sale) or net them to zero (ignores that the customer
   kept the goods). "Exclude refunded, include paid" lands on one, and falls out
   of the general refund rule without special-casing exchanges.
3. **"Bought" = at least one non-cancelled order.** *Alternative:* include
   cancelled-only buyers → 3,179 instead of 3,094. Chose completed purchases,
   since a cancelled-only customer never actually transacted. Both numbers are in
   the report so the definition is explicit.
4. **Shadow records are flagged, not merged.** After the split the two records
   share no email or phone, so linking them from contact fields alone isn't
   reliable. *Alternative:* merge on the id heuristic (7000000+X ↔ 5000000+X) —
   but that's reverse-engineering the generator, not something a real integration
   could do. Since shadows carry no orders, flagging is enough for correct
   metrics; genuine identity resolution is called out as future work.

## Files

- `sync.py` — the sync (deliverable). HTTP only.
- `derive_truth.py` — answer key for the two derived figures (not part of the sync).
- `reconciliation_report.md` / `.json` — generated by each run.
- `turmeriq.db` — the SQLite store (generated).
- `PART_B_DESIGN.md` — the design question.
- `mock_commerce_api.py` — the provided mock (unmodified).
