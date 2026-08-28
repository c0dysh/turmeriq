#!/usr/bin/env python3
"""
derive_truth.py — derivation of the two figures /admin/_truth leaves open.

This is NOT the sync. The mock dataset is deterministic, so here we import it
directly and compute the answers straight from the source data. Purpose: to know
what number the sync must reach (sync.py derives the same figures from data it
actually *fetches*, which is the real exercise). Run it to see the derivation and
the traps that make the naive answer wrong:

    `python3 derive_truth.py`

It ends with asserts, so it doubles as a check: if the dataset or our reasoning
drifts, it fails loudly.
"""

import importlib.util, re
from collections import Counter
from datetime import datetime, timezone

# ---- load the mock's in-memory dataset (deterministic, seed 20260827) --------
spec = importlib.util.spec_from_file_location("mock", "mock_commerce_api.py")
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)
ORDERS, CUSTOMERS, COD_STRINGS = mock.ORDERS, mock.CUSTOMERS, mock.COD_STRINGS

NOTE_DATE_RE = re.compile(r"order placed (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
Y2023 = datetime(2023, 1, 1, tzinfo=timezone.utc)


def real_date(o):
    """DEFECT 1: migrated orders carry the import stamp in created_at; the real
    order date survives only inside the free-text note."""
    m = NOTE_DATE_RE.search(o["note"] or "")
    if m:
        return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(o["created_at"])


def is_cod(o):
    """DEFECT 4: COD is the same three real methods under eight spellings."""
    return (o["payment_method"] or "").strip().lower() in COD_STRINGS


# ============================================================================
# Figure 1 — distinct real people who have bought
# ============================================================================
# A "buyer" = someone with at least one non-cancelled order. Orders reference
# customer_id; we count distinct ids. The trap (DEFECT 5) is that 300 people
# exist as two customer records each (email record + phone record). But the
# shadow records carry ZERO orders — every order points at the original id — so
# the split does not change the buyer count. We prove that below.

live = [o for o in ORDERS if o["cancelled_at"] is None]
buyer_ids = {o["customer_id"] for o in live}
orders_on_shadows = sum(1 for o in live if o["customer_id"] >= 7000000)

distinct_buyers = len(buyer_ids)
distinct_buyers_incl_cancelled = len({o["customer_id"] for o in ORDERS})

print("=" * 70)
print("FIGURE 1 — distinct real people who have bought")
print("=" * 70)
print(f"  distinct customer_id on non-cancelled orders : {distinct_buyers}")
print(f"  (including cancelled-only buyers)            : {distinct_buyers_incl_cancelled}")
print(f"  live orders that point at a shadow id (>=7e6): {orders_on_shadows}  "
      f"<- 0 means the email/phone split can't inflate this")
print(f"  ANSWER: {distinct_buyers}")
print()


# ============================================================================
# Figure 2 — 2023-onward net revenue, exchanges counted once
# ============================================================================
# Rules, each defending against one planted defect:
#   * date >= 2023-01-01 using the RECOVERED date        (DEFECT 1)
#   * exclude cancelled orders                            (voided)
#   * count "paid" AND COD-"pending" as realized revenue (DEFECT 4)
#   * exclude "refunded" -> this is what makes an exchange net to ONE:
#         the exchange source is flipped to "refunded" (excluded)
#         the exchange twin is "paid" (included, once)   (DEFECT 3)

def counts_as_revenue(o):
    if real_date(o) < Y2023:
        return False
    if o["cancelled_at"] is not None:
        return False
    fs = o["financial_status"]
    return fs == "paid" or (fs == "pending" and is_cod(o))


net_revenue = round(sum(o["total_price"] for o in ORDERS if counts_as_revenue(o)), 2)

# The naive-but-plausible number, for contrast: trust created_at, count only paid.
naive_revenue = round(sum(
    o["total_price"] for o in ORDERS
    if datetime.fromisoformat(o["created_at"]) >= Y2023 and o["financial_status"] == "paid"
), 2)

# Why naive is *higher* despite dropping COD: every migrated order carries the
# 2023 import stamp, so all 2,600 of them get counted as 2023 revenue.
legacy = [o for o in ORDERS if o["tags"].startswith("migrated")]
legacy_wrongly_2023 = sum(1 for o in legacy
                          if datetime.fromisoformat(o["created_at"]) >= Y2023)
legacy_really_2023 = sum(1 for o in legacy if real_date(o) >= Y2023)

# Exchange sanity: twins are all "paid", counted once; sources are "refunded".
twins = [o for o in ORDERS if o["tags"] == "exchange"]
twin_revenue = round(sum(o["total_price"] for o in twins), 2)

status_breakdown = Counter(
    (o["financial_status"], "cod" if is_cod(o) else "prepaid")
    for o in ORDERS if real_date(o) >= Y2023 and o["cancelled_at"] is None)

print("=" * 70)
print("FIGURE 2 — 2023-onward net revenue, exchanges counted once")
print("=" * 70)
print(f"  NET (correct)   : Rs {net_revenue:,.2f}")
print(f"  NAIVE (wrong)   : Rs {naive_revenue:,.2f}  (trust created_at, paid only)")
print(f"  ANSWER: {net_revenue}")
print()
print("  why naive is wrong:")
print(f"    migrated orders dated >=2023 by created_at : {legacy_wrongly_2023}  (all of them)")
print(f"    migrated orders truly >=2023 by real date  : {legacy_really_2023}")
print(f"    exchange twins (all paid, counted once)    : {len(twins)}  "
      f"= Rs {twin_revenue:,.2f}")
print(f"  2023 live status breakdown (status, method): {dict(status_breakdown)}")
print()


# ============================================================================
# Checks — fail loudly if reasoning or dataset drifts
# ============================================================================
assert distinct_buyers == 3094, distinct_buyers
assert orders_on_shadows == 0, orders_on_shadows
assert net_revenue == 20529284.0, net_revenue
assert naive_revenue == 21084985.0, naive_revenue
assert legacy_wrongly_2023 == 2600 and legacy_really_2023 == 0
assert len(twins) == 240 and all(o["financial_status"] == "paid" for o in twins)
print("all derivations checked OK")
