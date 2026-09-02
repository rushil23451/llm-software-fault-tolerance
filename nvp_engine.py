"""
nvp_engine.py — Runtime NVP engine for the LA-NVP billing PoC (pure NVP).

Direct analogue of recovery_engine.py, which implements Recovery Block — a
BACKWARD error-recovery scheme (try primary → on failure, roll back and try
an alternate). This engine instead implements N-Version Programming, a
FORWARD error-recovery scheme, exactly as the paper's §3.1 describes:

  1. Three algorithmically-diverse variants run concurrently in isolated
     subprocess sandboxes (Python + C++ + Java) via ThreadPoolExecutor.
  2. A hierarchical voter arbitrates their outputs — the LLM-generated
     voter first, with a hardcoded majority voter as fallback if the LLM
     voter crashes or reaches no consensus.
  3. The voter's majority answer is what the caller then persists.

Because NVP masks faults BEFORE any side effect touches persistent state,
there is no notion of "the primary wrote a corrupt row that we now have to
undo" here. In the happy path the caller performs a single durable write of
the voter's verdict. Rollback is only ever used in the exceptional
no-consensus case, and even then it just closes an empty transaction —
nothing was written, so nothing is actually undone. This is the classical
NVP property: fault masking via space redundancy, no time-travel required.

No LLM is called at runtime — the artifact is loaded once at import.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sandbox import stress_test
from runtime2 import _hardcoded_vote, _llm_vote, _poison_output, _SENTINEL_LLM_FAILED

HERE = Path(__file__).parent
ARTIFACT_PATH = HERE / "artifact_nvp_calculate_discounted_total.json"

with open(ARTIFACT_PATH, encoding="utf-8") as _f:
    ARTIFACT = json.load(_f)

PYTHON_TEMPLATE: str  = ARTIFACT["python_code"]
CPP_TEMPLATE:    str  = ARTIFACT["cpp_code"]
JAVA_TEMPLATE:   str  = ARTIFACT["java_code"]
VOTER_CODE:      str  = ARTIFACT["voter_code"]
OUTPUT_SCHEMA:   dict = ARTIFACT["output_schema"]


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder substitution
# ─────────────────────────────────────────────────────────────────────────────

def _substitute(code_template: str, subtotal: float, discount: float) -> str:
    """Substitute runtime inputs into the design-time templates.

    Variants ship with the tokens `__SUBTOTAL__` and `__DISCOUNT__` where the
    real values go; here we do a plain string replace before handing the code
    to the sandbox for compilation / execution.
    """
    return (
        code_template
        .replace("__SUBTOTAL__", repr(float(subtotal)))
        .replace("__DISCOUNT__", repr(float(discount)))
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def run_with_nvp(
    args: tuple,
    timeout: int = 30,
    inject_ccf: bool = True,
    poison_lang: str = "python",
) -> dict:
    """
    Execute pure NVP for one call (paper §3.1).

    Args:
        args        : (subtotal, discount) — inputs to the discount function.
        timeout     : per-variant execution timeout, seconds.
        inject_ccf  : if True, one variant's output is CCF-poisoned AFTER the
                      variant runs but BEFORE the vote. This demonstrates that
                      the voter masks the corruption — the poisoned value is
                      NEVER written to billing.db. Set False for the honest run.
        poison_lang : which variant to poison. Default 'python' for
                      reproducibility. Ignored when inject_ccf is False.

    Returns:
        {
          "steps":            list[dict],       # trace: 3 x VARIANT_*, 1 x VOTE
          "variant_outputs":  {"python":..., "c++":..., "java":...},
          "voter_verdict":    dict | None,      # the majority answer
          "voter_used":       "llm" | "hardcoded" | "none",
          "final_result":     dict | None,      # ≡ voter_verdict on success
          "final_version":    str,              # e.g. "vote (llm)" | "none"
          "poisoned_lang":    str | None,       # which variant was CCF-poisoned
          "success":          bool,             # voter reached consensus
        }
    """
    subtotal, discount = args

    py_code   = _substitute(PYTHON_TEMPLATE, subtotal, discount)
    cpp_code  = _substitute(CPP_TEMPLATE,    subtotal, discount)
    java_code = _substitute(JAVA_TEMPLATE,   subtotal, discount)

    variant_outputs: dict[str, dict | None] = {"python": None, "c++": None, "java": None}

    # ── 1. Fan out to three isolated subprocess sandboxes ─────────────────
    #     (all three run concurrently — the paper's "space-based redundancy")
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(stress_test, py_code,   1, timeout, "python"): "python",
            executor.submit(stress_test, cpp_code,  1, timeout, "c++"):    "c++",
            executor.submit(stress_test, java_code, 1, timeout, "java"):   "java",
        }
        for fut, lang in futures.items():
            try:
                r = fut.result()
                if r["successes"] == 1 and r["sample_outputs"]:
                    variant_outputs[lang] = r["sample_outputs"][0]
            except Exception:
                pass  # variant_outputs[lang] stays None

    # ── 2. Optional CCF injection (post-execution, pre-vote) ──────────────
    poisoned_lang: str | None = None
    if inject_ccf and variant_outputs.get(poison_lang) is not None:
        variant_outputs[poison_lang] = _poison_output(variant_outputs[poison_lang])
        poisoned_lang = poison_lang

    # ── 3. Build a trace entry per variant ────────────────────────────────
    steps: list[dict] = []
    for lang in ("python", "c++", "java"):
        out = variant_outputs[lang]
        if lang == poisoned_lang:
            note = "output CCF-poisoned before the vote — the voter must mask this."
            version = f"{lang} (poisoned)"
        elif out is None:
            note = "variant crashed or timed out."
            version = lang
        else:
            note = "variant returned a valid JSON output."
            version = lang
        steps.append({
            "stage":   f"VARIANT_{lang.upper()}",
            "version": version,
            "result":  out,
            "note":    note,
        })

    # ── 4. Hierarchical voter: LLM-generated → hardcoded majority fallback ─
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

    return {
        "steps":           steps,
        "variant_outputs": variant_outputs,
        "voter_verdict":   voter_verdict,
        "voter_used":      voter_used,
        "final_result":    voter_verdict,
        "final_version":   f"vote ({voter_used})" if voter_verdict is not None else "none",
        "poisoned_lang":   poisoned_lang,
        "success":         voter_verdict is not None,
    }
