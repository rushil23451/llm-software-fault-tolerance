# From the PoC's mini-durable-layer to a real Temporal workflow (NVP variant)

This is the NVP companion to `TEMPORAL_MAPPING.md` (which covers the Recovery
Block demo). Both PoCs share the same durable layer (`durable.py`) built on
SQLite transactions so they run anywhere with zero setup. This document shows
how each mechanism used by the **NVP** demo (`app_nvp.py` + `nvp_engine.py`)
maps onto a production durable-execution engine (Temporal).

## Concept mapping

| PoC mechanism (`durable.py` + `nvp_engine.py`) | Temporal equivalent | What it guarantees |
|---|---|---|
| `DurableWorkflow` instance | A **Workflow Execution** | One recoverable business transaction |
| `workflow_id` | Workflow Id | Idempotent, addressable run |
| `log_event(...)` → `history.db` | An event appended to **Workflow History** | Every step persisted before the next runs |
| `checkpoint()` (BEGIN txn) | Start of workflow / determinism boundary | A point the engine can resume/replay from |
| `run_with_nvp()` fanning out to `stress_test()` × 3 | **Three sibling Activities**, one per variant, launched with `asyncio.gather` under a **Parent Workflow** | N-way parallel execution with independent retry policies |
| completion order of the futures | Which sibling Activity's `execute_activity` future resolves first | Determines the "first-writer" role |
| `provisional_write_bill(first_result)` | An **Activity** with a side effect, run for the first-completing sibling only | The unit that actually touches external state |
| voter verdict via `_llm_vote` (fallback `_hardcoded_vote`) | A **voter Activity** that receives the outputs of all N sibling Activities and returns the majority answer | Semantic arbitration over parallel results |
| `matches_first == False` branch | Workflow-code branch on the voter Activity's verdict | Trigger for compensation |
| `rollback()` + rewrite | **Saga compensation** (a compensating Activity) + a follow-up `write_bill` Activity | Undo of a first-writer's already-performed side effect, then persist the correct answer |
| `commit()` | Workflow completion | Effects made durable |
| separate `history.db` file | Temporal's history store (Cassandra/…) | History survives even when business data is rolled back |

## The key conceptual difference (the research contribution)

Temporal's built-in failure handling is a **retry policy**: on failure it re-runs
**the same Activity** with backoff. That helps for *transient* faults (network
blip, timeout) but does nothing for a *deterministic logic bug* — a corrupt
implementation of `calculate_discounted_total` will return the same wrong value
on every retry, and a *Common Cause Failure* (CCF) will corrupt all replicas
simultaneously.

LA-NVP replaces the retry policy with **semantically-driven, N-diverse
arbitration**:

> On voter disagreement with the first-writer, the workflow does not re-run the
> same variant — the voter has already **observed a different, algorithmically
> and language-diverse implementation produce a majority answer**, and the
> engine's compensation undoes the first-writer's write before the majority
> answer is persisted.

Retry = *same code, hope the environment changed.*
LA-RBS = *different code (sequential), designed to fail differently.*
LA-NVP = *N different codes (concurrent), a vote decides which one is trusted.*

## Sketch of the real Temporal version (future work / appendix)

```python
@workflow.defn
class NVPBillingWorkflow:
    @workflow.run
    async def run(self, cart, discount):
        subtotal = await workflow.execute_activity(read_prices, cart)

        # Launch all three variants in parallel.
        py_fut   = workflow.execute_activity(run_variant, "python",  subtotal, discount)
        cpp_fut  = workflow.execute_activity(run_variant, "c++",     subtotal, discount)
        java_fut = workflow.execute_activity(run_variant, "java",    subtotal, discount)

        # First-writer = whichever finishes first (Shape B).
        futs = [py_fut, cpp_fut, java_fut]
        done, pending = await asyncio.wait(futs, return_when=asyncio.FIRST_COMPLETED)
        first_fut  = done.pop()
        first_lang, first_result = first_fut.result()

        # Optimistic provisional write (this is the "damage" surface).
        await workflow.execute_activity(
            provisional_write_bill, subtotal, discount, first_result, first_lang)

        # Wait for the remaining two, then vote.
        rest = await asyncio.gather(*pending)
        all_results = [(first_lang, first_result), *rest]
        verdict = await workflow.execute_activity(vote, all_results)

        if verdict is None:
            await workflow.execute_activity(compensate_write_bill)
            raise ApplicationError("NVP_ABORT: no consensus")

        if verdict != first_result:
            # Voter overrules → Saga compensation + rewrite.
            await workflow.execute_activity(compensate_write_bill)
            await workflow.execute_activity(
                write_bill, subtotal, discount, verdict, f"vote")
        # else: first-writer agreed with the vote — nothing to compensate.

        return verdict
```

The PoC's `nvp_engine.run_with_nvp` + `app_nvp.py` orchestration is the
single-process analogue of the workflow above; the PoC's `rollback()` is the
single-process analogue of `compensate_write_bill`.

## When NVP wins vs. when RB wins (paper-relevant)

| Consideration | Recovery Block (LA-RBS) | N-Version Programming (LA-NVP) |
|---|---|---|
| Cost per successful call | 1× variant + 1× AT (if primary passes) | Nx variant + 1× voter (always) |
| Latency | Best-case = primary time; worst = primary + Σ alternates | ≈ slowest of the N variants |
| Fault it catches most naturally | Deterministic bug in one variant, detectable by an oracle | CCF-style silent corruption where the majority still returns the truth |
| Judgement it needs from the LLM | A well-formed **acceptance test** (an oracle over the output) | A well-formed **voter** (agreement over the outputs — no oracle needed) |
| Compensation surface | Rolls back the primary's write if AT rejects | Rolls back the first-writer's write if the vote overrules |

Both mechanisms use the same durable layer to make the compensation load-bearing;
their difference is entirely in *how* the decision to compensate is reached.
