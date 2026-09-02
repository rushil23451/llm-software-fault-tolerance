"""
designtime.py — Phase 1: Code Generation (NO execution)
========================================================

Generates three variant implementations (Python, C++, Java) plus a custom
LLM voter function for a given task, then freezes everything into an
NVPArtifact.

This module is a pure library — it has no __main__ block.
It is called by main.py as Phase 1 of the NVP pipeline.

Public API:
    from designtime import design_time, NVPArtifact
    artifact = design_time("some task description")
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field

from models import generate


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

def _strip_think_blocks(raw: str) -> str:
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


def _strip_json_fences(raw: str) -> str:
    raw = _strip_think_blocks(raw)
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _strip_code_fences(raw: str) -> str:
    raw = _strip_think_blocks(raw)
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _parse_json(raw: str, context: str) -> dict:
    raw = re.sub(r"<tool_call>.*?</tool_call>", "", raw, flags=re.DOTALL)
    cleaned = _strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"[{context}] JSON parse failed.\nError: {e}\nRaw (first 600 chars):\n{raw[:600]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# NVPArtifact — the immutable Design-Time output
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class NVPArtifact:
    """All assets produced during Design-Time. Nothing is executed here."""

    task: str

    output_schema: dict
    approaches: list[dict]

    python_code: str
    cpp_code:    str
    java_code:   str

    voter_code:  str

    log: list[str] = field(default_factory=list)

    def save_to_json(self, path: str) -> None:
        import dataclasses
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(self), f, indent=2)
        print(f"[NVPArtifact] Saved to {path}")

    @classmethod
    def load_from_json(cls, path: str) -> "NVPArtifact":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Design-Time internals
# ══════════════════════════════════════════════════════════════════════════════

_MANAGER_SYSTEM = """\
You are a software architect designing an N-Version Programming experiment.
Specify THREE fundamentally different algorithmic strategies to solve the given task.
These strategies must be language-agnostic (do not mention specific languages).

You MUST also define a strict JSON output schema that ALL implementations must follow.
The schema specifies exact key names and value types the program must print to stdout.

Return ONLY a JSON object — no markdown, no code fences:
{
  "output_schema": {
    "description": "one sentence describing the output",
    "keys": [
      {"name": "exact_key_name", "type": "int|float|str|list|dict", "description": "..."}
    ],
    "example": {"key1": <value>, ...}
  },
  "approaches": [
    {"id": "1", "name": "short name", "strategy": "detailed step-by-step how-to"},
    {"id": "2", "name": "short name", "strategy": "detailed step-by-step how-to"},
    {"id": "3", "name": "short name", "strategy": "detailed step-by-step how-to"}
  ]
}

OUTPUT SCHEMA RULES:
- Key names must be snake_case, identical across all three implementations.
- Types: int, float, str, list, dict only.
- The example must be a valid concrete example matching the schema exactly.
- If the output is a mapping (e.g. word counts), wrap it in a named "result" key of type dict.
"""

def _run_manager(task: str, log: list[str]) -> tuple[dict, list[dict]]:
    log.append("[Manager] Generating strategies and output schema …")
    raw = generate(role="manager", system=_MANAGER_SYSTEM, user=f"Task: {task}", max_tokens=1500)
    result = _parse_json(raw, context="Manager")

    output_schema = result.get("output_schema")
    approaches    = result.get("approaches")

    if not output_schema or not isinstance(output_schema, dict):
        raise ValueError("[Manager] Response missing 'output_schema' key or it is not a dict.")
    if not approaches or not isinstance(approaches, list):
        raise ValueError("[Manager] Response missing 'approaches' key or it is not a list.")
    if len(approaches) < 3:
        raise ValueError(
            f"[Manager] Expected 3 approaches, got {len(approaches)}. "
            "Raw response (first 400 chars):\n" + raw[:400]
        )

    log.append(f"[Manager] Schema: {list(k['name'] for k in output_schema['keys'])}")
    log.append(f"[Manager] Approaches: {[a['name'] for a in approaches]}")
    return output_schema, approaches


_GENERATOR_BASE = """\
You are an expert programmer implementing one variant in an N-Version Programming experiment.
Write a COMPLETE, SELF-CONTAINED solution using ONLY the standard library.

UNIVERSAL RULES:
- Standard library ONLY. Zero third-party packages.
- Use a FIXED SEED (e.g. 42) for any randomness so output is deterministic.
- The program MUST print a single valid JSON object to stdout as its ONLY output line.
- NO debug prints, NO progress messages — only the final JSON result.
- Handle edge cases gracefully.
"""

_PYTHON_RULES = """\
PYTHON-SPECIFIC RULES:
- Define a `main()` function that computes the result and returns it as a plain Python dict.
- `main()` MUST be a regular synchronous function: `def main()` — NOT `async def main()`.
- DO NOT add an `if __name__ == '__main__':` block — the test harness adds its own.
- No `print()` calls inside `main()`. The harness prints.
- Return ONLY the Python source code — no fences, no explanation.
"""

_CPP_RULES = """\
C++-SPECIFIC RULES:
- Use a single file with a standard `int main()` entry point.
- Use ONLY the C++ standard library (no Boost, no third-party headers).
- Include ONLY headers you actually use: <iostream>, <vector>, <cmath>,
  <algorithm>, <sstream>, <iomanip>, <string>, <numeric>, <map>, etc.
- Output valid JSON by constructing the string manually via std::cout
  or std::ostringstream. Do NOT rely on any JSON library.

JSON FORMATTING — THESE ARE HARD RULES, VIOLATIONS WILL CAUSE RUNTIME FAILURE:
- NEVER place a comma after the last key-value pair in an object. Every comma
  must be followed by another key-value pair, not a closing brace.
  WRONG:  {"a": 1, "b": 2, }
  RIGHT:  {"a": 1, "b": 2}
- When building JSON in a loop, use a separator pattern: print the comma
  BEFORE each item except the first (use a bool `first = true` flag), rather
  than after each item. This is the only reliable way to avoid trailing commas.
- For floating-point values use std::defaultfloat (NOT std::fixed) with
  std::setprecision(15). This avoids unnecessary trailing zeros and never
  produces output like "0.500000000000000" — it will produce "0.5" instead.
- ALL string values in the JSON must be enclosed in double-quotes that are
  part of the JSON string, not part of C++ string delimiters. Always write
  them as: ss << "\\"" << value << "\\"";
- The only output to stdout must be a single line containing the JSON object.
  No newlines inside the JSON, no trailing newline after the closing brace
  is required but is acceptable.

- Compile target: C++17. You may use structured bindings, std::optional,
  range-based for loops, etc.
- Return ONLY the C++ source code — no fences, no explanation.
"""

_JAVA_RULES = """\
JAVA-SPECIFIC RULES:
- Use a single public class named `Main` with a `public static void main(String[] args)` entry point.
- Use ONLY `java.*` / `javax.*` standard library packages.
- Build the JSON string manually with StringBuilder (no third-party JSON libraries).
- Return ONLY the Java source code — no fences, no explanation.
"""

def _format_schema_contract(output_schema: dict) -> str:
    keys_desc = "\n".join(
        f'  - "{k["name"]}": {k["type"]}  # {k["description"]}'
        for k in output_schema.get("keys", [])
    )
    example = output_schema.get("example", {})
    return (
        f"\n\nOUTPUT CONTRACT — YOU MUST FOLLOW THIS EXACTLY:\n"
        f"Description: {output_schema.get('description', '')}\n"
        f"Your program MUST print a JSON object with EXACTLY these keys:\n"
        f"{keys_desc}\n"
        f"Example of a valid output: {json.dumps(example)}\n"
        f"ANY deviation in key names, nesting, or structure is a CONTRACT VIOLATION.\n"
        f"Do NOT add extra keys. Do NOT rename keys. Do NOT change nesting.\n"
    )


def _run_generators(
    task: str,
    output_schema: dict,
    approaches: list[dict],
    log: list[str],
) -> tuple[str, str, str]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    schema_contract = _format_schema_contract(output_schema)
    lang_configs = [
        ("generator_A", "Python", _PYTHON_RULES, approaches[0]),
        ("generator_B", "C++",    _CPP_RULES,    approaches[1]),
        ("generator_C", "Java",   _JAVA_RULES,   approaches[2]),
    ]

    def _build_prompt(lang, lang_rules, approach, others):
        other_strategies = "\n".join(
            f"  - [{o['name']}]: {o['strategy'][:200]}" for o in others
        )
        system = _GENERATOR_BASE + lang_rules
        user = (
            f"Task: {task}\n\n"
            f"Your assigned strategy (approach {approach['id']}):\n"
            f"Name: {approach['name']}\n"
            f"Strategy: {approach['strategy']}"
            f"{schema_contract}"
            f"\n\nOTHER STRATEGIES BEING IMPLEMENTED IN PARALLEL (do NOT replicate these):\n"
            f"{other_strategies}\n"
            f"Your implementation must be architecturally distinct from the above."
        )
        return system, user

    def _generate_one(role, lang, lang_rules, approach, others):
        system, user = _build_prompt(lang, lang_rules, approach, others)
        raw = generate(role=role, system=system, user=user, max_tokens=2500)
        return lang, _strip_code_fences(raw)

    log.append("[Generators] Launching Python, C++, Java variants in parallel …")
    results: dict[str, str] = {}
    futures_map = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        for role, lang, lang_rules, approach in lang_configs:
            others = [a for _, _, _, a in lang_configs if a["id"] != approach["id"]]
            future = executor.submit(_generate_one, role, lang, lang_rules, approach, others)
            futures_map[future] = lang

        for future in as_completed(futures_map):
            lang_done = futures_map[future]
            try:
                returned_lang, code = future.result()
                if not code or not code.strip():
                    raise ValueError("Generator returned empty code.")
                results[returned_lang] = code
                log.append(f"[Generator {returned_lang}] Done — {len(code)} chars.")
            except Exception as e:
                log.append(f"[Generator {lang_done}] ERROR — {e}")
                results[lang_done] = f"# Generation failed: {e}"

    return results["Python"], results["C++"], results["Java"]


_VOTER_SYSTEM = """\
You are writing a Python voter function for an N-Version Programming system.

The function signature is:
    def voter(out1, out2, out3):
        ...
        return result  # or None if no consensus

Each argument is either:
  - A Python dict (the parsed JSON output from a variant), or
  - None (the variant crashed or timed out).

Rules for the voter:
1. Ignore None inputs entirely — treat them as absent, not as wrong answers.
2. Use approximate equality (tolerance ≤ 1e-4) when comparing numeric values.
3. If two or more non-None outputs agree (majority), return that agreed-upon output as a dict.
4. If no majority exists (all differ, or fewer than 2 non-None inputs), return None.
5. When comparing dicts, compare them key-by-key using the known schema.
6. The function must be pure Python, no imports beyond the standard library.
7. You MAY use `import math` or `import json` inside the function if needed.

Return ONLY the Python source code of the `voter` function — no fences, no explanation, no class.
The code must start with `def voter(out1, out2, out3):`.
"""

def _run_voter_generator(task: str, output_schema: dict, log: list[str]) -> str:
    log.append("[VoterGen] Generating LLM voter function …")
    user = (
        f"Task: {task}\n\n"
        f"Output schema the variants must follow:\n{json.dumps(output_schema, indent=2)}\n\n"
        f"Write a `voter(out1, out2, out3)` function as described."
    )
    raw = generate(role="generator_A", system=_VOTER_SYSTEM, user=user, max_tokens=1000)
    voter_code = _strip_code_fences(raw)

    if "def voter(" not in voter_code:
        log.append("[VoterGen] WARNING — generated code does not define voter(). Using placeholder.")
        voter_code = textwrap.dedent("""\
            def voter(out1, out2, out3):
                return None
        """)
    else:
        log.append(f"[VoterGen] Done — {len(voter_code)} chars.")

    return voter_code


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def design_time(task: str) -> NVPArtifact:
    """
    Phase 1: Generate all code assets. No execution happens here.
    Returns an NVPArtifact ready to be passed to run_time().
    """
    log: list[str] = []
    log.append(f"[Design-Time] Task: {task}")

    output_schema, approaches = _run_manager(task, log)
    python_code, cpp_code, java_code = _run_generators(task, output_schema, approaches, log)
    voter_code = _run_voter_generator(task, output_schema, log)

    log.append("[Design-Time] ✓ All assets generated. Ready for Run-Time.")

    return NVPArtifact(
        task=task,
        output_schema=output_schema,
        approaches=approaches,
        python_code=python_code,
        cpp_code=cpp_code,
        java_code=java_code,
        voter_code=voter_code,
        log=log,
    )