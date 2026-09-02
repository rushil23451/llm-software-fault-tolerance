"""
runtime2.py — Phase 2: Execution + Voting  (library module)
=============================================================

Executes the three generated variants, runs the voter, and returns a
structured verdict dict.  This module is a pure library — it has no
orchestration logic and no __main__ block.

Public API:
    from runtime2 import run_time, print_verdict
    verdict = run_time(artifact, timeout=30, inject_ccf=False)
"""

from __future__ import annotations

import json
import re
from typing import Any

from sandbox import stress_test
from designtime import NVPArtifact


def _poison_output(output: dict | None) -> dict | None:
    """
    Output-level CCF injection — operates on the already-executed, parsed dict.
    """
    if output is None:
        return None

    def _flip(v):
        if isinstance(v, bool):           return not v
        if isinstance(v, int):             return -999
        if isinstance(v, float):           return -999.0
        if isinstance(v, str):             return "__CCF_POISON__"
        if isinstance(v, list):            return []
        if isinstance(v, dict):            return {k: _flip(vv) for k, vv in v.items()}
        return v

    return {k: _flip(v) for k, v in output.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Run-Time internals
# ══════════════════════════════════════════════════════════════════════════════

_SENTINEL_LLM_FAILED = object()


def _execute_variant(
    code: str,
    language: str,
    variant_id: str,
    log: list[str],
    timeout: int = 30,
) -> dict | None:
    log.append(f"[Run-Time] Executing variant {variant_id} ({language}) …")
    try:
        result = stress_test(code, runs=1, timeout=timeout, language=language)
    except Exception as e:
        log.append(f"[Run-Time] Variant {variant_id}: sandbox raised exception — {e}")
        return None

    if result["successes"] == 1 and result["sample_outputs"]:
        output = result["sample_outputs"][0]
        log.append(f"[Run-Time] Variant {variant_id}: SUCCESS — {str(output)[:120]}")
        return output
    else:
        err = result["errors"][0] if result["errors"] else "unknown error"
        log.append(f"[Run-Time] Variant {variant_id}: FAILED — {err[:120]}")
        return None


def _unwrap_schema_key(obj: Any) -> Any:
    """
    Unwrap single-key dicts whose sole value is itself a dict.

    The Manager sometimes wraps all test results under a schema key, e.g.:
        {'below_zero': {'test_1': 0, 'test_2': 0, ...}}
    while another variant emits the inner dict directly:
        {'test_1': 0, 'test_2': 0, ...}

    Both are semantically identical, but dict equality fails because the
    top-level keys differ.  Repeatedly unwrap until stable so we handle
    double-wrapping too.
    """
    while isinstance(obj, dict) and len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict):
            obj = inner
        else:
            break
    return obj


def _normalise_for_voter(obj):
    """
    Recursively normalise an output dict before handing it to either voter.

    Steps applied in order:
      1. Unwrap schema-key wrapping  e.g. {'below_zero': {...}} → {...}
      2. bool → int                  True→1 / False→0
      3. Recurse into dicts and lists

    Normalising bool → int prevents TypeError crashes inside LLM-generated
    voter code that does arithmetic on output values, and ensures the
    hardcoded voter's equality check is not confused by True == 1 at the
    Python level but "true" vs "1" at the JSON/string level.
    """
    obj = _unwrap_schema_key(obj)

    if obj is None:
        return None
    if isinstance(obj, bool):
        return int(obj)          # True→1, False→0  (must come before int check)
    if isinstance(obj, dict):
        return {k: _normalise_for_voter(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalise_for_voter(x) for x in obj]
    return obj


def _llm_vote(voter_code: str, out1, out2, out3, log: list[str]):
    log.append("[Vote] Attempting LLM voter …")
    try:
        import math as _math

        _safe_builtins = {
            "abs": abs, "round": round, "len": len, "min": min, "max": max,
            "sum": sum, "sorted": sorted, "reversed": reversed,
            "list": list, "dict": dict, "tuple": tuple, "set": set,
            "int": int, "float": float, "str": str, "bool": bool,
            "isinstance": isinstance, "zip": zip, "enumerate": enumerate,
            "range": range, "all": all, "any": any,
            "None": None, "True": True, "False": False,
            "print": print,
        }
        namespace: dict = {
            "math": _math,
            "json": json,
            "__builtins__": _safe_builtins,
        }
        exec(compile(voter_code, "<llm_voter>", "exec"), namespace)  # noqa: S102
        voter_fn = namespace.get("voter")
        if not callable(voter_fn):
            raise ValueError("voter() function not found in compiled namespace.")

        # Unwrap schema keys and normalise bool/int before passing to the
        # generated voter so structural mismatches don't hide true consensus.
        n1, n2, n3 = (_normalise_for_voter(o) for o in (out1, out2, out3))

        result = voter_fn(n1, n2, n3)

        if result is not None:
            log.append(f"[Vote] LLM voter reached consensus: {str(result)[:120]}")
        else:
            log.append("[Vote] LLM voter returned None (no consensus).")
        return result
    except Exception as e:
        etype = type(e).__name__
        if isinstance(e, TypeError):
            reason = f"{etype}: unsupported operation on output values."
        elif isinstance(e, KeyError):
            reason = f"{etype}: accessed a key that doesn't exist in one of the outputs."
        elif isinstance(e, (NameError, AttributeError)):
            reason = f"{etype}: referenced an undefined name or attribute."
        elif isinstance(e, SyntaxError):
            reason = f"{etype}: the generated voter has a syntax error."
        else:
            reason = f"{etype}: {e}"
        log.append(f"[Vote] LLM voter crashed — {reason}. Falling back to hardcoded voter.")
        return _SENTINEL_LLM_FAILED


def _canonicalize(obj: Any) -> Any:
    """
    Normalise an output value for comparison in the hardcoded voter.

    Float tolerance: values are rounded to 4 decimal places (≡ 1e-4 absolute
    tolerance), which matches the tolerance used in the LLM-generated voter.
    """
    if isinstance(obj, float):
        return int(obj) if obj.is_integer() else round(obj, 4)
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canonicalize(x) for x in obj]
    return obj


def _hardcoded_vote(out1, out2, out3, log: list[str]) -> dict | None:
    log.append("[Vote] Running hardcoded majority voter …")

    # Unwrap schema keys and normalise bool→int so that structural differences
    # introduced by the Manager's schema naming don't prevent a true consensus
    # from being detected.
    norm_outputs = [
        (i + 1, _normalise_for_voter(o))
        for i, o in enumerate([out1, out2, out3])
        if o is not None
    ]

    if len(norm_outputs) < 2:
        log.append("[Vote] Fewer than 2 variants succeeded — no consensus possible.")
        return None

    canon = [(vid, _canonicalize(o)) for vid, o in norm_outputs]
    for i in range(len(canon)):
        for j in range(i + 1, len(canon)):
            if canon[i][1] == canon[j][1]:
                winners = [canon[i][0], canon[j][0]]
                log.append(f"[Vote] Hardcoded voter — majority: variants {winners}")
                return norm_outputs[i][1]

    log.append("[Vote] Hardcoded voter — all outputs differ. No consensus.")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def run_time(artifact: NVPArtifact, timeout: int = 30, inject_ccf: bool = False) -> dict:
    """
    Phase 2: Execute the three variants once each and vote.

    Returns a structured verdict dict:
    {
        "task":             str,
        "final_answer":     dict | None,
        "is_consensus":     bool,
        "voter_used":       "llm" | "hardcoded" | "none",
        "variant_outputs":  {"python": ..., "c++": ..., "java": ...},
        "variant_status":   {"python": "ok"|"failed", ...},
        "verdict":          str,
        "log":              list[str],
    }
    """
    log = list(artifact.log)

    import concurrent.futures

    log_py, log_cpp, log_java = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f_py   = executor.submit(_execute_variant, artifact.python_code, "python", "1-PY", log_py, timeout)
        f_cpp  = executor.submit(_execute_variant, artifact.cpp_code, "c++", "2-CPP", log_cpp, timeout)
        f_java = executor.submit(_execute_variant, artifact.java_code, "java", "3-JV", log_java, timeout)

        out_py   = f_py.result()
        out_cpp  = f_cpp.result()
        out_java = f_java.result()

    log.extend(log_py)
    log.extend(log_cpp)
    log.extend(log_java)

    ccf_poisoned_lang: str | None = None
    if inject_ccf:
        import random as _random
        candidates = [
            ("python", out_py),
            ("c++",    out_cpp),
            ("java",   out_java),
        ]
        live_candidates = [(lang, out) for lang, out in candidates if out is not None]
        if live_candidates:
            target_lang, target_out = _random.choice(live_candidates)
            poisoned = _poison_output(target_out)
            log.append(
                f"[CCF]  Output-level fault injected into {target_lang} variant (chosen randomly).\n"
                f"       Original : {str(target_out)[:120]}\n"
                f"       Poisoned : {str(poisoned)[:120]}\n"
                f"       Voter must overrule this."
            )
            if target_lang == "python":
                out_py   = poisoned
            elif target_lang == "c++":
                out_cpp  = poisoned
            else:
                out_java = poisoned
            ccf_poisoned_lang = target_lang
        else:
            log.append("[CCF]  All variants failed — no output to poison (CCF not observable).")

    variant_outputs = {"python": out_py, "c++": out_cpp, "java": out_java}

    def _classify(lang, output):
        if lang == ccf_poisoned_lang:
            return "poisoned"
        return "ok" if output is not None else "failed"

    variant_status = {lang: _classify(lang, o) for lang, o in variant_outputs.items()}

    llm_result = _llm_vote(artifact.voter_code, out_py, out_cpp, out_java, log)

    if llm_result is _SENTINEL_LLM_FAILED:
        log.append("[Vote] LLM voter crashed — falling back to hardcoded voter.")
        final_answer = _hardcoded_vote(out_py, out_cpp, out_java, log)
        voter_used   = "hardcoded" if final_answer is not None else "none"
    elif llm_result is None:
        log.append("[Vote] LLM voter found no consensus — trying hardcoded voter.")
        final_answer = _hardcoded_vote(out_py, out_cpp, out_java, log)
        voter_used   = "hardcoded" if final_answer is not None else "none"
    else:
        final_answer = llm_result
        voter_used   = "llm"

    is_consensus = final_answer is not None
    succeeded = [lang for lang, s in variant_status.items() if s == "ok"]
    failed    = [lang for lang, s in variant_status.items() if s == "failed"]
    poisoned  = [lang for lang, s in variant_status.items() if s == "poisoned"]

    if is_consensus:
        verdict = (
            f"CONSENSUS via {voter_used} voter. "
            f"Successful variants: {succeeded}. "
            f"Failed variants: {failed if failed else 'none'}. "
            + (f"CCF-poisoned variants: {poisoned}. " if poisoned else "")
            + f"Answer: {str(final_answer)[:200]}"
        )
    else:
        verdict = (
            f"NO CONSENSUS. "
            f"Successful variants: {succeeded}. "
            f"Failed variants: {failed if failed else 'none'}. "
            + (f"CCF-poisoned variants: {poisoned}. " if poisoned else "")
            + f"Outputs differ or too few variants ran."
        )

    log.append(f"[Run-Time] Verdict: {verdict}")

    return {
        "task":            artifact.task,
        "final_answer":    final_answer,
        "is_consensus":    is_consensus,
        "voter_used":      voter_used,
        "variant_outputs": variant_outputs,
        "variant_status":  variant_status,
        "verdict":         verdict,
        "ccf_injected":    inject_ccf,
        "log":             log,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Pretty-printer
# ══════════════════════════════════════════════════════════════════════════════

def print_verdict(verdict: dict) -> None:
    sep = "═" * 72
    print(sep)
    print(f"  NVP VERDICT")
    print(sep)
    print(f"  Task    : {verdict['task'][:120]}")
    print(f"  Result  : {'✓ CONSENSUS' if verdict['is_consensus'] else '✗ NO CONSENSUS'}")
    print(f"  Voter   : {verdict['voter_used']}")
    print(f"  Answer  : {verdict['final_answer']}")
    print()
    print("  Variant status:")
    for lang, status in verdict["variant_status"].items():
        if status == "ok":
            icon = "✓"
        elif status == "poisoned":
            icon = "⚠"
        else:
            icon = "✗"
        out  = verdict["variant_outputs"][lang]
        print(f"    {icon} {lang:<8} {status:<8}  {str(out)[:80] if out else '—'}")
    print()
    print(f"  {verdict['verdict']}")
    print(sep)