"""
nvp_engine.py — Runtime NVP engine for the LA-NVP billing PoC.

Direct analogue of recovery_engine.py (which implements Recovery Block).
Instead of PRIMARY → AT → ALT-1 → ALT-2 (sequential, RB), this engine runs
three algorithmically-diverse variants concurrently in isolated subprocess
sandboxes (Python + C++ + Java) — exactly the pipeline the paper's §3.1
describes — and reaches a verdict via a hierarchical voter (LLM-generated
voter with a hardcoded majority-voter fallback).

The engine implements **Shape B (optimistic-first-write NVP)**:

  1. All three variants are launched in parallel via ThreadPoolExecutor.
  2. The FIRST variant to return is designated the "first-writer" — its
     output plays the role RB's PRIMARY plays: it is what the caller
     provisionally writes to billing.db.
  3. The remaining two variants finish; then the voter runs.
  4. If the voter's verdict agrees with the first-writer -> commit as-is.
     If it disagrees -> the caller rolls back the first-write and writes
     the voter's answer instead. If no consensus -> the workflow aborts.
     (The DB-level orchestration lives in app_nvp.py; this module returns
     the trace and lets the caller drive the durable workflow.)

For the demo, the first-writer's output is DELIBERATELY POISONED so that the
vote-overrule -> rollback -> recover arc is visible on every request. This is
the exact analogue of the RB demo's FAULTY_PRIMARY_SRC. Pass
`force_poison_first=False` to run the honest, unpoisoned pipeline.

No LLM is called at runtime — the artifact is loaded once at import.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sandbox import stress_test
from runtime2 import _hardcoded_vote, _llm_vote, _poison_output, _SENTINEL_LLM_FAILED

HERE = Path(__file__).parent
ARTIFACT_PATH = HERE / "artifact_nvp_calculate_discounted_total.json"

with open(ARTIFACT_PATH, encoding="utf-8") as _f:
    ARTIFACT = json.load(_f)

PYTHON_TEMPLATE: str = ARTIFACT["python_code"]
CPP_TEMPLATE:    str = ARTIFACT["cpp_code"]
JAVA_TEMPLATE:   str = ARTIFACT["java_code"]
VOTER_CODE:      str = ARTIFACT["voter_code"]
OUTPUT_SCHEMA:   dict = ARTIFACT["output_schema"]


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder substitution
# ─────────────────────────────────────────────────────────────────────────────

def _substitute(code_template: str, subtotal: float, discount: float) -> str:
    """Substitute the runtime inputs into the design-time templates.

    The variants ship with the tokens `__SUBTOTAL__` and `__DISCOUNT__` where
    the real values go; here we do a plain string replace before handing the
    code to the sandbox for compilation / execution.
    """
    return (
        code_template
        .replace("__SUBTOTAL__", repr(float(subtotal)))
        .replace("__DISCOUNT__", repr(float(discount)))
    )


# ─────────────────────────────────────────────────────────────────────────────
# Approximate dict equality (matches the voter's 1e-4 tolerance)
# ─────────────────────────────────────────────────────────────────────────────

def _dicts_close(a, b, tol: float = 1e-4) -> bool:
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return a == b
    if set(a.keys()) != set(b.keys()):
        return False
    for k in a:
        va, vb = a[k], b[k]
        if isinstance(va, bool) or isinstance(vb, bool):
            if va != vb:
                return False
        elif isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if abs(float(va) - float(vb)) > tol:
                return False
        else:
            if va != vb:
                return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_with_nvp(args: tuple, timeout: int = 30, force_poison_first: bool = True) -> dict:
    """
    Execute the NVP protocol (Shape B) for one call.

    Returns:
        {
          "steps":         list[dict],   # human-readable trace (rendered in the UI)
          "first_lang":    str,          # which variant returned first
          "first_result":  dict | None,  # what the first-writer produced
                                          #   (poisoned if force_poison_first=True)
          "voter_verdict": dict | None,  # majority answer, or None if no consensus
          "voter_used":    str,          # 'llm' | 'hardcoded' | 'none'
          "matches_first": bool,         # True  -> commit first-write, no rollback
          "overruled":     bool,         # True  -> rollback first-write, write vote
          "final_result":  dict | None,  # the correct answer (voter_verdict)
          "final_version": str,          # what code produced the final answer
          "variant_outputs": dict,       # per-language raw parsed outputs
          "success":       bool,
        }
    """
    subtotal, discount = args

    py_code   = _substitute(PYTHON_TEMPLATE, subtotal, discount)
    cpp_code  = _substitute(CPP_TEMPLATE,    subtotal, discount)
    java_code = _substitute(JAVA_TEMPLATE,   subtotal, discount)

    steps: list[dict] = []
    completion_order: list[str] = []
    variant_outputs: dict[str, dict | None] = {"python": None, "c++": None, "java": None}

    # ── 1. Fan out to three isolated subprocess sandboxes ─────────────────
    with ThreadPoolExecutor(max_workers=3) as executor:
        fut_to_lang = {
            executor.submit(stress_test, py_code,   1, timeout, "python"): "python",
            executor.submit(stress_test, cpp_code,  1, timeout, "c++"):    "c++",
            executor.submit(stress_test, java_code, 1, timeout, "java"):   "java",
        }
        for fut in as_completed(fut_to_lang):
            lang = fut_to_lang[fut]
            try:
                r = fut.result()
                if r["successes"] == 1 and r["sample_outputs"]:
                    variant_outputs[lang] = r["sample_outputs"][0]
                else:
                    variant_outputs[lang] = None
            except Exception:
                variant_outputs[lang] = None
            completion_order.append(lang)

    # ── 2. Designate the first-to-return as the "first-writer" ────────────
    first_lang = completion_order[0]
    original_first_result = variant_outputs[first_lang]

    if force_poison_first and original_first_result is not None:
        # Deliberate fault injection so the recovery arc is visible every request.
        # This is the NVP analogue of RB's FAULTY_PRIMARY_SRC.
        poisoned = _poison_output(original_first_result)
        variant_outputs[first_lang] = poisoned
        first_result = poisoned
        first_note = (
            f"first variant to return: {first_lang}. Output DELIBERATELY POISONED "
            f"(CCF injection). Original: {original_first_result} -> Poisoned: {poisoned}. "
            f"This is what would hit billing.db as the first (provisional) write."
        )
        first_version = f"{first_lang} (poisoned)"
    else:
        first_result = original_first_result
        first_note = (
            f"first variant to return: {first_lang}. This is what would hit "
            f"billing.db as the first (provisional) write."
        )
        first_version = first_lang

    steps.append({
        "stage":   "FIRST_WRITE",
        "version": first_version,
        "result":  first_result,
        "note":    first_note,
    })

    # ── 3. Record every variant's outcome (in completion order) ───────────
    for lang in completion_order:
        out = variant_outputs[lang]
        if lang == first_lang and force_poison_first and original_first_result is not None:
            note = f"returned first; output was poisoned before the vote."
        elif out is None:
            note = "variant crashed or timed out."
        else:
            note = "variant returned a valid JSON output."
        steps.append({
            "stage":   f"VARIANT_{lang.upper()}",
            "version": lang,
            "result":  out,
            "note":    note,
        })

    # ── 4. Run the hierarchical voter (LLM -> hardcoded fallback) ─────────
    voter_log: list[str] = []
    llm_result = _llm_vote(
        VOTER_CODE,
        variant_outputs["python"],
        variant_outputs["c++"],
        variant_outputs["java"],
        voter_log,
    )
    if llm_result is _SENTINEL_LLM_FAILED or llm_result is None:
        voter_verdict = _hardcoded_vote(
            variant_outputs["python"],
            variant_outputs["c++"],
            variant_outputs["java"],
            voter_log,
        )
        voter_used = "hardcoded" if voter_verdict is not None else "none"
    else:
        voter_verdict = llm_result
        voter_used = "llm"

    steps.append({
        "stage":   "VOTE",
        "version": voter_used,
        "result":  voter_verdict,
        "note":    " | ".join(voter_log[-3:]) if voter_log else "voting complete",
    })

    if voter_verdict is None:
        return {
            "steps":           steps,
            "first_lang":      first_lang,
            "first_result":    first_result,
            "voter_verdict":   None,
            "voter_used":      "none",
            "matches_first":   False,
            "overruled":       False,
            "final_result":    None,
            "final_version":   "none",
            "variant_outputs": variant_outputs,
            "success":         False,
        }

    matches = _dicts_close(first_result, voter_verdict)
    overruled = not matches

    if overruled:
        steps.append({
            "stage":   "OVERRULE",
            "version": voter_used,
            "result":  voter_verdict,
            "note":    (
                f"voter ({voter_used}) disagrees with first-writer "
                f"({first_lang}). The provisional write must be ROLLED BACK "
                f"and replaced with the voter's answer."
            ),
        })
    else:
        steps.append({
            "stage":   "AGREE",
            "version": voter_used,
            "result":  voter_verdict,
            "note":    (
                f"voter ({voter_used}) agrees with first-writer "
                f"({first_lang}). The provisional write can be COMMITTED as-is."
            ),
        })

    return {
        "steps":           steps,
        "first_lang":      first_lang,
        "first_result":    first_result,
        "voter_verdict":   voter_verdict,
        "voter_used":      voter_used,
        "matches_first":   matches,
        "overruled":       overruled,
        "final_result":    voter_verdict,
        "final_version":   (
            f"vote ({voter_used})" if overruled
            else f"{first_lang} (agrees with vote)"
        ),
        "variant_outputs": variant_outputs,
        "success":         True,
    }
