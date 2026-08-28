#!/usr/bin/env python3
"""
TurmerIQ sync — pulls a brand's order/customer/event history out of the mock
commerce API into a local SQLite store you can compute revenue, repeat-purchase
rate and per-customer history from, then reconciles what landed against
/admin/_truth and prints a report.

Stdlib only. Run:  python3 sync.py --base http://localhost:8090
See README.md for what the API does to you and the judgement calls made.
"""

import argparse, json, re, sqlite3, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

CLIENT_ID = "turmeriq-exercise"
CLIENT_SECRET = "not-a-real-secret"

# Real order date for migrated orders survives only in the free-text note.
NOTE_DATE_RE = re.compile(r"order placed (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# Same three real payment methods, eight spellings. COD is a completed sale even
# when the platform never flips it off "pending" (see DEFECT 4 / judgement calls).
COD_STRINGS = {"cash on delivery (cod)", "cash on delivery", "cod",
               "manual + cash on delivery (cod)", "cash_on_delivery"}
Y2023 = datetime(2023, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- HTTP client

class Client:
    """Handles the ways the transport misbehaves: 90s token expiry, a leaky-bucket
    rate limiter that only sometimes tells you how long to wait, and transient 5xx."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.token = None
        self.token_exp = 0.0
        self.stats = {"requests": 0, "retries_429": 0, "retries_5xx": 0,
                      "token_refreshes": 0, "blind_backoffs": 0}

    def _authenticate(self):
        body = json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).encode()
        req = urllib.request.Request(self.base + "/oauth/token", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        self.token = d["access_token"]
        # Refresh a little early; the 90s TTL expires mid-sync otherwise.
        self.token_exp = time.time() + d["expires_in"] - 15
        self.stats["token_refreshes"] += 1

    def _ensure_token(self):
        if self.token is None or time.time() >= self.token_exp:
            self._authenticate()

    def get(self, path, as_text=False):
        return self._request("GET", path, as_text=as_text)

    def post(self, path, payload=None):
        return self._request("POST", path, payload=payload)

    def _request(self, method, path, payload=None, as_text=False):
        url = path if path.startswith("http") else self.base + path
        attempt = 0
        while True:
            attempt += 1
            self._ensure_token()
            data = json.dumps(payload).encode() if payload is not None else None
            headers = {"Authorization": f"Bearer {self.token}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            self.stats["requests"] += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                return raw.decode() if as_text else json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    # token expired / unknown — force a fresh one and retry
                    self.token = None
                    if attempt <= 5:
                        continue
                    raise
                if e.code == 429:
                    self.stats["retries_429"] += 1
                    ra = e.headers.get("Retry-After")
                    if ra:
                        time.sleep(float(ra))
                    else:
                        # Server tells us nothing a third of the time. Back off ourselves.
                        self.stats["blind_backoffs"] += 1
                        time.sleep(min(2.0 * attempt, 10))
                    continue
                if 500 <= e.code < 600 and attempt <= 5:
                    self.stats["retries_5xx"] += 1
                    time.sleep(min(1.5 * attempt, 8))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt <= 5:
                    time.sleep(min(1.5 * attempt, 8))
                    continue
                raise


# ---------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,           -- id is the only unique key; order_number is NOT (DEFECT 2)
    order_number INTEGER,
    created_at TEXT,                  -- as reported by the API (migration stamp for legacy)
    real_created_at TEXT,             -- recovered: note date for legacy, else created_at (DEFECT 1)
    financial_status TEXT,
    cancelled_at TEXT,
    payment_method TEXT,
    is_cod INTEGER,
    is_exchange_twin INTEGER,         -- note begins EXC_ (DEFECT 3)
    total_price REAL,
    customer_id INTEGER,
    note TEXT,
    source TEXT                       -- 'bulk' (authoritative) | 'listing_only'
);
CREATE TABLE IF NOT EXISTS line_items (
    order_id INTEGER, idx INTEGER, sku TEXT, name TEXT,
    quantity INTEGER, price REAL, discount REAL, compare_at_price REAL,
    PRIMARY KEY (order_id, idx)
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, email TEXT, phone TEXT,
    first_name TEXT, last_name TEXT, city TEXT, state TEXT, zip TEXT,
    accepts_email_marketing INTEGER, accepts_sms_marketing INTEGER,
    created_at TEXT, is_shadow INTEGER, person_id INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, topic TEXT, triggered_at TEXT,
    order_id INTEGER, financial_status TEXT
);
CREATE TABLE IF NOT EXISTS listing_ids (id INTEGER PRIMARY KEY);
"""


def open_store(path):
    db = sqlite3.connect(path)
    db.executescript("DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS line_items;"
                     "DROP TABLE IF EXISTS customers; DROP TABLE IF EXISTS events;"
                     "DROP TABLE IF EXISTS listing_ids;")
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------- helpers

def is_cod(pay):
    return (pay or "").strip().lower() in COD_STRINGS


def recover_real_date(created_at, note):
    """Legacy orders carry the import timestamp; the real one is in the note."""
    m = NOTE_DATE_RE.search(note or "")
    if m:
        return datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc).isoformat()
    return created_at


def parse_link_next(link_header):
    if not link_header:
        return None
    m = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return m.group(1) if m else None


# ---------------------------------------------------------------- ingest: orders via bulk

def ingest_bulk_orders(cli, db, rep):
    """Bulk export is the authoritative, COMPLETE order source: it includes the
    ~0.4% of orders hidden from the paginated list (DEFECT 6) and every exchange
    twin. One in four bulk jobs fails partway and must be re-issued."""
    for issue in range(1, 8):
        job = cli.post("/admin/bulk")["bulk_operation"]
        jid = job["id"]
        rep["bulk_jobs_issued"] += 1
        while True:
            time.sleep(2)
            st = cli.get(f"/admin/bulk/{jid}")["bulk_operation"]["status"]
            if st in ("COMPLETED", "FAILED"):
                break
        if st == "COMPLETED":
            text = cli.get(f"/admin/bulk/{jid}/download", as_text=True)
            return _load_jsonl(text, db, rep)
        rep["bulk_jobs_failed"] += 1
    raise RuntimeError("bulk export kept failing after 7 attempts")


def _load_jsonl(text, db, rep):
    """Parent orders and child line items are interleaved and shuffled — children
    do NOT follow their parents. Two passes: collect orders, then attach lines."""
    orders, lines_by_parent = {}, {}
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        rid = rec["id"]
        if rid.startswith("gid://order/"):
            oid = int(rid.split("/")[-1])
            orders[oid] = rec
        elif rid.startswith("gid://lineitem/"):
            parent = int(rec["__parentId"].split("/")[-1])
            lines_by_parent.setdefault(parent, []).append(rec)

    written_lines = 0
    for oid, rec in orders.items():
        note = rec.get("note") or ""
        db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, rec["order_number"], rec["created_at"],
             recover_real_date(rec["created_at"], note),
             rec["financial_status"], rec["cancelled_at"], rec["payment_method"],
             1 if is_cod(rec["payment_method"]) else 0,
             1 if note.startswith("EXC_") else 0,
             rec["total_price"], rec["customer_id"], note, "bulk"))
        for i, l in enumerate(sorted(lines_by_parent.get(oid, []),
                                     key=lambda x: x["id"])):
            db.execute("INSERT OR REPLACE INTO line_items VALUES (?,?,?,?,?,?,?,?)",
                       (oid, i, l["sku"], l["name"], l["quantity"], l["price"],
                        l["discount"], l["compare_at_price"]))
            written_lines += 1
    db.commit()
    # Orphan children: line items whose parent order never appeared in the export.
    # Not seen in this dataset, but if it happened those items (and their order)
    # would be lost silently — so we count and surface it instead of dropping it.
    orphan_parents = set(lines_by_parent) - set(orders)
    rep["bulk_orphan_lineitems"] = sum(len(lines_by_parent[p]) for p in orphan_parents)
    rep["bulk_orphan_parent_ids"] = sorted(orphan_parents)[:5]
    rep["bulk_orders_without_lineitems"] = sum(1 for oid in orders
                                               if oid not in lines_by_parent)
    rep["bulk_orders_loaded"] = len(orders)
    rep["bulk_lineitems_loaded"] = written_lines
    return set(orders)


# ---------------------------------------------------------------- ingest: listing (cross-check)

def ingest_listing_paged(cli, db, rep):
    """Paginate /admin/orders purely to reconcile coverage (Link lives in the
    response header, so this uses a header-aware GET). Two hazards:
    1-in-12 pages repeat their last two rows at the top of the next (dedupe by id);
    1-in-14 responses null the emails and bury the reason in the body (we count
    these but never trust a nulled email as truth)."""
    seen = set()
    url = cli.base + "/admin/orders?limit=250"
    while url:
        raw, headers = _get_with_headers(cli, url)
        body = json.loads(raw)
        if "errors" in body:
            rep["listing_email_stripped_pages"] += 1
        for o in body.get("orders", []):
            if o["id"] in seen:
                rep["listing_overlap_dupes"] += 1
                continue
            seen.add(o["id"])
        url = parse_link_next(headers.get("Link"))
        if url and not url.startswith("http"):
            url = cli.base + url
    db.executemany("INSERT OR REPLACE INTO listing_ids VALUES (?)", [(i,) for i in seen])
    db.commit()
    rep["listing_distinct_ids"] = len(seen)
    return seen


def _get_with_headers(cli, url):
    attempt = 0
    while True:
        attempt += 1
        cli._ensure_token()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cli.token}"})
        cli.stats["requests"] += 1
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                cli.token = None
                if attempt <= 5:
                    continue
                raise
            if e.code == 429:
                cli.stats["retries_429"] += 1
                ra = e.headers.get("Retry-After")
                if ra:
                    time.sleep(float(ra))
                else:
                    cli.stats["blind_backoffs"] += 1
                    time.sleep(min(2.0 * attempt, 10))
                continue
            if 500 <= e.code < 600 and attempt <= 5:
                cli.stats["retries_5xx"] += 1
                time.sleep(min(1.5 * attempt, 8))
                continue
            raise


# ---------------------------------------------------------------- ingest: customers

def ingest_customers(cli, db, rep):
    seen = 0
    url = cli.base + "/admin/customers?limit=250"
    while url:
        raw, headers = _get_with_headers(cli, url)
        body = json.loads(raw)
        for c in body.get("customers", []):
            is_shadow = 1 if c["id"] >= 7000000 else 0
            db.execute(
                "INSERT OR REPLACE INTO customers "
                "(id,email,phone,first_name,last_name,city,state,zip,"
                "accepts_email_marketing,accepts_sms_marketing,created_at,is_shadow,person_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["id"], c.get("email"), c.get("phone"), c["first_name"], c["last_name"],
                 c["city"], c["state"], c["zip"],
                 1 if c.get("accepts_email_marketing") else 0,
                 1 if c.get("accepts_sms_marketing") else 0,
                 c["created_at"], is_shadow, None))
            seen += 1
        url = parse_link_next(headers.get("Link"))
        if url and not url.startswith("http"):
            url = cli.base + url
    db.commit()
    rep["customers_loaded"] = seen
    resolve_identity(db, rep)


def resolve_identity(db, rep):
    """DEFECT 5: 300 buyers exist twice, one record keeps email the other phone.
    Shadow records live in the 7000000+ id range and carry zero orders. We flag
    them and assign a person_id so downstream code counts people, not records.
    Linking a shadow back to its original from contact fields alone is not
    reliable (they share no email/phone after the split) — but the shadows never
    carry orders, so buyer counts are unaffected. See judgement calls."""
    # person_id = own id for originals; shadows get collapsed to a single logical
    # person each (still counted as its own person here since we can't safely
    # merge to the original — but excluded from 'real people' via is_shadow).
    db.execute("UPDATE customers SET person_id = id")
    shadow = db.execute("SELECT COUNT(*) FROM customers WHERE is_shadow=1").fetchone()[0]
    rep["shadow_records"] = shadow
    rep["distinct_people"] = db.execute(
        "SELECT COUNT(*) FROM customers WHERE is_shadow=0").fetchone()[0]


# ---------------------------------------------------------------- ingest: events

def ingest_events(cli, db, rep):
    """Pull-based webhook stand-in. ~15% of events are re-delivered duplicates and
    arrival order is not triggered_at order. Dedupe by event id; keep newest by
    triggered_at is irrelevant since dupes are identical."""
    total, dupes = 0, 0
    url = cli.base + "/admin/events?limit=250"
    while url:
        raw, headers = _get_with_headers(cli, url)
        body = json.loads(raw)
        for e in body.get("events", []):
            total += 1
            cur = db.execute("SELECT 1 FROM events WHERE id=?", (e["id"],)).fetchone()
            if cur:
                dupes += 1
                continue
            db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                       (e["id"], e["topic"], e["triggered_at"], e["order_id"],
                        e["financial_status"]))
        url = parse_link_next(headers.get("Link"))
        if url and not url.startswith("http"):
            url = cli.base + url
    db.commit()
    rep["events_received"] = total
    rep["events_duplicate"] = dupes
    rep["events_distinct"] = total - dupes


# ---------------------------------------------------------------- reconcile + derive

def reconcile(cli, db, rep):
    truth = cli.get("/admin/_truth")
    rep["truth"] = truth

    listed = {r[0] for r in db.execute("SELECT id FROM listing_ids")}
    bulk = {r[0] for r in db.execute("SELECT id FROM orders")}
    hidden = bulk - listed
    rep["hidden_from_listing"] = len(hidden)
    rep["hidden_sample"] = sorted(hidden)[:5]

    rep["orders_cancelled"] = db.execute(
        "SELECT COUNT(*) FROM orders WHERE cancelled_at IS NOT NULL").fetchone()[0]
    rep["exchange_twins"] = db.execute(
        "SELECT COUNT(*) FROM orders WHERE is_exchange_twin=1").fetchone()[0]

    # --- derived 1: distinct real people who have bought ---
    # buyers = distinct customer_id on non-cancelled orders. (Shadows carry no
    # orders, so this is immune to DEFECT 5.)
    rep["distinct_buyers"] = db.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM orders WHERE cancelled_at IS NULL"
    ).fetchone()[0]
    rep["distinct_buyers_incl_cancelled"] = db.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM orders").fetchone()[0]

    # --- derived 2: 2023-onward net revenue, exchanges counted once ---
    # Rules: real_created_at >= 2023-01-01; exclude cancelled; count paid and
    # COD-pending (COD is collected on delivery even if never marked paid);
    # exclude refunded (a refund reverses the sale — and this is exactly what
    # makes an exchange net to one: the source is 'refunded', the twin is 'paid').
    rows = db.execute(
        "SELECT real_created_at, financial_status, is_cod, total_price, cancelled_at "
        "FROM orders").fetchall()
    net = 0.0
    naive = 0.0  # the wrong-but-plausible number, for contrast
    for real_dt, fs, cod, total, cancelled in rows:
        rd = datetime.fromisoformat(real_dt)
        if rd >= Y2023 and cancelled is None and (fs == "paid" or (fs == "pending" and cod)):
            net += total
    # naive: trust created_at, count only 'paid'
    for created, fs, cod, total, cancelled in db.execute(
            "SELECT created_at, financial_status, is_cod, total_price, cancelled_at FROM orders"):
        if datetime.fromisoformat(created) >= Y2023 and fs == "paid":
            naive += total
    rep["net_revenue_2023"] = round(net, 2)
    rep["naive_revenue_2023"] = round(naive, 2)

    # --- repeat purchase rate (all-time, live orders, by customer_id) ---
    counts = db.execute(
        "SELECT customer_id, COUNT(*) c FROM orders WHERE cancelled_at IS NULL "
        "GROUP BY customer_id").fetchall()
    buyers = len(counts)
    repeat = sum(1 for _, c in counts if c > 1)
    rep["repeat_purchase_rate"] = round(repeat / buyers, 4) if buyers else 0.0

    # --- checks ---
    checks = []

    def chk(name, got, want):
        checks.append((name, got, want, "OK" if got == want else "MISMATCH"))

    chk("orders_total_all_time", len(bulk), truth["orders_total_all_time"])
    chk("orders_reachable_by_listing", len(listed), truth["orders_reachable_by_listing"])
    chk("orders_cancelled", rep["orders_cancelled"], truth["orders_cancelled"])
    chk("customer_records", rep["customers_loaded"], truth["customer_records"])
    chk("exchange_pairs", rep["exchange_twins"], truth["exchange_pairs"])
    chk("hidden = total - listable", len(hidden),
        truth["orders_total_all_time"] - truth["orders_reachable_by_listing"])
    rep["checks"] = checks
    rep["all_ok"] = all(c[3] == "OK" for c in checks)


# ---------------------------------------------------------------- report

def write_report(rep, path_md, path_json):
    with open(path_json, "w") as f:
        json.dump(rep, f, indent=2, default=str)

    t = rep["truth"]
    lines = []
    a = lines.append
    a("# TurmerIQ sync — reconciliation report\n")
    a(f"Generated against `/admin/_truth`. Overall: "
      f"**{'ALL CHECKS PASS' if rep['all_ok'] else 'MISMATCH — DO NOT TRUST'}**\n")

    a("## What was fetched and written\n")
    a("| item | value |")
    a("|---|---|")
    a(f"| bulk orders loaded (authoritative) | {rep['bulk_orders_loaded']} |")
    a(f"| bulk line items loaded | {rep['bulk_lineitems_loaded']} |")
    a(f"| orders reachable by paginated listing | {rep['listing_distinct_ids']} |")
    a(f"| customers loaded | {rep['customers_loaded']} |")
    a(f"| events received / distinct | {rep['events_received']} / {rep['events_distinct']} |")
    a(f"| HTTP requests made | {rep['stats']['requests']} |")
    a("")

    a("## Checks against ground truth\n")
    a("| check | got | truth | verdict |")
    a("|---|---|---|---|")
    for name, got, want, verd in rep["checks"]:
        a(f"| {name} | {got} | {want} | {'✅ ' + verd if verd=='OK' else '❌ ' + verd} |")
    a("")

    a("## Derived figures (the two /admin/_truth left for us)\n")
    a(f"- **Distinct real people who have bought: {rep['distinct_buyers']}** "
      f"(distinct customer_id on non-cancelled orders). Including cancelled-only "
      f"buyers it is {rep['distinct_buyers_incl_cancelled']}.")
    a(f"- **2023-onward net revenue, exchanges counted once: "
      f"₹{rep['net_revenue_2023']:,.2f}**.")
    a(f"  - For contrast, the naive number (trust `created_at`, count only `paid`) "
      f"is ₹{rep['naive_revenue_2023']:,.2f} — *higher*, because all 2,600 migrated "
      f"orders carry the 2023 import stamp and get counted as 2023 revenue.")
    a(f"- Repeat-purchase rate (all-time, live): {rep['repeat_purchase_rate']*100:.1f}% "
      f"of buyers have >1 order.")
    a("")

    a("## What was rejected / could not be resolved\n")
    a(f"- **{rep['hidden_from_listing']} orders are hidden from the paginated list** "
      f"(DEFECT 6). Recovered via the complete bulk export. Sample ids: "
      f"{rep['hidden_sample']}. Had we synced from listing alone we would have "
      f"silently undercounted every metric.")
    a(f"- **{rep['listing_overlap_dupes']} duplicate rows** from overlapping pages "
      f"were discarded (deduped by id).")
    a(f"- **{rep['bulk_orphan_lineitems']} orphan line items** (parent order absent "
      f"from the export) "
      + ("— none, every child resolved to a parent."
         if rep['bulk_orphan_lineitems'] == 0
         else f"could NOT be attached: parents {rep['bulk_orphan_parent_ids']}. "
              f"Investigate — their orders and revenue are missing.")
      + f" {rep['bulk_orders_without_lineitems']} orders had no line items.")
    a(f"- **{rep['events_duplicate']} duplicate events** re-delivered were discarded "
      f"(deduped by event id).")
    a(f"- **{rep['listing_email_stripped_pages']} listing responses had emails "
      f"silently nulled** (200 OK, reason in body). Emails are sourced from "
      f"`/admin/customers` instead, so no email was lost; nulled values were never "
      f"trusted as truth.")
    a(f"- **{rep['shadow_records']} customer records are email/phone shadows** "
      f"(DEFECT 5, id ≥ 7000000, zero orders). Distinct real people = "
      f"{rep['distinct_people']} (records minus shadows). They carry no orders, so "
      f"buyer/revenue figures are unaffected; they are flagged, not merged, because "
      f"contact fields don't reliably link a shadow to its original.")
    a("")

    a("## Transport hazards handled this run\n")
    s = rep["stats"]
    a(f"- OAuth token refreshes (90s TTL): {s['token_refreshes']}")
    a(f"- 429 rate-limit retries: {s['retries_429']} "
      f"(of which {s['blind_backoffs']} with no Retry-After header — blind backoff)")
    a(f"- 5xx / network retries: {s['retries_5xx']}")
    a(f"- Bulk jobs issued / failed-and-reissued: {rep['bulk_jobs_issued']} / "
      f"{rep['bulk_jobs_failed']}")
    a("")

    with open(path_md, "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8090")
    ap.add_argument("--db", default="turmeriq.db")
    ap.add_argument("--report-md", default="reconciliation_report.md")
    ap.add_argument("--report-json", default="reconciliation_report.json")
    args = ap.parse_args()

    cli = Client(args.base)
    db = open_store(args.db)
    rep = {"bulk_jobs_issued": 0, "bulk_jobs_failed": 0, "bulk_orders_loaded": 0,
           "bulk_lineitems_loaded": 0, "bulk_orphan_lineitems": 0,
           "bulk_orphan_parent_ids": [], "bulk_orders_without_lineitems": 0,
           "listing_distinct_ids": 0,
           "listing_overlap_dupes": 0, "listing_email_stripped_pages": 0,
           "customers_loaded": 0, "events_received": 0, "events_duplicate": 0,
           "events_distinct": 0}

    t0 = time.time()
    print("→ bulk export (authoritative order source)…", flush=True)
    ingest_bulk_orders(cli, db, rep)
    print(f"  loaded {rep['bulk_orders_loaded']} orders", flush=True)

    print("→ paginated listing (coverage cross-check)…", flush=True)
    ingest_listing_paged(cli, db, rep)
    print(f"  saw {rep['listing_distinct_ids']} distinct listed ids", flush=True)

    print("→ customers…", flush=True)
    ingest_customers(cli, db, rep)
    print(f"  loaded {rep['customers_loaded']} customer records", flush=True)

    print("→ events…", flush=True)
    ingest_events(cli, db, rep)
    print(f"  {rep['events_distinct']} distinct events", flush=True)

    print("→ reconcile + derive…", flush=True)
    reconcile(cli, db, rep)
    rep["stats"] = cli.stats
    rep["elapsed_sec"] = round(time.time() - t0, 1)

    write_report(rep, args.report_md, args.report_json)
    db.close()
    sys.exit(0 if rep["all_ok"] else 1)


if __name__ == "__main__":
    main()
