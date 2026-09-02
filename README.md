# LA-RBS + LA-NVP Billing PoC — Checkpointed Fault-Tolerance Demos

A proof-of-concept for **LLM-assisted software fault tolerance with durable
(checkpointed) runtime recovery**. Two matched demos, sharing the same
domain (order/billing over SQLite) and the same durable-execution layer, run
side-by-side to show two complementary mechanisms:

| Demo | Port | Mechanism | Detector | What LLM generated (offline) |
|------|------|-----------|----------|------------------------------|
| **LA-RBS** — `app.py` | **8080** | Sequential Recovery Block (primary → AT → alt-1 → alt-2) | acceptance test | primary + 2 alternates + AT |
| **LA-NVP** — `app_nvp.py` | **8081** | Concurrent N-Version Programming (Py + C++ + Java run in parallel; voter arbitrates) — wired in **Shape B** (optimistic-first-write + vote-driven compensation) | majority voter | 3 diverse variants + voter |

Both demos show the same *stateful* pipeline — where a **faulty or
CCF-corrupted discount function** would write a **corrupt bill** to
`billing.db`. The minimal durable-execution layer **rolls the bad write back**,
the fault-tolerance mechanism (RB alternate, or NVP vote-overrule) supplies the
correct answer, and **only a correct bill is ever committed**.

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
| `nvp_engine.py` | Runtime NVP engine implementing **Shape B**: launches all 3 variants concurrently via `sandbox.stress_test`, designates the first-to-return as the first-writer (poisoned by default for the demo), runs the LLM voter (falls back to a hardcoded majority voter), and returns a trace shaped so `app_nvp.py` can drive the durable workflow with minimal glue. |
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
Python one. Add items, set a discount, tick **"Poison first-writer"** (on by
default), press **Run NVP checkout**. Watch:
- the **three variant tiles** — one is flagged FIRST · POISONED;
- the **voter strip** — voter reaches CONSENSUS on the two clean variants and OVERRULES the poisoned first-writer;
- the **Durable event history** — CHECKPOINT → WRITE → VOTE_OVERRULE → ROLLBACK → WRITE → COMMIT;
- the **billing.db viewer** — one committed row, `0 corrupt rows` badge.

Untick "Poison first-writer" to see the happy path: voter agrees with the
first-writer → single COMMIT, no rollback.

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

## Why NVP is wired in "Shape B" (optimistic-first-write)

Textbook NVP runs all N variants first, votes, and only *then* writes the
winner. Under that shape, nothing bad ever reaches the DB, so there is nothing
to roll back and `durable.py` becomes decorative — the demo would lose its
whole point (the durable layer needs to be *load-bearing*, not decorative, for
the paper's claim to hold).

`app_nvp.py` therefore drives NVP in **Shape B**: the first variant to return
is provisionally written to `billing.db` **before** the vote completes. If the
voter (running once all three finish) disagrees with the first-writer, the
durable layer rolls the corrupt row back and writes the majority answer
instead. This mirrors the RB demo's write-then-detect-then-rollback arc
one-for-one, just with a voter as the detector and *first-to-return* as the
analogue of RB's primary.

`nvp_engine.py` has a `force_poison_first=True` knob (on by default, exposed as
the "Poison first-writer" checkbox in the UI) that deterministically corrupts
the first-writer's output. This is the NVP analogue of RB's planted
`FAULTY_PRIMARY_SRC` — it guarantees the compensation arc is visible on every
request. Turn it off to see the honest run.
