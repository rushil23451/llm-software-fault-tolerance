"""
design_time_nvp.py — NVP equivalent of design_time.py (from the RB demo).

Runs ONCE, offline. Calls the existing NVP design-time pipeline
(designtime.design_time) with a task string pinned to the billing spec, then
freezes the resulting NVPArtifact into artifact_nvp_calculate_discounted_total.json.

At runtime, nvp_engine.py loads this artifact and never touches the LLM.

A pre-generated artifact already ships with this demo (built by
_build_nvp_artifact.py) so it runs with no API key. Re-run this file with
GROQ_API_KEY set to regenerate the three variants + voter live.

Usage:
    GROQ_API_KEY=gsk_... python design_time_nvp.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from designtime import design_time

HERE = Path(__file__).parent
ARTIFACT_PATH = HERE / "artifact_nvp_calculate_discounted_total.json"


# The task string handed to the Manager. Two crucial additions vs. a plain
# HumanEval prompt:
#   1. The Manager's schema will be pinned to {"total": float}.
#   2. Every generator must leave two textual placeholders — __SUBTOTAL__
#      and __DISCOUNT__ — in the code so the runtime engine can substitute
#      the real values before compilation / execution. This is what lets a
#      single design-time artifact serve every cart the demo processes.
TASK = textwrap.dedent("""\
    Implement `calculate_discounted_total(subtotal, discount) -> float`.

    Return the order total after applying a fractional discount to the subtotal
    (discount=0.1 means 10 % off). Round the result to 2 decimal places.

      calculate_discounted_total(100.0, 0.10) -> 90.0
      calculate_discounted_total(0.0,   0.50) -> 0.0
      calculate_discounted_total(47.97, 0.10) -> 43.17

    OUTPUT SCHEMA (all three variants MUST agree on this):
      {"total": <float>}

    RUNTIME INPUT CONVENTION — READ CAREFULLY:
    The values of `subtotal` and `discount` are NOT known at design time.
    Each variant MUST include two placeholder tokens spelled EXACTLY as:

        __SUBTOTAL__
        __DISCOUNT__

    where these numbers would normally appear. At runtime the harness will
    textually substitute these tokens for real numeric literals BEFORE
    compilation / execution. Concrete requirements:

      * Python variant: define two module-level constants at the top of the
        file, before `def main()`:
            SUBTOTAL = __SUBTOTAL__
            DISCOUNT = __DISCOUNT__

      * C++ variant: define them at the top of `int main()`:
            double subtotal = __SUBTOTAL__;
            double discount = __DISCOUNT__;

      * Java variant: define them at the top of `public static void main`:
            double subtotal = __SUBTOTAL__;
            double discount = __DISCOUNT__;

    Do NOT read from argv / stdin / files / env vars. The tokens above are
    the only input mechanism.

    OUTPUT: emit exactly one JSON object to stdout: {"total": <number>}.
""")


def main() -> None:
    print("[NVP design-time] regenerating billing artifact via the LLM pipeline …")
    artifact = design_time(TASK)

    # Persist using NVPArtifact's own serializer so the JSON layout matches
    # what nvp_engine.py (and NVPArtifact.load_from_json) already expect.
    artifact.save_to_json(str(ARTIFACT_PATH))

    print()
    print("[NVP design-time] wrote", ARTIFACT_PATH)
    print("[NVP design-time] approaches:")
    for a in artifact.approaches:
        print(f"    - {a['name']}: {a['strategy'][:100]}")
    print("[NVP design-time] output schema keys:",
          [k["name"] for k in artifact.output_schema["keys"]])
    print()
    print("[NVP design-time] IMPORTANT: verify the generated variants contain")
    print("  __SUBTOTAL__ and __DISCOUNT__ placeholder tokens. If they don't,")
    print("  re-run this script — the LLM occasionally hard-codes the example")
    print("  inputs. nvp_engine.py cannot substitute values without them.")

    for lang, code in [("python", artifact.python_code),
                       ("c++",    artifact.cpp_code),
                       ("java",   artifact.java_code)]:
        has_sub  = "__SUBTOTAL__" in code
        has_disc = "__DISCOUNT__" in code
        mark = "✓" if (has_sub and has_disc) else "✗"
        print(f"    {mark} {lang:<7}  __SUBTOTAL__={has_sub}  __DISCOUNT__={has_disc}")


if __name__ == "__main__":
    main()
