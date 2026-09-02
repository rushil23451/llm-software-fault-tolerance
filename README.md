# LA-RBS + LA-NVP Billing PoC — Checkpointed Fault-Tolerance Demos

A proof-of-concept for **LLM-assisted software fault tolerance with durable
(checkpointed) runtime recovery**. Two matched demos, sharing the same
domain (order/billing over SQLite) and the same durable-execution layer, run
side-by-side to show two complementary mechanisms:

| Demo | Port | Mechanism | Detector | What LLM generated (offline) |
|------|------|-----------|----------|------------------------------|
| **LA-RBS** — `app.py` | **8080** | Sequential Recovery Block (primary → AT → alt-1 → alt-2) | acceptance test | primary + 2 alternates + AT |
| **LA-NVP** — `app_nvp.py` | **8081** | Pure concurrent N-Version Programming (Py + C++ + Java run in parallel; voter arbitrates *before* any write) — a forward error-recovery scheme, exactly as the paper's §3.1 describes | majority voter | 3 diverse variants + voter |

Both demos share the same *stateful* pipeline (order cart → discount → bill),
but they defend against faults in opposite temporal directions:

- **LA-RBS** — a faulty primary WOULD write a **corrupt bill** to
  `billing.db`; an acceptance test rejects it AFTER the write; the durable
  layer **rolls it back** and an algorithmically-diverse alternate is
  hot-patched in. This is **backward** error recovery, and the durable
  layer's compensation is load-bearing.
- **LA-NVP** — three variants run in parallel and their outputs are
  arbitrated by a majority voter **before** any bill is written. The
  corrupt output of a CCF-poisoned variant never reaches `billing.db`.
  This is **forward** error recovery — the standard textbook property of
  NVP; the durable layer is used only for atomicity of the single write
  (no compensation happens, and the demo's event log has no ROLLBACK on
  the happy path).

These are the "one deep example" the paper is built around.

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

### Shared

| File | Role |
|------|------|
| `db_setup.py` | Creates + seeds `products.db`, `billing.db`, `history.db`. |
| `durable.py` | The **durable-execution layer** — checkpoint / provisional write / rollback / commit on SQLite. The PoC stand-in for Temporal. Used by both demos unchanged. |

### LA-RBS (Recovery Block demo)

| File | Role |
|------|------|
| `design_time.py` | Runs once, offline. LLM generates primary + 2 alternates + acceptance test → `artifact_calculate_discounted_total.json`. **No LLM at runtime.** |
| `artifact_calculate_discounted_total.json` | Pre-generated artifact (ships here so the demo runs with **no API key**). |
| `recovery_engine.py` | Runtime RB engine: loads the faulty primary, runs the acceptance test, iterates alternates, hot-patches the winner. |
| `app.py` | Flask server on **:8080**; drives the durable workflow. |
| `templates/index.html` | RB UI. |

### LA-NVP (N-Version Programming demo)

| File | Role |
|------|------|
| `designtime.py`, `runtime2.py`, `sandbox.py`, `models.py`, `main.py` | The paper's §3.1 NVP pipeline itself — manager + 3 generators (Python/C++/Java) + voter generator + polyglot subprocess sandbox + hierarchical voter. Reused verbatim by the billing demo. |
| `design_time_nvp.py` | Thin wrapper that pins the NVP pipeline to the billing spec (`calculate_discounted_total`) and freezes the resulting `NVPArtifact` into JSON. Runs once, offline. **No LLM at runtime.** |
| `_build_nvp_artifact.py` | Offline builder for the shipped, hand-crafted NVP artifact — lets the demo run with **no API key**. |
| `artifact_nvp_calculate_discounted_total.json` | Pre-generated NVP artifact (3 diverse variants with `__SUBTOTAL__`/`__DISCOUNT__` placeholders + voter code). |
| `nvp_engine.py` | Runtime NVP engine (pure NVP): launches all 3 variants concurrently via `sandbox.stress_test`, optionally CCF-poisons one variant's output *after* execution and *before* the vote, runs the LLM voter (falls back to a hardcoded majority voter), and returns the majority verdict. Never touches the DB. |
| `app_nvp.py` | Flask server on **:8081**; endpoints mirror `app.py` 1:1 but drive the NVP workflow. |
| `templates/index_nvp.html` | NVP UI: three variant tiles side-by-side, voter/decision strip, durable event log, DB viewer. |
| `TEMPORAL_MAPPING_NVP.md` | NVP companion to `TEMPORAL_MAPPING.md`. |

---

## Run the demos

```bash
pip install -r requirements.txt
python db_setup.py        # create + seed the databases (once, shared by both)
```

### LA-RBS (Recovery Block)

```bash
python app.py             # http://localhost:8080
```

Add items, set a discount, press **Calculate Discount & Bill**. Watch:
- the **Recovery-Block trace** — PRIMARY returns `None` (AT REJECT) → ALT-1 (AT PASS);
- the **Durable event history** — CHECKPOINT → WRITE → AT_REJECT → ROLLBACK → WRITE → COMMIT;
- the **billing.db viewer** — one committed row, `0 corrupt rows` badge.

### LA-NVP (N-Version Programming)

```bash
python app_nvp.py         # http://localhost:8081
```

Requires `g++` (any C++17 compiler) and `javac`/`java` (JDK) on PATH — the NVP
pipeline compiles and runs a C++ variant and a Java variant alongside the
Python one. Add items, set a discount, tick **"Poison one variant"** (on by
default), press **Run NVP checkout**. Watch:
- the **three variant tiles** — one is flagged CCF POISONED;
- the **voter strip** — voter reaches CONSENSUS on the two clean variants and
  the poisoned one is masked;
- the **Durable event history** — `CHECKPOINT → WRITE → COMMIT` (no rollback
  on the happy path — this is NVP as forward recovery);
- the **billing.db viewer** — one committed row, `0 corrupt rows` badge.

Untick "Poison one variant" to see the honest run: all three variants agree,
voter reaches the same verdict, same single `CHECKPOINT → WRITE → COMMIT`
sequence. The event log is *identical* on the happy path whether or not
poison was injected — that is precisely the forward-recovery property.

Both demos share `billing.db` / `history.db` — the badge accumulates rows across
both if you leave the DBs alone. Press **Reset databases** in either UI to
start fresh.

### Regenerate the LLM artifacts (optional)
```bash
# RB artifact
GROQ_API_KEY=sk-... python design_time.py

# NVP artifact (regenerates 3 diverse variants + voter via the pipeline in
# designtime.py; verify each generated variant kept the __SUBTOTAL__ /
# __DISCOUNT__ placeholders):
GROQ_API_KEY=sk-... python design_time_nvp.py

# Rebuild the hand-crafted NVP artifact (no API key needed):
python _build_nvp_artifact.py
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

See `TEMPORAL_MAPPING.md` (RB) and `TEMPORAL_MAPPING_NVP.md` (NVP) for how each
piece maps onto a real Temporal workflow.

---

## Why the NVP demo has no rollback on the happy path

This is intentional and follows the standard textbook definition of NVP.
NVP is a **forward** error-recovery scheme: N variants run in parallel,
a voter arbitrates their outputs, and only the voter's answer is ever
written to persistent state. Because arbitration happens *before* the
side effect, there is nothing to compensate — the corrupt output of a
CCF-poisoned variant is discarded by the voter and never reaches
`billing.db`.

The durable layer is still used — for atomicity of the write itself and
for the addressable event history that survives restarts — but its
`rollback()` is only ever invoked in the exceptional no-consensus case,
where it just closes an empty transaction (no rows were written, so
nothing is actually undone). Contrast this with the RB demo, where
`rollback()` is load-bearing on every recovery request.

`nvp_engine.py` has an `inject_ccf=True` knob (on by default, exposed as
the "Poison one variant" checkbox in the UI). It corrupts one variant's
output *after* execution and *before* the vote, purely to make it easy to
verify that the voter masks the corruption. Turn it off to see the
identical `CHECKPOINT → WRITE → COMMIT` sequence run with no poison
present at all — the point being that the event history is identical
whether or not a CCF was masked.
