#!/usr/bin/env python3
"""
Mock Commerce API — TurmerIQ engineering exercise.

Run:  python3 mock_commerce_api.py          (serves on http://localhost:8080)
      python3 mock_commerce_api.py --port 9000

Standard library only. No installation required. Python 3.8+.

This server imitates a commerce platform's admin API. It is deterministic:
the same dataset is generated on every run, so results are comparable.

See EXERCISE.md for what you are being asked to build.
"""

import json, random, re, time, hashlib, argparse, threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SEED = 20260827
CLIENT_ID = "turmeriq-exercise"
CLIENT_SECRET = "not-a-real-secret"
TOKEN_TTL = 90          # seconds — deliberately short
BUCKET_CAPACITY = 40
BUCKET_REFILL = 2.0     # tokens per second

# ---------------------------------------------------------------- dataset

PRODUCTS = [
    ("APMCS", "Men Hemp Half Sleeve Shirt", "Shirts", 2499),
    ("APMFS", "Men Hemp Full Sleeve Shirt", "Shirts", 2899),
    ("APMCP", "Men Hemp Comfort Pant", "Pants", 3199),
    ("APMLP", "Men Hemp Lounge Pant", "Pants", 2799),
    ("APMTR", "Men Hemp Trouser", "Pants", 3499),
    ("APWTU", "Women Hemp Tunic", "Tops", 2299),
    ("APWBR", "Women Hemp Brief", "Innerwear", 899),
    ("APMBR", "Men Hemp Brief", "Innerwear", 799),
    ("APMCO", "Men Co-ord Set", "Co-ord", 5499),
]
SIZES = ["S", "M", "L", "XL", "XXL"]
CITIES = [("Mumbai","Maharashtra","400001"),("Delhi","Delhi","110001"),
          ("Bengaluru","Karnataka","560001"),("Pune","Maharashtra","411001"),
          ("Hyderabad","Telangana","500001"),("Chennai","Tamil Nadu","600001"),
          ("Kolkata","West Bengal","700001"),("Jaipur","Rajasthan","302001")]
FIRST = ["Aarav","Vivaan","Aditya","Ishaan","Kabir","Ananya","Diya","Aadhya",
         "Meera","Riya","Rohan","Arjun","Sanya","Tara","Nikhil","Priya"]
LAST  = ["Sharma","Verma","Iyer","Nair","Reddy","Kapoor","Mehta","Bose",
         "Chopra","Rao","Gupta","Menon","Shah","Joshi"]

# Free-text payment strings — same three real methods, eight spellings.
PAY_VARIANTS = [
    "Cash on Delivery (COD)", "Cash on delivery", "COD",
    "manual + Cash on Delivery (COD)", "cash_on_delivery",
    "Razorpay", "razorpay ", "Credit Card (Razorpay)", "UPI", "upi",
]
COD_STRINGS = {"cash on delivery (cod)", "cash on delivery", "cod",
               "manual + cash on delivery (cod)", "cash_on_delivery"}

MIGRATION_STAMP = datetime(2023, 1, 3, 11, 30, tzinfo=timezone.utc)
CUTOVER         = datetime(2023, 1, 3, tzinfo=timezone.utc)
NOW             = datetime(2026, 8, 20, tzinfo=timezone.utc)


def build_dataset():
    """Deterministic. Returns (orders, customers, events)."""
    rnd = random.Random(SEED)
    customers, orders = {}, []

    n_customers = 9000
    for i in range(n_customers):
        cid = 5000000 + i
        fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        city, state, pin = rnd.choice(CITIES)
        has_email = rnd.random() < 0.86
        has_phone = rnd.random() < 0.83
        if not has_email and not has_phone:
            has_phone = True
        customers[cid] = {
            "id": cid,
            "first_name": fn, "last_name": ln,
            "email": f"{fn.lower()}.{ln.lower()}{i}@example.com" if has_email else None,
            "phone": f"+919{rnd.randint(100000000, 999999999)}" if has_phone else None,
            "city": city, "state": state, "zip": pin,
            "accepts_email_marketing": rnd.random() < 0.42,
            "accepts_sms_marketing": rnd.random() < 0.034,
            "accepts_whatsapp_marketing": False,
            "created_at": (NOW - timedelta(days=rnd.randint(1, 2100))).isoformat(),
            "orders_count": 0, "total_spent": 0.0,
        }

    # ---- legacy orders (pre-cutover, migrated: dates overwritten) ----
    legacy_numbers = list(range(1498, 1498 + 2600))
    rnd.shuffle(legacy_numbers)
    oid = 900000000
    buyers = rnd.sample(list(customers), 3400)

    def make_lines(r):
        out = []
        for _ in range(r.choices([1, 2, 3], weights=[68, 24, 8])[0]):
            sku_base, name, cat, price = r.choice(PRODUCTS)
            size = r.choice(SIZES)
            qty = r.choices([1, 2], weights=[92, 8])[0]
            disc = r.choice([0, 0, 0, 0, 200, 300, 500])
            out.append({
                "sku": f"{sku_base}{size}", "name": f"{name} - {size}",
                "category_hint": cat, "quantity": qty,
                "price": float(price), "discount": float(disc),
                "compare_at_price": float(round(price * 1.25)) if r.random() < 0.26 else None,
            })
        return out

    for k in range(2600):
        oid += 1
        cust = rnd.choice(buyers)
        real_dt = CUTOVER - timedelta(days=rnd.randint(1, 850),
                                      seconds=rnd.randint(0, 86399))
        lines = make_lines(rnd)
        sub = sum(l["price"] * l["quantity"] - l["discount"] for l in lines)
        pay = rnd.choice(PAY_VARIANTS)
        orders.append({
            "id": oid,
            "order_number": legacy_numbers[k],
            # DEFECT 1: migrated orders carry the import timestamp, not the real one.
            "created_at": MIGRATION_STAMP.isoformat(),
            "processed_at": MIGRATION_STAMP.isoformat(),
            "updated_at": MIGRATION_STAMP.isoformat(),
            # The real timestamp survives only inside the free-text note.
            "note": f"Imported via DataPorter. Original platform log: "
                    f"order placed {real_dt.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"legacy_ref {rnd.randint(10000,99999)}",
            "tags": "migrated,dataporter",
            "customer_id": cust,
            "financial_status": rnd.choices(["paid", "refunded"], weights=[94, 6])[0],
            "cancelled_at": None,
            "payment_method": pay,
            "subtotal_price": round(sub, 2),
            "shipping_price": 0.0 if sub > 1500 else 99.0,
            "total_price": round(sub + (0.0 if sub > 1500 else 99.0), 2),
            "currency": "INR",
            "line_items": lines,
            "_real_created_at": real_dt,
        })

    # ---- native orders (post-cutover) ----
    native_number = 13271
    for k in range(6200):
        oid += 1
        cust = rnd.choice(buyers)
        dt = CUTOVER + timedelta(days=rnd.randint(0, (NOW - CUTOVER).days),
                                 seconds=rnd.randint(0, 86399))
        lines = make_lines(rnd)
        sub = sum(l["price"] * l["quantity"] - l["discount"] for l in lines)
        pay = rnd.choice(PAY_VARIANTS)
        is_cod = pay.strip().lower() in COD_STRINGS
        cancelled = rnd.random() < 0.15
        if cancelled:
            fin = "voided"
        elif is_cod:
            # DEFECT 4: COD orders are left at "pending" and nobody updates them.
            fin = rnd.choices(["pending", "paid"], weights=[86, 14])[0]
        else:
            fin = rnd.choices(["paid", "refunded"], weights=[96, 4])[0]
        orders.append({
            "id": oid,
            # DEFECT 2: native numbering overlaps the legacy range.
            "order_number": native_number,
            "created_at": dt.isoformat(),
            "processed_at": dt.isoformat(),
            "updated_at": (dt + timedelta(days=rnd.randint(0, 30))).isoformat(),
            "note": "",
            "tags": "",
            "customer_id": cust,
            "financial_status": fin,
            "cancelled_at": (dt + timedelta(hours=rnd.randint(1, 72))).isoformat() if cancelled else None,
            "payment_method": pay,
            "subtotal_price": round(sub, 2),
            "shipping_price": 0.0 if sub > 1500 else 99.0,
            "total_price": round(sub + (0.0 if sub > 1500 else 99.0), 2),
            "currency": "INR",
            "line_items": lines,
            "_real_created_at": dt,
        })
        native_number += 1

    # ---- DEFECT 3: exchanges recorded as refunded/paid twins ----
    exchange_sources = rnd.sample([o for o in orders if o["id"] > 900002600
                                   and o["cancelled_at"] is None], 240)
    for src in exchange_sources:
        oid += 1
        base = datetime.fromisoformat(src["created_at"])
        twin = dict(src)
        twin["id"] = oid
        twin["order_number"] = native_number; native_number += 1
        twin["created_at"] = (base + timedelta(seconds=rnd.randint(4, 40))).isoformat()
        twin["processed_at"] = twin["created_at"]
        twin["updated_at"] = twin["created_at"]
        twin["note"] = f"EXC_{src['order_number']} size exchange"
        twin["tags"] = "exchange"
        twin["line_items"] = [dict(l) for l in src["line_items"]]
        for l in twin["line_items"]:            # same product, different size
            l["sku"] = l["sku"][:-1] + rnd.choice(SIZES)
        src["financial_status"] = "refunded"
        twin["financial_status"] = "paid"
        twin["_real_created_at"] = datetime.fromisoformat(twin["created_at"])
        orders.append(twin)

    orders.sort(key=lambda o: o["id"])

    # ---- rollups on customers (buyers only) ----
    for o in orders:
        if o["cancelled_at"]:
            continue
        c = customers[o["customer_id"]]
        c["orders_count"] += 1
        c["total_spent"] = round(c["total_spent"] + o["total_price"], 2)

    # ---- DEFECT 5: some buyers exist twice, split across email and phone ----
    dupes = rnd.sample([c for c in customers.values() if c["orders_count"] > 1], 300)
    for c in dupes:
        new_id = 7000000 + c["id"] % 100000
        if new_id in customers:
            continue
        shadow = dict(c)
        shadow["id"] = new_id
        # one record keeps the email, the other keeps the phone
        if c["email"] and c["phone"]:
            shadow["email"] = None
            shadow["phone"] = c["phone"]
            c["phone"] = None
        shadow["orders_count"] = 0
        shadow["total_spent"] = 0.0
        customers[new_id] = shadow

    # ---- event feed (the "webhook" stream) ----
    events, eid = [], 1
    recent = [o for o in orders if o["_real_created_at"] > NOW - timedelta(days=300)]
    for o in recent:
        eid += 1
        events.append({
            "id": f"evt_{eid}",
            "topic": "orders/updated",
            "triggered_at": (o["_real_created_at"] + timedelta(minutes=rnd.randint(1, 400))).isoformat(),
            "order_id": o["id"],
            "financial_status": o["financial_status"],
        })
    # 15% duplicates, re-delivered
    for e in rnd.sample(events, max(1, len(events) // 7)):
        events.append(dict(e))
    # deliberately shuffled: arrival order is not triggered_at order
    rnd.shuffle(events)

    # ---- DEFECT 6: 0.4% of orders never appear in the paginated list ----
    hidden = set(rnd.sample([o["id"] for o in orders], max(1, len(orders) // 250)))

    for o in orders:
        o.pop("_real_created_at", None)

    return orders, customers, events, hidden


ORDERS, CUSTOMERS, EVENTS, HIDDEN = build_dataset()
ORDERS_BY_ID = {o["id"]: o for o in ORDERS}
LISTED = [o for o in ORDERS if o["id"] not in HIDDEN]

# ---------------------------------------------------------------- runtime state

_lock = threading.Lock()
TOKENS = {}                      # token -> expiry epoch
BUCKET = {"tokens": float(BUCKET_CAPACITY), "ts": time.time()}
BULK_JOBS = {}                   # job_id -> dict
_req_counter = {"n": 0}


def take_token():
    """Leaky bucket. Returns (allowed, remaining, retry_after_or_None)."""
    with _lock:
        now = time.time()
        BUCKET["tokens"] = min(BUCKET_CAPACITY,
                               BUCKET["tokens"] + (now - BUCKET["ts"]) * BUCKET_REFILL)
        BUCKET["ts"] = now
        if BUCKET["tokens"] >= 1:
            BUCKET["tokens"] -= 1
            return True, int(BUCKET["tokens"]), None
        wait = (1 - BUCKET["tokens"]) / BUCKET_REFILL
        return False, 0, round(wait, 2)


def next_req_n():
    with _lock:
        _req_counter["n"] += 1
        return _req_counter["n"]


def serialise_order(o, strip_email):
    d = {k: v for k, v in o.items()}
    c = CUSTOMERS[o["customer_id"]]
    d["customer"] = {
        "id": c["id"],
        "email": None if strip_email else c["email"],
        "phone": c["phone"],
        "first_name": c["first_name"], "last_name": c["last_name"],
    }
    d["shipping_address"] = {"city": c["city"], "province": c["state"], "zip": c["zip"]}
    return d


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # quiet

    # ---- helpers
    def send_json(self, code, body, extra_headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, code, text, ctype="text/plain"):
        raw = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def auth_ok(self):
        h = self.headers.get("Authorization", "")
        m = re.match(r"Bearer\s+(\S+)", h)
        if not m:
            self.send_json(401, {"errors": "missing bearer token"})
            return False
        tok = m.group(1)
        exp = TOKENS.get(tok)
        if exp is None:
            self.send_json(401, {"errors": "unknown token"})
            return False
        if time.time() > exp:
            self.send_json(401, {"errors": "token expired"})
            return False
        return True

    def rate_ok(self):
        allowed, remaining, retry = take_token()
        if allowed:
            return True
        n = next_req_n()
        headers = {"X-Api-Call-Limit": f"{BUCKET_CAPACITY}/{BUCKET_CAPACITY}"}
        # Two thirds of the time we tell you how long to wait. The rest of the
        # time you are on your own.
        if n % 3 != 0:
            headers["Retry-After"] = str(max(1, int(retry) + 1))
        self.send_json(429, {"errors": "Too Many Requests"}, headers)
        return False

    # ---- routing
    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/oauth/token":
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln) if ln else b"{}"
            try:
                data = json.loads(body or b"{}")
            except Exception:
                data = {}
            if data.get("client_id") != CLIENT_ID or data.get("client_secret") != CLIENT_SECRET:
                return self.send_json(401, {"errors": "bad credentials"})
            tok = hashlib.sha1(f"{time.time()}{random.random()}".encode()).hexdigest()
            TOKENS[tok] = time.time() + TOKEN_TTL
            return self.send_json(200, {"access_token": tok, "expires_in": TOKEN_TTL,
                                        "token_type": "Bearer"})

        if not self.auth_ok() or not self.rate_ok():
            return

        if p.path == "/admin/bulk":
            job_id = "bulk_" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:10]
            n = next_req_n()
            BULK_JOBS[job_id] = {
                "id": job_id, "created": time.time(),
                "duration": 12 + (n % 9) * 2,
                # One in four bulk jobs fails partway and has to be re-issued.
                "will_fail": (n % 4 == 0),
                "status": "CREATED",
            }
            return self.send_json(200, {"bulk_operation": {"id": job_id, "status": "CREATED"}})

        return self.send_json(404, {"errors": "not found"})

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)

        if p.path == "/":
            return self.send_text(200, __doc__ or "mock commerce api")

        if not self.auth_ok() or not self.rate_ok():
            return

        # ---------------- paginated orders
        if p.path == "/admin/orders":
            limit = min(int(q.get("limit", ["50"])[0]), 250)
            cursor = int(q.get("page_info", ["0"])[0])
            n = next_req_n()

            page = LISTED[cursor:cursor + limit]
            # 1 in 12 pages repeats its last two records at the top of the next
            overlap = []
            if cursor > 0 and (cursor // max(limit, 1)) % 12 == 3:
                overlap = LISTED[max(0, cursor - 2):cursor]
            payload_orders = overlap + page

            # 1 in 14 responses is HTTP 200 with the emails silently nulled and
            # the reason buried in the body.
            strip = (n % 14 == 0)
            body = {"orders": [serialise_order(o, strip) for o in payload_orders]}
            if strip:
                body["errors"] = ("customer_email omitted: app is not approved for "
                                  "protected customer data")

            headers = {"X-Api-Call-Limit": f"{BUCKET_CAPACITY - int(BUCKET['tokens'])}/{BUCKET_CAPACITY}"}
            nxt = cursor + limit
            if nxt < len(LISTED):
                headers["Link"] = f'</admin/orders?limit={limit}&page_info={nxt}>; rel="next"'
            return self.send_json(200, body, headers)

        # ---------------- single order (the only way to reach hidden ones)
        m = re.match(r"^/admin/orders/(\d+)$", p.path)
        if m:
            o = ORDERS_BY_ID.get(int(m.group(1)))
            if not o:
                return self.send_json(404, {"errors": "not found"})
            return self.send_json(200, {"order": serialise_order(o, False)})

        # ---------------- orders changed since a timestamp (includes hidden)
        if p.path == "/admin/orders/since":
            since = q.get("updated_at_min", [None])[0]
            if not since:
                return self.send_json(400, {"errors": "updated_at_min required"})
            try:
                cut = datetime.fromisoformat(since)
            except ValueError:
                return self.send_json(400, {"errors": "bad timestamp"})
            out = [o for o in ORDERS
                   if datetime.fromisoformat(o["updated_at"]) >= cut][:250]
            return self.send_json(200, {"orders": [serialise_order(o, False) for o in out]})

        # ---------------- customers
        if p.path == "/admin/customers":
            limit = min(int(q.get("limit", ["50"])[0]), 250)
            cursor = int(q.get("page_info", ["0"])[0])
            allc = list(CUSTOMERS.values())
            page = allc[cursor:cursor + limit]
            headers = {}
            nxt = cursor + limit
            if nxt < len(allc):
                headers["Link"] = f'</admin/customers?limit={limit}&page_info={nxt}>; rel="next"'
            return self.send_json(200, {"customers": page}, headers)

        # ---------------- event feed (stands in for webhooks)
        if p.path == "/admin/events":
            limit = min(int(q.get("limit", ["100"])[0]), 250)
            cursor = int(q.get("cursor", ["0"])[0])
            page = EVENTS[cursor:cursor + limit]
            headers = {}
            nxt = cursor + limit
            if nxt < len(EVENTS):
                headers["Link"] = f'</admin/events?limit={limit}&cursor={nxt}>; rel="next"'
            return self.send_json(200, {"events": page}, headers)

        # ---------------- bulk job status
        m = re.match(r"^/admin/bulk/([a-z0-9_]+)$", p.path)
        if m:
            job = BULK_JOBS.get(m.group(1))
            if not job:
                return self.send_json(404, {"errors": "no such job"})
            age = time.time() - job["created"]
            if age < 3:
                st = "CREATED"
            elif age < job["duration"]:
                st = "RUNNING"
            elif job["will_fail"]:
                st = "FAILED"
            else:
                st = "COMPLETED"
            job["status"] = st
            body = {"bulk_operation": {"id": job["id"], "status": st}}
            if st == "COMPLETED":
                body["bulk_operation"]["url"] = f"/admin/bulk/{job['id']}/download"
            if st == "FAILED":
                body["bulk_operation"]["error_code"] = "INTERNAL_SERVER_ERROR"
            return self.send_json(200, body)

        # ---------------- bulk download (JSONL, parent/child, unordered)
        m = re.match(r"^/admin/bulk/([a-z0-9_]+)/download$", p.path)
        if m:
            job = BULK_JOBS.get(m.group(1))
            if not job or job["status"] != "COMPLETED":
                return self.send_json(409, {"errors": "job not completed"})
            lines = []
            for o in ORDERS:
                lines.append(json.dumps({
                    "id": f"gid://order/{o['id']}",
                    "order_number": o["order_number"],
                    "created_at": o["created_at"],
                    "note": o["note"],
                    "financial_status": o["financial_status"],
                    "cancelled_at": o["cancelled_at"],
                    "payment_method": o["payment_method"],
                    "total_price": o["total_price"],
                    "customer_id": o["customer_id"],
                }))
                for i, l in enumerate(o["line_items"]):
                    lines.append(json.dumps({
                        "id": f"gid://lineitem/{o['id']}-{i}",
                        "__parentId": f"gid://order/{o['id']}",
                        "sku": l["sku"], "name": l["name"],
                        "quantity": l["quantity"], "price": l["price"],
                        "discount": l["discount"],
                        "compare_at_price": l["compare_at_price"],
                    }))
            # children do not follow their parents in the file
            rnd = random.Random(SEED + 1)
            rnd.shuffle(lines)
            return self.send_text(200, "\n".join(lines), "application/x-ndjson")

        # ---------------- ground truth, for self-checking
        if p.path == "/admin/_truth":
            live = [o for o in ORDERS if o["cancelled_at"] is None]
            exch = [o for o in ORDERS if o["tags"] == "exchange"]
            return self.send_json(200, {
                "note": "Use this to check your own work. Reaching these numbers "
                        "reliably is the exercise; reading them off is not.",
                "orders_total_all_time": len(ORDERS),
                "orders_reachable_by_listing": len(LISTED),
                "orders_cancelled": len(ORDERS) - len(live),
                "customer_records": len(CUSTOMERS),
                "exchange_pairs": len(exch),
                "hint": "Two further figures — distinct real people who have "
                        "bought, and 2023-onward net revenue with exchanges "
                        "counted once — are for you to derive and defend.",
            })

        return self.send_json(404, {"errors": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    print(f"Mock Commerce API on http://localhost:{a.port}")
    print(f"  {len(ORDERS)} orders · {len(CUSTOMERS)} customer records · {len(EVENTS)} events")
    print(f"  client_id={CLIENT_ID}  client_secret={CLIENT_SECRET}")
    print("  POST /oauth/token to begin.  Ctrl-C to stop.")
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
