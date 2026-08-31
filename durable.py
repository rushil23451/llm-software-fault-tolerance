"""
durable.py — A minimal "durable execution" layer built on SQLite transactions.

This is the PoC stand-in for Temporal/Restate. It gives us the two mechanics
that a real durable-execution engine provides:

  1. CHECKPOINT   — capture a consistent point we can return to. Here a
                    checkpoint = the start of a SQLite transaction (BEGIN) on
                    the billing DB. Anything written after it is provisional
                    until COMMIT.
  2. ROLLBACK     — undo every side effect performed since the checkpoint.
                    Here that is SQLite's ROLLBACK: provisional bill rows vanish
                    as if they never happened.

Two separate connections / files are used ON PURPOSE:
  * billing.db (transactional)  — the business writes we protect and can undo.
  * history.db (autocommit)     — the event history. Because it is a separate
                                  store, a ROLLBACK of billing.db never erases
                                  the recorded history. This mirrors how a real
                                  durable engine keeps its Workflow history in a
                                  store independent of your application data.

Mapping to Temporal (used in the paper):
    DurableWorkflow.checkpoint()  ~  start of a Workflow execution
    log_event(...)                ~  an event appended to the Workflow history
    provisional_write_bill(...)   ~  an Activity that performs a side effect
    rollback()                    ~  Saga compensation (undo committed effects)
    commit()                      ~  Workflow completion
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
BILLING_DB = HERE / "billing.db"
HISTORY_DB = HERE / "history.db"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DurableWorkflow:
    """
    One checkout attempt = one durable workflow instance.

    Usage:
        wf = DurableWorkflow()
        wf.checkpoint("pre-billing state captured")
        wf.provisional_write_bill(...)             # written, NOT committed
        wf.log_event("AT_REJECT", "...")
        wf.rollback("AT rejected faulty total")    # provisional write disappears
        ... run alternate ...
        wf.provisional_write_bill(...)
        wf.commit("correct bill committed")
        wf.close()
    """

    def __init__(self):
        self.workflow_id = "wf_" + uuid.uuid4().hex[:8]
        self.seq = 0
        # Business connection: manual transaction control (isolation_level=None
        # lets us issue explicit BEGIN / ROLLBACK / COMMIT).
        self.biz = sqlite3.connect(BILLING_DB, isolation_level=None)
        self.biz.row_factory = sqlite3.Row
        self.biz.execute("PRAGMA busy_timeout = 3000")
        self._in_txn = False
        # History connection: separate file, autocommit. Never rolled back.
        self.hist = sqlite3.connect(HISTORY_DB, isolation_level=None)
        self.hist.execute("PRAGMA busy_timeout = 3000")
        self.trace = []  # human-readable trace returned to the UI

    # ── event history (always durable) ───────────────────────────────
    def log_event(self, event_type: str, detail: str):
        self.seq += 1
        ts = _now()
        self.hist.execute(
            "INSERT INTO durable_log (workflow_id, seq, event_type, detail, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workflow_id, self.seq, event_type, detail, ts),
        )
        entry = {"seq": self.seq, "type": event_type, "detail": detail, "ts": ts}
        self.trace.append(entry)
        print(f"  [{self.workflow_id}] #{self.seq} {event_type}: {detail}")
        return entry

    # ── checkpoint = open a transaction on the billing DB ────────────
    def checkpoint(self, detail="pre-fault state captured"):
        if not self._in_txn:
            self.biz.execute("BEGIN")
            self._in_txn = True
        self.log_event("CHECKPOINT", detail)

    # ── provisional (uncommitted) write ──────────────────────────────
    def provisional_write_bill(self, items_json, subtotal, discount,
                               total, status, version):
        """Write a bill row INSIDE the current transaction. Not durable until
        commit(); a rollback() erases it."""
        if not self._in_txn:
            self.biz.execute("BEGIN")
            self._in_txn = True
        cur = self.biz.execute(
            "INSERT INTO bills (items_json, subtotal, discount, total, status, "
            "version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (items_json, subtotal, discount, total, status, version, _now()),
        )
        self.log_event(
            "WRITE",
            f"provisional bill row id={cur.lastrowid} total={total!r} "
            f"status={status} version='{version}'",
        )
        return cur.lastrowid

    # ── rollback = undo everything since checkpoint ──────────────────
    def rollback(self, reason: str):
        if self._in_txn:
            self.biz.execute("ROLLBACK")
            self._in_txn = False
        self.log_event("ROLLBACK", f"provisional writes undone — {reason}")

    # ── commit = make the effects durable ────────────────────────────
    def commit(self, detail="workflow committed"):
        if self._in_txn:
            self.biz.execute("COMMIT")
            self._in_txn = False
        self.log_event("COMMIT", detail)

    def close(self):
        if self._in_txn:
            self.biz.execute("ROLLBACK")
            self._in_txn = False
        self.biz.close()
        self.hist.close()
