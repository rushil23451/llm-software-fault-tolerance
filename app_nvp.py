"""
app_nvp.py — Flask server for the LA-NVP billing PoC.

Companion to app.py (the Recovery-Block demo). Same domain (cart -> discount
-> committed bill), same three SQLite databases (products.db / billing.db /
history.db), same durable layer (durable.py) — the only thing that changes
is the fault-tolerance mechanism: RB is replaced by NVP, wired in Shape B
(optimistic-first-write NVP with vote-driven compensation).

Endpoints (mirror the RB app 1:1 so the two demos are directly comparable):
  GET  /              -> the UI (cart + discount + NVP trace + DB viewer)
  GET  /products      -> catalogue from products.db (read-only)
  POST /checkout      -> run the durable NVP workflow, return trace
  GET  /billing       -> current committed bills + durable event history
  POST /reset         -> re-seed all databases

The /checkout route executes Shape B:

  1. checkpoint                       (durable layer captures pre-fault state)
  2. run all 3 variants in parallel   (Python + C++ + Java)
  3. designate the first-to-return as the "first-writer"
     (its output is DELIBERATELY POISONED for the demo — the NVP analogue of
      RB's FAULTY_PRIMARY_SRC)
  4. provisional WRITE of that (bad) result into billing.db      (the damage)
  5. voter runs across all 3 variant outputs (LLM voter -> hardcoded fallback)
  6. voter overrules the poisoned first-writer
  7. ROLLBACK                         (bad row erased — billing.db clean again)
  8. provisional WRITE + COMMIT       (voter's answer persists)

If the voter agrees with the first-writer (e.g. `force_poison_first=False`),
step 6-8 collapse to a single COMMIT with no rollback.

Runs on port 8081 (RB demo owns 8080) so both can run side-by-side.

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
    """Pull the scalar `total` out of a variant's JSON output. None if absent."""
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
    payload = request.get_json(force=True)
    cart          = payload.get("items", [])
    discount      = float(payload.get("discount", 0.0))
    inject_ccf    = bool(payload.get("inject_ccf", True))  # demo default: on
    timeout       = int(payload.get("timeout", 30))

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

    # ── 2. Run the NVP pipeline (Shape B). No DB writes happen inside. ──
    nvp = nvp_engine.run_with_nvp(
        (subtotal, discount),
        timeout=timeout,
        force_poison_first=inject_ccf,
    )

    first_result_dict = nvp["first_result"]
    voter_verdict     = nvp["voter_verdict"]
    first_total       = _extract_total(first_result_dict)
    voter_total       = _extract_total(voter_verdict)

    # ── 3. Drive the durable workflow using the NVP trace ────────────────
    #        checkpoint -> provisional-write first -> (rollback + rewrite)? -> commit
    wf = DurableWorkflow()
    try:
        wf.checkpoint(
            f"pre-billing snapshot (subtotal={subtotal}, discount={discount})")

        wf.provisional_write_bill(
            items_json, subtotal, discount,
            total=first_total, status="PENDING",
            version=f"first-writer:{nvp['first_lang']}"
                    + (" (poisoned)" if inject_ccf and first_result_dict is not None else ""),
        )

        if not nvp["success"]:
            # No consensus across variants — abort like RB does when all alts fail.
            wf.log_event(
                "VOTE_ABORT",
                "voter reached no consensus (fewer than 2 non-crashed "
                "variants agreed) — the workflow cannot commit a bill.")
            wf.rollback("no NVP consensus — provisional first-write undone")
            wf.log_event("ABORT", "all variants exhausted — no bill written")
            final = {"total": None, "version": "none", "recovered": False}

        elif nvp["overruled"]:
            # Voter disagrees with the first-writer. This is the demo's key arc:
            # the corrupt provisional write must be rolled back and replaced
            # with the voter's answer.
            wf.log_event(
                "VOTE_OVERRULE",
                f"voter ({nvp['voter_used']}) overrules first-writer "
                f"({nvp['first_lang']}): first_total={first_total!r} vs "
                f"voter_total={voter_total!r} — corrupt bill must not persist.")
            wf.rollback(f"voter overruled — {nvp['first_lang']} produced "
                        f"first_total={first_total!r}")

            wf.provisional_write_bill(
                items_json, subtotal, discount,
                total=voter_total, status="COMMITTED",
                version=nvp["final_version"])
            wf.commit(
                f"recovered via voter ({nvp['voter_used']}) — correct bill "
                f"total={voter_total} committed")
            final = {"total": voter_total,
                     "version": nvp["final_version"],
                     "recovered": True}

        else:
            # Voter agrees with the first-writer -> no compensation needed.
            wf.commit(
                f"voter ({nvp['voter_used']}) agrees with first-writer "
                f"({nvp['first_lang']}) — bill committed as-is")
            final = {"total": voter_total,
                     "version": nvp["final_version"],
                     "recovered": False}

        db_events   = wf.trace
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
        "workflow_id":    workflow_id,
        "subtotal":       subtotal,
        "discount":       discount,
        "line_items":     line_items,
        "nvp_steps":      nvp["steps"],       # FIRST_WRITE / VARIANT_* / VOTE / OVERRULE|AGREE
        "durable_events": db_events,          # CHECKPOINT / WRITE / VOTE_OVERRULE / ROLLBACK / COMMIT
        "variant_outputs": nvp["variant_outputs"],
        "first_lang":     nvp["first_lang"],
        "first_result":   nvp["first_result"],
        "voter_used":     nvp["voter_used"],
        "voter_verdict":  nvp["voter_verdict"],
        "overruled":      nvp["overruled"],
        "final":          final,
        "bills":          bills,
        "bad_rows":       bad_rows,
        "success":        nvp["success"],
        "inject_ccf":     inject_ccf,
    })


if __name__ == "__main__":
    if not PRODUCTS_DB.exists() or not BILLING_DB.exists() or not HISTORY_DB.exists():
        print("[APP-NVP] Databases missing — running db_setup first...")
        import db_setup
        db_setup.setup_products()
        db_setup.setup_billing()
        db_setup.setup_history()
    print("=" * 64)
    print("LA-NVP Billing PoC  —  http://localhost:8081")
    print("3 diverse variants (Py/C++/Java) -> first-write -> voter overrule -> rollback -> recover.")
    print("=" * 64)
    app.run(debug=False, port=8081)
