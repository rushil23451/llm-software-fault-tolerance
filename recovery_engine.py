"""
recovery_engine.py — The runtime Recovery-Block engine (no LLM at runtime).

Adapted from the original runtime_service.py watchdog. Responsibilities:
  1. Load the design-time artifact (primary + 2 alternates + acceptance test).
  2. Compile the acceptance test once.
  3. Load a DELIBERATELY FAULTY primary into a live module (to demonstrate
     recovery — in production this would be your real, possibly-buggy code).
  4. Expose run_with_recovery(): call the live function, judge with the AT,
     and if it fails, iterate the algorithmically-diverse alternates, hot-patch
     the first one that passes, and report a full trace.

This module contains NO durable-execution logic. It answers one question:
"given these inputs, what is the correct result and which code version produced
it?" The caller (app.py) wraps it in the durable workflow so that the DB write
performed with the faulty result can be rolled back.
"""

import json
import sys
import types
import textwrap
from pathlib import Path

HERE = Path(__file__).parent
ARTIFACT_PATH = HERE / "artifact_calculate_discounted_total.json"

# ── Load artifact (design-time output; no LLM call here) ─────────────
with open(ARTIFACT_PATH) as f:
    ARTIFACT = json.load(f)

ENTRY = ARTIFACT["entry_point"]              # calculate_discounted_total
PRIMARY_SRC = ARTIFACT["primary_code"]        # the designed (correct) primary
ALTS = ARTIFACT["alternates"]                 # [alt1_src, alt2_src]
AT_SRC = ARTIFACT["at_code"]

# ── Compile the acceptance test once ─────────────────────────────────
_at_ns = {}
exec(compile(AT_SRC, "<at>", "exec"), _at_ns)
acceptance_test = _at_ns["acceptance_test"]

# ── The planted fault ────────────────────────────────────────────────
# A classic "missing return" bug: it computes the total but forgets to
# return it, so the function yields None -> a NULL total would hit the DB.
FAULTY_PRIMARY_SRC = textwrap.dedent("""\
    def calculate_discounted_total(subtotal: float, discount: float) -> float:
        total = subtotal * (1 - discount)
        # BUG (planted): missing `return total` — function returns None.
        rounded = round(total, 2)
""")

# ── Build the live module and load the faulty primary ────────────────
pricing_module = types.ModuleType("pricing_service")
exec(compile(FAULTY_PRIMARY_SRC, "<faulty_primary>", "exec"),
     pricing_module.__dict__)
sys.modules["pricing_service"] = pricing_module

_patched_with = "primary (faulty)"


def current_version() -> str:
    return _patched_with


def hot_patch(source: str, label: str):
    """Replace the live function in-place, no restart (exec into module dict)."""
    global _patched_with
    exec(compile(source, f"<hot_patch:{label}>", "exec"), pricing_module.__dict__)
    _patched_with = label


def reset_to_faulty():
    """Restore the faulty primary so each demo request starts fresh and the
    recovery is visible every time (otherwise a prior hot-patch would already
    have fixed the module)."""
    global _patched_with
    exec(compile(FAULTY_PRIMARY_SRC, "<faulty_primary>", "exec"),
         pricing_module.__dict__)
    _patched_with = "primary (faulty)"


def run_with_recovery(args: tuple):
    """
    Execute the recovery-block protocol for one call.

    Returns a dict:
      {
        "steps":       [ {stage, version, result, at_pass, note}, ... ],
        "final_result": <value or None>,
        "final_version": <str>,
        "recovered":   <bool>,
        "primary_result": <value>,   # what the faulty primary produced
        "success":     <bool>,
      }
    The caller decides what to do about DB writes based on this trace.
    """
    steps = []

    # 1) Run the (faulty) live primary.
    fn = getattr(pricing_module, ENTRY)
    primary_result = fn(*args)
    primary_ok = bool(acceptance_test(primary_result, *args))
    steps.append({
        "stage": "PRIMARY",
        "version": current_version(),
        "result": primary_result,
        "at_pass": primary_ok,
        "note": "faulty primary executed" if not primary_ok
                else "primary passed",
    })

    if primary_ok:
        return {
            "steps": steps,
            "final_result": primary_result,
            "final_version": current_version(),
            "recovered": False,
            "primary_result": primary_result,
            "success": True,
        }

    # 2) Recovery block: try each algorithmically-diverse alternate in order.
    for i, alt_src in enumerate(ALTS, start=1):
        ns = {}
        exec(compile(alt_src, f"<alt{i}>", "exec"), ns)
        alt_result = ns[ENTRY](*args)
        alt_ok = bool(acceptance_test(alt_result, *args))
        steps.append({
            "stage": f"ALT-{i}",
            "version": f"alt-{i}",
            "result": alt_result,
            "at_pass": alt_ok,
            "note": "acceptance test PASSED" if alt_ok
                    else "acceptance test rejected",
        })
        if alt_ok:
            hot_patch(alt_src, f"alt-{i}")
            return {
                "steps": steps,
                "final_result": alt_result,
                "final_version": f"alt-{i}",
                "recovered": True,
                "primary_result": primary_result,
                "success": True,
            }

    # 3) All alternates exhausted.
    return {
        "steps": steps,
        "final_result": None,
        "final_version": "none",
        "recovered": False,
        "primary_result": primary_result,
        "success": False,
    }
