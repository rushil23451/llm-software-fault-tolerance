"""
app.py — Flask server for the LA-RBS billing PoC.

Endpoints:
  GET  /              -> the UI (cart + discount + recovery trace + DB viewer)
  GET  /products      -> catalogue from products.db (read-only)
  POST /checkout      -> run the durable recovery-block workflow, return trace
  GET  /billing       -> current committed bills + the durable event history
  POST /reset         -> re-seed all databases

The /checkout route is the heart of the demo. It performs the
write -> detect -> rollback -> recover sequence:

  1. checkpoint                    (durable layer captures pre-fault state)
  2. run FAULTY primary            -> None / garbage total
  3. provisional WRITE of the bad bill into billing.db   (the damage)
  4. acceptance test REJECTS it
  5. ROLLBACK                      (bad row erased — billing.db clean again)
  6. run algorithmically-diverse ALTERNATE -> correct total
  7. acceptance test ACCEPTS it
  8. provisional WRITE + COMMIT    (exactly one good row persists)

Run:
  python db_setup.py      # once
  python app.py
  open http://localhost:8080
"""

import json
import sqlite3
from pathlib import Path

from flask import Flask, request, jsonify, render_template

import recovery_engine
from durable import DurableWorkflow

HERE = Path(__file__).parent
PRODUCTS_DB = HERE / "products.db"
BILLING_DB = HERE / "billing.db"
HISTORY_DB = HERE / "history.db"

app = Flask(__name__)


# ── helpers ──────────────────────────────────────────────────────────
def _query(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── routes ───────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


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
    # count any corrupt rows (NULL total) — should always be 0
    bad_rows = _query(
        BILLING_DB, "SELECT COUNT(*) AS n FROM bills WHERE total IS NULL")[0]["n"]
    return jsonify({"bills": bills, "history": history, "bad_rows": bad_rows})


@app.route("/reset", methods=["POST"])
def reset():
    import db_setup
    db_setup.setup_products()
    db_setup.setup_billing()
    db_setup.setup_history()
    recovery_engine.reset_to_faulty()
    return jsonify({"ok": True})


@app.route("/checkout", methods=["POST"])
def checkout():
    payload = request.get_json(force=True)
    cart = payload.get("items", [])            # [{id, qty}, ...]
    discount = float(payload.get("discount", 0.0))

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

    # Always start the demo from the faulty primary so recovery is visible.
    recovery_engine.reset_to_faulty()

    # ── 2. Compute the correct result + full RB trace (no DB writes yet) ─
    rb = recovery_engine.run_with_recovery((subtotal, discount))

    # ── 3. Drive the durable workflow: write -> detect -> rollback -> recover ─
    wf = DurableWorkflow()
    db_events = []
    try:
        wf.checkpoint(
            f"pre-billing snapshot (subtotal={subtotal}, discount={discount})")

        # The faulty primary ran first. Persist its (bad) result provisionally —
        # this is the corrupt write we must be able to undo.
        primary_result = rb["primary_result"]
        primary_ok = rb["steps"][0]["at_pass"]

        wf.provisional_write_bill(
            items_json, subtotal, discount,
            total=primary_result, status="PENDING",
            version="primary (faulty)")

        if primary_ok:
            # (Not the demo path, but handle it: primary was fine.)
            wf.commit("primary result accepted — committed")
            final = {"total": primary_result, "version": "primary",
                     "recovered": False}
        else:
            wf.log_event(
                "AT_REJECT",
                f"acceptance test rejected primary total={primary_result!r} "
                f"— corrupt bill must not persist")
            wf.rollback("faulty primary produced invalid total")

            if rb["success"]:
                good_total = rb["final_result"]
                wf.provisional_write_bill(
                    items_json, subtotal, discount,
                    total=good_total, status="COMMITTED",
                    version=rb["final_version"])
                wf.commit(
                    f"recovered via {rb['final_version']} — correct bill "
                    f"total={good_total} committed")
                final = {"total": good_total, "version": rb["final_version"],
                         "recovered": True}
            else:
                wf.log_event(
                    "ABORT", "all alternates exhausted — no bill written")
                final = {"total": None, "version": "none", "recovered": False}

        db_events = wf.trace
        workflow_id = wf.workflow_id
    finally:
        wf.close()

    # ── 4. Read back billing.db to prove it is clean ────────────────────
    bills = _query(
        BILLING_DB,
        "SELECT id, subtotal, discount, total, status, version, created_at "
        "FROM bills ORDER BY id")
    bad_rows = _query(
        BILLING_DB, "SELECT COUNT(*) AS n FROM bills WHERE total IS NULL")[0]["n"]

    return jsonify({
        "workflow_id": workflow_id,
        "subtotal": subtotal,
        "discount": discount,
        "line_items": line_items,
        "rb_steps": rb["steps"],       # PRIMARY / ALT-1 / ... with AT verdicts
        "durable_events": db_events,   # CHECKPOINT / WRITE / AT_REJECT / ...
        "final": final,
        "bills": bills,
        "bad_rows": bad_rows,
        "success": rb["success"],
    })


if __name__ == "__main__":
    if not PRODUCTS_DB.exists() or not BILLING_DB.exists() or not HISTORY_DB.exists():
        print("[APP] Databases missing — running db_setup first...")
        import db_setup
        db_setup.setup_products()
        db_setup.setup_billing()
        db_setup.setup_history()
    print("=" * 64)
    print("LA-RBS Billing PoC  —  http://localhost:8080")
    print("Faulty discount code -> corrupt write -> rollback -> recover.")
    print("=" * 64)
    app.run(debug=False, port=8080)
