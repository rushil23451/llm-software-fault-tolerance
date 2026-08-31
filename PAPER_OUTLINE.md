# Paper outline

**Working title:** *LLM-Assisted Recovery Blocks: Automated Fault-Tolerant Code
Generation with Checkpointed Runtime Recovery*

**Target venues:** ISSRE, SRDS, or an LLM-for-SE workshop at ICSE/FSE.

**Thesis (one sentence):** LLM-generated recovery-block artifacts (primary + N
diverse alternates + acceptance test), produced at design time, can be executed
inside a durable-execution workflow so that logic faults in stateful software are
recovered at runtime — rolling back corrupt side effects and hot-patching a
diverse alternate — without any LLM call and without leaving bad data behind.

---

## 1. Introduction
- LLMs write buggy code; classical fault tolerance (N-Version, Recovery Blocks)
  assumed *human*-written diversity that was expensive. LLMs make diversity cheap.
- Prior RB work is stateless; real services have side effects (DB writes).
- Contribution list:
  1. A two-phase LA-RBS pipeline (design-time generation, runtime recovery).
  2. Integration with **durable execution** for safe, stateful rollback.
  3. A working PoC (the billing pipeline) demonstrating write→rollback→recover.
  4. An evaluation across EvalPlus with a fault taxonomy and diversity metric.

## 2. Background & Related Work
- Recovery Blocks (Randell 1975) and N-Version Programming (Avizienis); the
  acceptance test as the adjudicator.
- Durable execution: Cadence → Temporal; Restate. Event-log replay,
  Activities, Saga compensation. (Cite that they provide retry, not diversity.)
- LLMs for code generation and self-repair; correlated-failure risk when the
  same model writes the code and its test.

## 3. System Design
- **Design time** (offline, runs once): 4 LLM calls → primary, alt-1, alt-2,
  acceptance test → JSON artifact. Fixes: (1) read `->` return annotation for
  type; (2) `validate_fault_is_visible`; (3) FORBIDDEN-APPROACHES prompt +
  show previous code to force diversity.
- **Runtime** (no LLM): agent watchdog intercepts calls, runs AT, iterates
  alternates, hot-patches via `exec()` into the live module.
- **Durable layer:** checkpoint / provisional write / rollback / commit.
  Include the **Temporal mapping table** (see `TEMPORAL_MAPPING.md`).
- Threat model note on `exec()` (see Discussion).

## 4. Proof-of-Concept Demo
- Architecture: Flask + `products.db` (read) + `billing.db` (write) +
  `history.db` (event log).
- The planted fault: missing-return in `calculate_discounted_total` → NULL total.
- Walk the 9-step sequence; include the **event-history listing** and a
  **before/after billing.db** figure (0 corrupt rows). This is the key figure.

## 5. Evaluation
- **Datasets:** EvalPlus (HumanEval+, 764× tests) for AT-quality; BigCodeBench-Hard
  and PyResBugs/DebugBench for realistic/real faults.
- **Metrics** (compute per task):

  | Metric | Definition |
  |--------|-----------|
  | Recovery Rate (RR) | recovered / (total − phantoms) |
  | MTTR | ms from AT rejection to correct result returned |
  | AT False-Positive Rate | wrong results the AT accepts (check vs EvalPlus 764 tests) |
  | AT False-Negative Rate | correct results the AT rejects (run AT on ground truth) |
  | Version Divergence Score | normalised AST edit distance, primary vs each alternate |
  | Recovery Overhead Ratio | recovery_time / normal_time |

- **Fault taxonomy:** null-return, wrong-type, wrong-value, exception; report
  RR per category (shows where the AT is weak).
- **Diversity result:** AST edit-distance histogram proving Fix 3 works.

## 6. Discussion / Limitations
- AT quality & correlated failure (same LLM writes code + test) — mitigate with
  a different model or property-based tests.
- Scope: pure-fn → stateful now handled for single-DB writes; multi-service
  distributed rollback is future work (real Temporal Saga).
- `exec()` threat model: untrusted LLM code in-process → subprocess/WASM sandbox.
- Determinism constraints durable execution imposes on workflow code.

## 7. Conclusion & Future Work
- Real-Temporal deployment; SWE-bench multi-file stateful repos; larger alternate
  pools; adaptive alternate ordering by past success.

---

## Figures to produce from the PoC
1. Architecture diagram (three DBs, watchdog, durable layer).
2. The 9-step sequence diagram (write → rollback → recover).
3. Event-history listing (`history.db`) + before/after `billing.db`.
4. AST-divergence histogram (from evaluation).
5. RR-per-fault-category bar chart.
