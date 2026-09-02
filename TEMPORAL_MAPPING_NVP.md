# From the PoC's mini-durable-layer to a real Temporal workflow (NVP variant)

This is the NVP companion to `TEMPORAL_MAPPING.md` (which covers the Recovery
Block demo). Both PoCs share the same durable layer (`durable.py`) built on
SQLite transactions so they run anywhere with zero setup. This document
shows how the mechanisms used by the **NVP** demo (`app_nvp.py` +
`nvp_engine.py`) map onto a production durable-execution engine (Temporal).

## Where NVP differs from RB (the important framing)

**Recovery Block is a *backward* error-recovery scheme.** The primary runs,
its output might already have hit persistent state, an acceptance test then
rejects it, and the engine's compensation (`rollback()` here; a saga
compensating Activity in Temporal) undoes the damage before an alternate
is tried.

**N-Version Programming is a *forward* error-recovery scheme.** All N variants
run in parallel, a voter arbitrates their outputs, and only the voter's answer
is ever written to persistent state. Because arbitration happens *before* the
side effect, there is nothing to compensate on the happy path — fault masking
is achieved via space redundancy alone.

The durable-execution layer is still useful for NVP — it gives the write an
addressable workflow id, appends every step to an event history that survives
restarts, and guarantees atomicity of the write itself — but it is not being
asked to compensate a corrupt write, because no corrupt write happens.

## Concept mapping

| PoC mechanism (`durable.py` + `nvp_engine.py`) | Temporal equivalent | What it guarantees |
|---|---|---|
| `DurableWorkflow` instance | A **Workflow Execution** | One recoverable business transaction |
| `workflow_id` | Workflow Id | Idempotent, addressable run |
| `log_event(...)` → `history.db` | An event appended to **Workflow History** | Every step persisted before the next runs |
| `checkpoint()` (BEGIN txn) | Start of workflow / determinism boundary | A point the engine can resume/replay from |
| `run_with_nvp()` fanning out to `stress_test()` × 3 | **Three sibling Activities** (one per variant) launched with `asyncio.gather` under a **Parent Workflow** | N-way parallel execution with independent retry policies |
| voter verdict via `_llm_vote` (fallback `_hardcoded_vote`) | A **voter Activity** that receives the outputs of all N sibling Activities and returns the majority answer | Semantic arbitration over parallel results |
| `provisional_write_bill(voter_verdict)` | An **Activity** with a side effect, run once after the voter Activity resolves | The unit that actually touches external state |
| `commit()` | Workflow completion | Effect made durable |
| `rollback()` in the no-consensus branch | Aborting the workflow before any side-effecting Activity has been called | An empty transaction closed cleanly (no compensation is needed because no write occurred) |
| separate `history.db` file | Temporal's history store (Cassandra/…) | History survives even when business data would have been rolled back |

There is **no Saga compensation row** in this table — that entry only appears in
`TEMPORAL_MAPPING.md` (the RB variant), because backward recovery is the
mechanism RB needs and NVP does not.

## The key conceptual difference (the research contribution)

Temporal's built-in failure handling is a **retry policy**: on failure it re-runs
**the same Activity** with backoff. That helps for *transient* faults (network
blip, timeout) but does nothing for a *deterministic logic bug* — a corrupt
implementation of `calculate_discounted_total` will return the same wrong value
on every retry — or a *Common-Cause Failure* (CCF) that corrupts all replicas
of the same implementation simultaneously.

LA-NVP replaces the retry policy with **semantically-driven, N-diverse
arbitration**:

> N algorithmically- and language-diverse implementations run concurrently.
> A voter observes all N outputs and returns the majority answer. The Workflow
> writes only that answer.

Retry = *same code, hope the environment changed.*
LA-RBS = *different code (sequential), designed to fail differently, with
compensation to undo an accepted-then-rejected write.*
LA-NVP = *N different codes (concurrent), the vote decides which one is
trusted before anything is written — no compensation required.*

## Sketch of the real Temporal version (future work / appendix)

```python
@workflow.defn
class NVPBillingWorkflow:
    @workflow.run
    async def run(self, cart, discount):
        subtotal = await workflow.execute_activity(read_prices, cart)

        # Launch all three variants concurrently.
        results = await asyncio.gather(
            workflow.execute_activity(run_variant, "python",  subtotal, discount),
            workflow.execute_activity(run_variant, "c++",     subtotal, discount),
            workflow.execute_activity(run_variant, "java",    subtotal, discount),
        )

        verdict = await workflow.execute_activity(vote, results)
        if verdict is None:
            raise ApplicationError("NVP_ABORT: no consensus")

        # Single write — no compensation possible or necessary, because
        # nothing was written before this call.
        await workflow.execute_activity(
            write_bill, subtotal, discount, verdict, "vote")

        return verdict
```

The PoC's `nvp_engine.run_with_nvp` + `app_nvp.py` orchestration is the
single-process analogue of the workflow above. Note the total absence of a
`compensate_write_bill` Activity — that is deliberate.

## When NVP wins vs. when RB wins (paper-relevant)

| Consideration | Recovery Block (LA-RBS) | N-Version Programming (LA-NVP) |
|---|---|---|
| Recovery direction | Backward (rollback + retry with alternate) | Forward (mask via majority vote) |
| Cost per successful call | 1× variant + 1× AT (if primary passes); worst-case N variants + N ATs | Always N variants + 1× vote |
| Latency | Best-case = primary time; worst = primary + Σ alternates | ≈ slowest of the N variants |
| Fault it catches most naturally | Deterministic bug in one variant that a semantic oracle can detect | CCF-style silent corruption of *one* variant where the majority still returns the truth |
| Judgement it needs from the LLM | A well-formed **acceptance test** (an oracle over the output) | A well-formed **voter** (agreement over the outputs — no oracle needed) |
| Compensation surface | Rolls back the primary's write if AT rejects | Not applicable — no writes happen before the vote |
| Suited to real-time / hard deadlines | Weaker — worst-case fan-out is sequential | Stronger — masking is bounded by the slowest variant, not by retries |

Both mechanisms use the same durable layer for atomicity and for appending an
event history; their difference is entirely in *how* — and *when* — the
decision to write is reached.
