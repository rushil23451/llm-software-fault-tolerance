# LA-RBS Billing PoC — Checkpointed Recovery-Block Demo

A proof-of-concept for **LLM-Assisted Recovery Blocks with durable (checkpointed)
runtime recovery**. It shows a realistic, *stateful* pipeline — an order/billing
service backed by SQLite — where a **faulty discount function** would write a
**corrupt bill** to the database. A minimal durable-execution layer **rolls the
bad write back**, an **algorithmically-diverse alternate** (generated offline by
an LLM) is hot-patched in, its result is checked by an **acceptance test**, and
**only a correct bill is ever committed**.

This is the "one deep example" the paper is built around.

---

## The scenario (write → detect → rollback → recover)

```
1. CHECKPOINT              durable layer captures pre-fault state (BEGIN txn)
2. read items + prices     from products.db   (read-only)
3. run FAULTY primary      calculate_discounted_total() -> None  (planted bug)
4. WRITE bad bill          provisional row (total = NULL) into billing.db
5. ACCEPTANCE TEST         rejects the NULL total
6. ROLLBACK                bad row erased — billing.db is clean again
7. run ALTERNATE-1         diverse algorithm -> correct total
8. ACCEPTANCE TEST         accepts it
9. WRITE + COMMIT          exactly one correct bill persists
```

Result: `billing.db` ends with **one clean row and zero corrupt rows**, while
`history.db` preserves the full event trail proving a bad write happened and was
undone.

---

## Files

| File | Role |
|------|------|
| `design_time.py` | Runs once, offline. LLM generates primary + 2 alternates + acceptance test → `artifact_calculate_discounted_total.json`. **No LLM at runtime.** |
| `artifact_calculate_discounted_total.json` | Pre-generated artifact (ships here so the demo runs with **no API key**). |
| `db_setup.py` | Creates + seeds `products.db`, `billing.db`, `history.db`. |
| `durable.py` | The **durable-execution layer** — checkpoint / provisional write / rollback / commit on SQLite. The PoC stand-in for Temporal. |
| `recovery_engine.py` | Runtime Recovery-Block engine: loads the faulty primary, runs the acceptance test, iterates alternates, hot-patches the winner. |
| `app.py` | Flask server + REST endpoints; drives the durable workflow. |
| `templates/index.html` | The UI: cart, discount input, live recovery trace, event history, DB viewer. |

---

## Run it

```bash
cd larbs_billing_poc
pip install -r requirements.txt
python db_setup.py        # create + seed the databases (once)
python app.py             # starts on http://localhost:8080
```

Open **http://localhost:8080**, add items from the catalogue, set a discount
(e.g. `0.10`), and press **Calculate Discount & Bill**. Watch:

- the **Recovery-Block trace** — PRIMARY returns `None` (AT REJECT) → ALT-1 (AT PASS);
- the **Durable event history** — CHECKPOINT → WRITE → AT_REJECT → ROLLBACK → WRITE → COMMIT;
- the **billing.db viewer** — one committed row, `0 corrupt rows` badge.

Use **Reset databases** to start fresh between demos.

### Regenerate the LLM artifacts (optional)
```bash
GROQ_API_KEY=sk-... python design_time.py
```

---

## Verifying the claim from the shell

```bash
python -c "import sqlite3;c=sqlite3.connect('billing.db');\
print('rows:',c.execute('SELECT id,total,status,version FROM bills').fetchall());\
print('corrupt:',c.execute('SELECT COUNT(*) FROM bills WHERE total IS NULL').fetchone()[0])"
```

Expected: one row with a real total, `corrupt: 0`.

---

## Why the write-then-rollback ordering matters

If the acceptance test ran *before* any DB write, this would just be input
validation — nothing to roll back, and durable execution would add nothing. The
demo deliberately lets the faulty result **hit the database first**, so the
rollback is load-bearing. That is precisely the capability a durable-execution
engine (Temporal/Restate) provides, and what this PoC reproduces in miniature.

See `TEMPORAL_MAPPING.md` for how each piece maps onto a real Temporal workflow.
