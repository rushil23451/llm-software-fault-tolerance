"""
app_nvp.py — Flask server for the LA-NVP billing PoC (pure NVP).

Companion to app.py (the Recovery-Block demo). Same domain (cart -> discount
-> committed bill), same three SQLite databases (products.db / billing.db /
history.db), same durable layer (durable.py) — the only thing that changes
is the fault-tolerance mechanism: RB is replaced by pure N-Version
Programming as described in the paper's §3.1.

Because NVP is a FORWARD error-recovery scheme, the voter arbitrates BEFORE
any bill is written. The corrupt output of a CCF-poisoned variant NEVER
reaches billing.db. The happy-path event log is just:

    CHECKPOINT -> WRITE (voter verdict) -> COMMIT

The durable layer's rollback() is used ONLY in the exceptional no-consensus
case, where it just closes an empty transaction (nothing was ever written,
so nothing is actually undone). This is precisely what the NVP literature
means when it calls NVP "forward recovery, not backward recovery".

Endpoints mirror app.py 1:1 so the two demos are directly comparable.
Runs on port 8081 (RB demo owns 8080) so both can run side-by-side.

/checkout drives:
  1. checkpoint                    (BEGIN txn on billing.db)
  2. run all 3 variants in parallel  (Python + C++ + Java, isolated sandboxes)
  3. optionally poison one variant's output  (CCF injection, post-execution)
  4. voter arbitrates              (LLM voter -> hardcoded majority fallback)
  5a. consensus  -> WRITE(voter's verdict) -> COMMIT
  5b. no consensus -> VOTE_ABORT -> ROLLBACK (empty txn) -> abort

Run:
  python db_setup.py      # once
  python app_nvp.py
  open http://localhost:8081
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import nvp_engine
from durable import DurableWorkflow

HERE         = Path(__file__).parent
PRODUCTS_DB  = HERE / "products.db"
BILLING_DB   = HERE / "billing.db"
HISTORY_DB   = HERE / "history.db"

app = Flask(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _query(db_path: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _extract_total(result: dict | None) -> float | None:
    """Pull the scalar `total` out of the voter's dict verdict."""
    if not isinstance(result, dict):
        return None
    v = result.get("total")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


# ─────────────────────────────────────────────────────────────────────────────
# routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index_nvp.html")


@app.route("/products")
def products():
    return jsonify(_query(
        PRODUCTS_DB,
        "SELECT id, name, unit_price, stock FROM products ORDER BY id"))


@app.route("/billing")
def billing():
    bills = _query(
        BILLING_DB,
        "SELECT id, items_json, subtotal, discount, total, status, version, "
        "created_at FROM bills ORDER BY id")
    history = _query(
        HISTORY_DB,
        "SELECT workflow_id, seq, event_type, detail, ts FROM durable_log "
        "ORDER BY id DESC LIMIT 60")
    bad_rows = _query(
        BILLING_DB, "SELECT COUNT(*) AS n FROM bills WHERE total IS NULL")[0]["n"]
    return jsonify({"bills": bills, "history": history, "bad_rows": bad_rows})


@app.route("/reset", methods=["POST"])
def reset():
    import db_setup
    db_setup.setup_products()
    db_setup.setup_billing()
    db_setup.setup_history()
    return jsonify({"ok": True})


@app.route("/checkout", methods=["POST"])
def checkout():
    payload    = request.get_json(force=True)
    cart       = payload.get("items", [])
    discount   = float(payload.get("discount", 0.0))
    inject_ccf = bool(payload.get("inject_ccf", True))
    timeout    = int(payload.get("timeout", 30))

    if not cart:
        return jsonify({"error": "Cart is empty."}), 400
    if not (0.0 <= discount < 1.0):
        return jsonify({"error": "Discount must be in [0, 1)."}), 400

    # ── 1. Read prices from products.db (read-only) and build subtotal ──
    catalogue = {p["id"]: p for p in _query(
        PRODUCTS_DB, "SELECT id, name, unit_price FROM products")}
    line_items = []
    subtotal = 0.0
    for entry in cart:
        pid = int(entry["id"])
        qty = int(entry.get("qty", 1))
        if pid not in catalogue:
            return jsonify({"error": f"Unknown product id {pid}"}), 400
        price = catalogue[pid]["unit_price"]
        line_total = round(price * qty, 2)
        subtotal += line_total
        line_items.append({
            "id": pid, "name": catalogue[pid]["name"],
            "qty": qty, "unit_price": price, "line_total": line_total})
    subtotal = round(subtotal, 2)
    items_json = json.dumps(line_items)

    # ── 2. Run the NVP pipeline (§3.1). No DB writes happen inside. ──
    nvp = nvp_engine.run_with_nvp(
        (subtotal, discount),
        timeout=timeout,
        inject_ccf=inject_ccf,
    )

    voter_total = _extract_total(nvp["voter_verdict"])

    # ── 3. Drive the durable workflow. ───────────────────────────────────
    #        Happy path: checkpoint -> WRITE(voter verdict) -> COMMIT.
    #        No-consensus: checkpoint -> VOTE_ABORT -> ROLLBACK (empty txn).
    wf = DurableWorkflow()
    try:
        wf.checkpoint(
            f"pre-billing snapshot (subtotal={subtotal}, discount={discount})")

        if nvp["success"]:
            wf.provisional_write_bill(
                items_json, subtotal, discount,
                total=voter_total, status="COMMITTED",
                version=nvp["final_version"])
            wf.commit(
                f"voter ({nvp['voter_used']}) reached consensus — bill "
                f"committed with total={voter_total}")
            final = {
                "total":       voter_total,
                "version":     nvp["final_version"],
                "masked":      nvp["poisoned_lang"] is not None,
                "masked_lang": nvp["poisoned_lang"],
            }
        else:
            wf.log_event(
                "VOTE_ABORT",
                "voter reached no consensus (fewer than 2 non-crashed "
                "variants agreed) — no bill can be written.")
            wf.rollback(
                "no NVP consensus — transaction closed cleanly "
                "(no rows were ever provisionally written)")
            wf.log_event("ABORT", "all variants exhausted — no bill written")
            final = {"total": None, "version": "none", "masked": False,
                     "masked_lang": None}

        db_events   = wf.trace
        workflow_id = wf.workflow_id
    finally:
        wf.close()

    # ── 4. Read back billing.db to prove it is clean ─────────────────────
    bills = _query(
        BILLING_DB,
        "SELECT id, subtotal, discount, total, status, version, created_at "
        "FROM bills ORDER BY id")
    bad_rows = _query(
        BILLING_DB, "SELECT COUNT(*) AS n FROM bills WHERE total IS NULL")[0]["n"]

    return jsonify({
        "workflow_id":     workflow_id,
        "subtotal":        subtotal,
        "discount":        discount,
        "line_items":      line_items,
        "nvp_steps":       nvp["steps"],       # VARIANT_* × 3, VOTE
        "durable_events":  db_events,          # CHECKPOINT, WRITE, COMMIT (happy path)
        "variant_outputs": nvp["variant_outputs"],
        "voter_used":      nvp["voter_used"],
        "voter_verdict":   nvp["voter_verdict"],
        "poisoned_lang":   nvp["poisoned_lang"],
        "final":           final,
        "bills":           bills,
        "bad_rows":        bad_rows,
        "success":         nvp["success"],
        "inject_ccf":      inject_ccf,
    })


if __name__ == "__main__":
    if not PRODUCTS_DB.exists() or not BILLING_DB.exists() or not HISTORY_DB.exists():
        print("[APP-NVP] Databases missing — running db_setup first...")
        import db_setup
        db_setup.setup_products()
        db_setup.setup_billing()
        db_setup.setup_history()
    print("=" * 64)
    print("LA-NVP Billing PoC (pure NVP)  —  http://localhost:8081")
    print("3 diverse variants (Py/C++/Java) -> voter masks CCF -> single COMMIT.")
    print("=" * 64)
    app.run(debug=False, port=8081)
