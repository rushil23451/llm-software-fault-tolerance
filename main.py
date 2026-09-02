"""
main.py — NVP Pipeline Orchestrator
=====================================

Runs the full NVP pipeline in two explicit phases:
  Phase 1 (Design-Time): design_time()  — code generation, no execution
  Phase 2 (Run-Time)   : run_time()     — execution, voting, verdict

Usage:
    python main.py                              # run 5 HumanEval tasks (default)
    python main.py --samples 10                 # run 10 tasks
    python main.py --samples 5 --timeout 60     # custom timeout
    python main.py --samples 3 --inject-ccf     # enable CCF fault injection

Log files:
    nvp_run.log          — always written; mirrors everything printed to stdout
    nvp_ccf_injected.log — written only when --inject-ccf is passed; contains
                           the same content as nvp_run.log plus a CCF header
"""

from __future__ import annotations

import argparse
import sys
import datetime
from pathlib import Path

from designtime import design_time, NVPArtifact
from runtime2 import run_time, print_verdict


# ══════════════════════════════════════════════════════════════════════════════
# Tee: mirrors stdout to one or more log files simultaneously
# ══════════════════════════════════════════════════════════════════════════════

class _Tee:
    """Wraps sys.stdout so every write goes to the terminal AND to log file(s)."""

    def __init__(self, *log_paths: Path):
        self._stdout = sys.stdout
        self._files  = [p.open("w", encoding="utf-8") for p in log_paths]

    def write(self, data: str) -> int:
        self._stdout.write(data)
        for f in self._files:
            f.write(data)
        return len(data)

    def flush(self) -> None:
        self._stdout.flush()
        for f in self._files:
            f.flush()

    def close(self) -> None:
        sys.stdout = self._stdout
        for f in self._files:
            f.close()


def _setup_logging(inject_ccf: bool) -> tuple[_Tee, list[Path]]:
    """
    Opens log file(s) and installs the Tee on sys.stdout.

    Returns (tee_instance, list_of_log_paths) so the caller can announce them.
    """
    main_log = Path("nvp_run.log")
    log_paths = [main_log]

    if inject_ccf:
        ccf_log = Path("nvp_ccf_injected.log")
        log_paths.append(ccf_log)

    tee = _Tee(*log_paths)
    sys.stdout = tee  # type: ignore[assignment]

    # Write a header into every log file
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_lines = [
        "=" * 72,
        f"  NVP Pipeline Log — {timestamp}",
    ]
    if inject_ccf:
        header_lines.append("  *** CCF FAULT INJECTION ENABLED ***")
    header_lines.append("=" * 72)

    print("\n".join(header_lines))
    print()

    return tee, log_paths


# ══════════════════════════════════════════════════════════════════════════════
# HumanEval prompt builder (kept here so runtime2.py is import-only)
# ══════════════════════════════════════════════════════════════════════════════

def _build_humaneval_prompt(row: dict) -> str:
    entry_point = row.get("entry_point", "solution")
    prompt_text = row.get("prompt", "")
    test_code   = row.get("test", "")

    assert_lines = [
        line.strip()
        for line in test_code.splitlines()
        if line.strip().startswith("assert")
    ][:5]
    test_block = "\n".join(assert_lines) if assert_lines else "(no explicit asserts found)"

    return (
        f"Implement the following algorithm.\n\n"
        f"SPECIFICATION (Python reference — implement the same logic in your assigned language):\n"
        f"{prompt_text}\n\n"
        f"Function / entry-point name: {entry_point}\n\n"
        f"Test cases (extracted from the HumanEval test suite):\n"
        f"{test_block}\n\n"
        f"OUTPUT REQUIREMENT:\n"
        f"Your main() / main function MUST:\n"
        f"  1. Implement the algorithm described above.\n"
        f"  2. Call it with each test case's input (run them one by one).\n"
        f"  3. Output a single JSON object to stdout:\n"
        f'       {{"test_1": <result of test 1>, "test_2": <result of test 2>, ...}}\n\n'
        f"Do NOT use assert statements. Just call the function and collect results."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Main evaluation loop
# ══════════════════════════════════════════════════════════════════════════════

def run_pipeline(samples: int = 5, timeout: int = 30, inject_ccf: bool = False) -> list[dict]:
    """
    Orchestrates the full NVP pipeline for `samples` HumanEval tasks.

    For each task:
      1. Calls design_time()  → generates code artifacts (no execution)
      2. Calls run_time()     → executes variants and votes
    """
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' package not installed.\nRun:  pip install datasets")
        return []

    print("Loading HumanEval dataset …")
    try:
        ds = load_dataset("openai_humaneval", split="test")
    except Exception as e:
        print(f"ERROR loading dataset: {e}")
        return []

    sep = "═" * 72
    all_results: list[dict] = []

    for idx, row in enumerate(ds):
        if idx >= samples:
            break

        task_id     = row.get("task_id", f"HumanEval/{idx}")
        entry_point = row.get("entry_point", "unknown")
        prompt_text = row.get("prompt", "")

        print(f"\n{sep}")
        print(f"  HumanEval  {task_id}  ({idx + 1}/{samples})")
        print(sep)
        print(f"  Function : {entry_point}")
        print(f"  Spec     : {prompt_text[:120].strip()}")
        print()

        task_prompt = _build_humaneval_prompt(row)

        # ── Phase 1: Design-Time ──────────────────────────────────────────────
        print("  Phase 1 — Design-Time …")
        try:
            artifact: NVPArtifact = design_time(task_prompt)
            for line in artifact.log:
                print(f"    {line}")
        except Exception as e:
            print(f"  [ERROR] design_time() failed for {task_id}: {e}")
            continue

        # ── Phase 2: Run-Time ─────────────────────────────────────────────────
        print("\n  Phase 2 — Run-Time …")
        try:
            verdict = run_time(artifact, timeout=timeout, inject_ccf=inject_ccf)
            rt_start = len(artifact.log)
            for line in verdict["log"][rt_start:]:
                print(f"    {line}")
        except Exception as e:
            print(f"  [ERROR] run_time() failed for {task_id}: {e}")
            continue

        verdict["task_id"]     = task_id
        verdict["entry_point"] = entry_point
        all_results.append(verdict)

        print()
        print_verdict(verdict)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  HUMANEVAL EVALUATION COMPLETE — {len(all_results)}/{samples} tasks ran")
    print(sep)
    print(f"  {'Task ID':<22} | {'Consensus':^10} | {'Voter':^10} | {'Variants OK':^12} | Function")
    print("  " + "─" * 72)

    for r in all_results:
        consensus = "✓ YES" if r["is_consensus"] else "✗ NO"
        ok_count  = sum(1 for s in r["variant_status"].values() if s == "ok")
        print(
            f"  {r['task_id']:<22} | {consensus:^10} | {r['voter_used']:^10} | "
            f"{ok_count}/3{' ':^9} | {r['entry_point']}"
        )

    # ── Aggregate metrics ─────────────────────────────────────────────────────
    total = len(all_results)
    if total:
        consensus_count = sum(1 for r in all_results if r["is_consensus"])
        total_variants  = total * 3
        ok_variants     = sum(
            sum(1 for s in r["variant_status"].values() if s == "ok")
            for r in all_results
        )
        fallback_count  = sum(1 for r in all_results if r["voter_used"] == "hardcoded")
        llm_crash_count = sum(
            1 for r in all_results
            if any("[Vote] LLM voter crashed" in line for line in r["log"])
        )

        print(f"\n  AGGREGATE METRICS")
        print("  " + "─" * 40)
        print(f"  Consensus rate      : {consensus_count}/{total}  ({100*consensus_count/total:.1f}%)")
        print(f"  Variant survival    : {ok_variants}/{total_variants}  ({100*ok_variants/total_variants:.1f}%)")
        print(f"  LLM voter crashes   : {llm_crash_count}/{total}  ({100*llm_crash_count/total:.1f}%)")
        print(f"  Hardcoded fallbacks : {fallback_count}/{total}  ({100*fallback_count/total:.1f}%)")

    return all_results


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NVP Pipeline — Design-Time → Run-Time Orchestrator"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Number of HumanEval tasks to evaluate (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Per-variant execution timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--inject-ccf",
        action="store_true",
        help="Randomly poison one variant's output to verify the voter detects and overrules it.",
    )
    args = parser.parse_args()

    # ── Set up logging before any pipeline output ─────────────────────────────
    tee, log_paths = _setup_logging(args.inject_ccf)

    try:
        run_pipeline(
            samples=args.samples,
            timeout=args.timeout,
            inject_ccf=args.inject_ccf,
        )
    finally:
        # Always flush + close log files, even on crash
        print()
        print("─" * 72)
        for p in log_paths:
            print(f"  Log written → {p.resolve()}")
        print("─" * 72)
        tee.close()