# From the PoC's mini-durable-layer to a real Temporal workflow

The PoC implements durable execution with SQLite transactions so it runs
anywhere with zero setup. This document shows how each PoC mechanism maps onto a
production durable-execution engine (Temporal). Put the table in the paper's
**System Design** section; it is the bridge between the working demo and the
stated research claim.

## Concept mapping

| PoC mechanism (`durable.py`) | Temporal equivalent | What it guarantees |
|------------------------------|---------------------|--------------------|
| `DurableWorkflow` instance | A **Workflow Execution** | One recoverable business transaction |
| `workflow_id` | Workflow Id | Idempotent, addressable run |
| `log_event(...)` → `history.db` | An event appended to **Workflow History** | Every step persisted before the next runs |
| `checkpoint()` (BEGIN txn) | Start of workflow / determinism boundary | A point the engine can resume/replay from |
| `provisional_write_bill(...)` | An **Activity** with a side effect | The unit that actually touches external state |
| acceptance test verdict | A branch in workflow code | Semantic decision on whether to proceed |
| `rollback()` | **Saga compensation** (compensating Activity) | Undo of an already-performed side effect |
| `commit()` | Workflow completion | Effects made durable |
| separate `history.db` file | Temporal's history store (Cassandra/…) | History survives even when business data is rolled back |

## The key conceptual difference (the research contribution)

Temporal's built-in failure handling is a **retry policy**: on failure it re-runs
**the same Activity** with backoff. That helps for *transient* faults (network
blip, timeout) but does nothing for a *deterministic logic bug* — the discount
function will return `None` every time.

LA-RBS replaces the retry policy with **semantically-driven recovery**:

> On acceptance-test rejection, the workflow does not re-run the same code — it
> runs a **different, algorithmically-diverse implementation** generated offline
> by an LLM, verified by an LLM-generated acceptance test, using the engine's
> compensation to undo the faulty attempt's writes first.

Retry = *same code, hope the environment changed.*
LA-RBS = *different code, designed to fail differently.*

## Sketch of the real Temporal version (future work / appendix)

```python
@workflow.defn
class BillingWorkflow:
    @workflow.run
    async def run(self, cart, discount):
        subtotal = await workflow.execute_activity(read_prices, cart)
        for version in ["primary", "alt-1", "alt-2"]:
            total = await workflow.execute_activity(
                compute_discount, version, subtotal, discount)
            if await workflow.execute_activity(acceptance_test, total, subtotal, discount):
                await workflow.execute_activity(write_bill, subtotal, discount, total, version)
                return total
            # else: no bill was written for this version; try the next one
        raise ApplicationError("RB_FAILURE: all alternates exhausted")
```

To reproduce the *write-then-rollback* demo under Temporal, make `write_bill`
provisional and register a `compensate_write_bill` Activity that the workflow
invokes (Saga style) when the acceptance test rejects a version that already
wrote. The PoC's `rollback()` is the single-process analogue of that
compensation.
