"""
db_setup.py — Create and seed the two SQLite databases used by the PoC.

  products.db  — READ-ONLY during the checkout flow. The catalogue of items
                 the user can add to a cart.
  billing.db   — WRITTEN to during checkout. This is the database we must
                 protect from corrupt records (table: bills).
  history.db   — The durable-execution event history (table: durable_log).
                 Kept in a SEPARATE file ON PURPOSE: in a real durable engine
                 (e.g. Temporal) the event history lives in its own store, so a
                 ROLLBACK of the business DB never erases the recorded history.

Run once:  python db_setup.py
It is safe to re-run; it drops and recreates both databases.
"""

import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
PRODUCTS_DB = HERE / "products.db"
BILLING_DB = HERE / "billing.db"
HISTORY_DB = HERE / "history.db"

SEED_PRODUCTS = [
    # (name, unit_price, stock)
    ("USB-C Cable",        7.99,  120),
    ("Wireless Mouse",     15.99, 60),
    ("Mechanical Keyboard", 79.50, 25),
    ("27\" Monitor",       229.00, 12),
    ("Laptop Stand",       34.25, 40),
    ("Webcam 1080p",       48.00, 30),
    ("Noise-Cancel Headset", 129.99, 18),
]


def setup_products():
    if PRODUCTS_DB.exists():
        PRODUCTS_DB.unlink()
    conn = sqlite3.connect(PRODUCTS_DB)
    conn.execute("""
        CREATE TABLE products (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            unit_price REAL    NOT NULL,
            stock      INTEGER NOT NULL
        )
    """)
    conn.executemany(
        "INSERT INTO products (name, unit_price, stock) VALUES (?, ?, ?)",
        SEED_PRODUCTS,
    )
    conn.commit()
    conn.close()
    print(f"[DB] products.db created and seeded with {len(SEED_PRODUCTS)} items.")


def setup_billing():
    if BILLING_DB.exists():
        BILLING_DB.unlink()
    conn = sqlite3.connect(BILLING_DB)
    conn.execute("""
        CREATE TABLE bills (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            items_json TEXT    NOT NULL,
            subtotal   REAL    NOT NULL,
            discount   REAL    NOT NULL,
            total      REAL,                 -- nullable ON PURPOSE: a faulty
                                             -- primary can try to write NULL here
            status     TEXT    NOT NULL,      -- COMMITTED
            version    TEXT    NOT NULL,      -- which code version produced it
            created_at TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] billing.db created (bills table, empty).")


def setup_history():
    if HISTORY_DB.exists():
        HISTORY_DB.unlink()
    conn = sqlite3.connect(HISTORY_DB)
    # The durable-execution "event history". Every step of a checkout workflow
    # is appended here BEFORE the next step runs — this is what a platform like
    # Temporal persists for you automatically. It lives in its own file so a
    # ROLLBACK of billing.db cannot erase it.
    conn.execute("""
        CREATE TABLE durable_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id  TEXT    NOT NULL,
            seq          INTEGER NOT NULL,
            event_type   TEXT    NOT NULL,   -- CHECKPOINT | WRITE | AT_REJECT
                                             -- | ROLLBACK | COMMIT
            detail       TEXT    NOT NULL,
            ts           TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] history.db created (durable_log table, empty).")


if __name__ == "__main__":
    setup_products()
    setup_billing()
    setup_history()
    print("[DB] Setup complete.")
