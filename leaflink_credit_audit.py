"""
LeafLink credits / balance audit  (read-only, one-shot)
--------------------------------------------------------
Answers three questions before we build anything on the dashboards:

  1. Do any orders have non-zero `credits`?          -> credits table
  2. Do any orders have non-zero `payment_balance`?  -> AR balance columns
  3. What are the real magnitudes / examples?        -> sanity checks

It reuses the SAME environment variables and auth as your scraper, so if the
scraper runs, this runs. It does NOT write any dashboard data — it only prints
a report. Nothing is changed.

RUN IT THE SAME WAY YOU RUN THE SCRAPER, e.g.:
    LEAFLINK_API_KEY=xxxx python leaflink_credit_audit.py

Then paste the whole printed report back.
"""

import os
import sys
import time
import json
from collections import Counter

import requests

# --- mirror the scraper's config (same env vars) ----------------------------
API_BASE   = os.getenv("LEAFLINK_API_BASE", "https://www.leaflink.com")
ENDPOINT   = os.getenv("LEAFLINK_ENDPOINT", "/api/v2/orders-received/")
API_KEY    = os.getenv("LEAFLINK_API_KEY", "")
SELLER_ID  = os.getenv("LEAFLINK_SELLER_ID", "9105")
FROM_DATE  = os.getenv("LEAFLINK_FROM_DATE", "2025-01-01")
PAGE_SIZE  = int(os.getenv("LEAFLINK_PAGE_SIZE", "500"))
MAX_PAGES  = int(os.getenv("LEAFLINK_MAX_PAGES", "0"))  # 0 = all
SERVER_DATE_FILTER = os.getenv("LEAFLINK_SERVER_DATE_FILTER", "1") != "0"


def auth_headers() -> dict:
    return {
        "Authorization": f"App {API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "chill-credit-audit",
    }


def _get(url, params):
    last = None
    for attempt in range(6):
        try:
            resp = requests.get(url, headers=auth_headers(), params=params, timeout=120)
        except requests.RequestException as e:
            last = e
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            wait = 5 * (attempt + 1)
            print(f"  {resp.status_code} — backing off {wait}s")
            time.sleep(wait)
            last = resp
            continue
        return resp
    raise RuntimeError(f"request failed after retries: {last}")


def _num(v):
    """LeafLink money can be a bare number or {'amount': n, 'currency': 'USD'}."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_all_orders():
    if not API_KEY:
        print("ERROR: LEAFLINK_API_KEY not set. Run this the same way you run the scraper.")
        sys.exit(1)

    url = API_BASE.rstrip("/") + ENDPOINT
    orders = []
    page = 1
    while True:
        params = {"page_size": PAGE_SIZE, "page": page}
        if SELLER_ID:
            params["seller"] = SELLER_ID
        if SERVER_DATE_FILTER and FROM_DATE:
            # same filter key the scraper relies on
            params["created_on__gte"] = FROM_DATE
        resp = _get(url, params)
        if resp.status_code == 403:
            print("ERROR: 403 Forbidden — the App token lacks Orders read permission.")
            sys.exit(1)
        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code} on page {page}: {resp.text[:300]}")
            sys.exit(1)
        data = resp.json()
        batch = data.get("results", data if isinstance(data, list) else [])
        if not batch:
            break
        orders.extend(batch)
        print(f"  page {page}: {len(batch)} orders (total {len(orders)})")
        page += 1
        if MAX_PAGES and page > MAX_PAGES:
            break
        if not data.get("next"):
            break
    return orders


def main():
    print(f"Fetching orders from {ENDPOINT} (seller {SELLER_ID}, from {FROM_DATE})...")
    orders = fetch_all_orders()
    n = len(orders)
    print(f"\nFetched {n} orders.\n")
    if not n:
        print("No orders returned — nothing to audit.")
        return

    # --- confirm the fields exist on the raw object -------------------------
    sample_keys = sorted(orders[0].keys())
    money_like = [k for k in sample_keys
                  if any(w in k.lower() for w in ("credit", "balance", "paid", "total", "amount", "payment", "refund", "discount"))]
    print("=== money / payment / credit-ish fields present on an order ===")
    for k in money_like:
        print(f"  {k!r} = {orders[0].get(k)!r}")
    print()

    # --- credits -------------------------------------------------------------
    credits = [_num(o.get("credits")) for o in orders]
    nonzero_credit = [(o, c) for o, c in zip(orders, credits) if abs(c) > 1e-9]
    print("=== CREDITS (field: 'credits') ===")
    print(f"  orders with non-zero credits : {len(nonzero_credit)} of {n}")
    print(f"  total credit dollars         : ${sum(credits):,.2f}")
    if nonzero_credit:
        print("  examples (up to 10):")
        for o, c in sorted(nonzero_credit, key=lambda x: -abs(x[1]))[:10]:
            cust = (o.get("customer") or {}).get("display_name", "?")
            print(f"    {o.get('short_id','?'):>10}  ${c:>12,.2f}  {cust[:32]}  paid={o.get('paid')}")
    print()

    # --- payment_balance (outstanding / remaining) --------------------------
    bal = [_num(o.get("payment_balance")) for o in orders]
    nonzero_bal = [(o, b) for o, b in zip(orders, bal) if abs(b) > 1e-9]
    print("=== PAYMENT BALANCE (field: 'payment_balance') ===")
    print(f"  orders with non-zero balance : {len(nonzero_bal)} of {n}")
    print(f"  total outstanding dollars    : ${sum(bal):,.2f}")
    if nonzero_bal:
        print("  examples (up to 10):")
        for o, b in sorted(nonzero_bal, key=lambda x: -abs(x[1]))[:10]:
            cust = (o.get("customer") or {}).get("display_name", "?")
            tot = _num(o.get("total"))
            print(f"    {o.get('short_id','?'):>10}  bal ${b:>12,.2f}  of total ${tot:>12,.2f}  paid={o.get('paid')}  status={o.get('payment_status')}")
    print()

    # --- does balance ever DIFFER from all-or-nothing? ----------------------
    # (partial payments = balance strictly between 0 and total)
    partial = []
    for o in orders:
        b = _num(o.get("payment_balance")); t = _num(o.get("total"))
        if t > 0 and 1e-9 < b < t - 1e-9:
            partial.append((o, b, t))
    print("=== PARTIAL PAYMENTS (balance strictly between 0 and total) ===")
    print(f"  partially-paid orders : {len(partial)} of {n}")
    if partial:
        print("  -> partial balances are REAL; AR 'payments applied' will be meaningful.")
        for o, b, t in sorted(partial, key=lambda x: -x[2])[:8]:
            print(f"    {o.get('short_id','?'):>10}  paid ${t-b:>11,.2f} of ${t:>11,.2f}  (bal ${b:,.2f})")
    else:
        print("  -> no partial payments; balance is all-or-nothing (paid vs full).")
    print()

    print("=== payment_status distribution ===")
    for k, v in Counter(o.get("payment_status") for o in orders).most_common():
        print(f"  {k!r}: {v}")
    print("\n--- end audit ---")


if __name__ == "__main__":
    main()
