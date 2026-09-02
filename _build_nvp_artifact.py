"""
_build_nvp_artifact.py — Builds the pre-generated NVP artifact for the billing demo.

This is the offline analogue of design_time_nvp.py: it writes exactly the same
JSON schema NVPArtifact.save_to_json() produces, but with hand-crafted variants
so the demo runs with no API key.

Regeneration path: run design_time_nvp.py with GROQ_API_KEY set to get freshly
LLM-generated variants (which will overwrite this file).
"""

import json
import textwrap
from pathlib import Path

ARTIFACT_PATH = Path(__file__).parent / "artifact_nvp_calculate_discounted_total.json"

# ── The task, verbatim from design_time_nvp.py ──────────────────────────────
TASK = textwrap.dedent("""\
    Implement `calculate_discounted_total(subtotal, discount) -> float`.

    Return the order total after applying a fractional discount to the subtotal
    (discount=0.1 means 10 % off). Round the result to 2 decimal places.

      calculate_discounted_total(100.0, 0.10) -> 90.0
      calculate_discounted_total(0.0,   0.50) -> 0.0
      calculate_discounted_total(47.97, 0.10) -> 43.17

    RUNTIME INPUT CONVENTION:
    The values of subtotal and discount are NOT known at design time. Each
    variant MUST include two placeholder tokens `__SUBTOTAL__` and
    `__DISCOUNT__` where these numbers would go. At runtime, the harness
    textually substitutes these tokens for the real numeric values before
    compilation / execution.

    OUTPUT: emit a single JSON object to stdout: {"total": <number>}.
""")

OUTPUT_SCHEMA = {
    "description": "The order total after applying the discount, rounded to 2 decimal places.",
    "keys": [
        {
            "name": "total",
            "type": "float",
            "description": "The discounted total (subtotal minus the discount amount).",
        }
    ],
    "example": {"total": 90.0},
}

APPROACHES = [
    {
        "id": "1",
        "name": "Direct multiplicative form",
        "strategy": (
            "Compute the retained fraction R = 1 - discount, then multiply the "
            "subtotal by R in a single expression. Round the result to 2 decimals."
        ),
    },
    {
        "id": "2",
        "name": "Subtractive form",
        "strategy": (
            "Compute the discount amount D = subtotal * discount as a distinct "
            "intermediate, then return subtotal - D. Round the result to 2 decimals."
        ),
    },
    {
        "id": "3",
        "name": "Integer cent arithmetic",
        "strategy": (
            "Convert the subtotal to integer cents, apply the retained fraction, "
            "round to the nearest cent, then convert back to dollars. Avoids "
            "intermediate binary-float accumulation entirely."
        ),
    },
]

# ── Python variant — Strategy 1 (direct multiplicative form) ────────────────
PYTHON_CODE = textwrap.dedent("""\
    SUBTOTAL = __SUBTOTAL__
    DISCOUNT = __DISCOUNT__

    def main():
        # Strategy 1: direct multiplicative form.
        # retained_fraction = 1 - discount, then subtotal * retained_fraction.
        retained_fraction = 1.0 - DISCOUNT
        total = SUBTOTAL * retained_fraction
        return {"total": round(total, 2)}
""")

# ── C++ variant — Strategy 2 (subtractive form) ─────────────────────────────
CPP_CODE = textwrap.dedent("""\
    #include <iostream>
    #include <iomanip>
    #include <sstream>
    #include <cmath>

    int main() {
        double subtotal = __SUBTOTAL__;
        double discount = __DISCOUNT__;

        // Strategy 2: subtractive form.
        double discount_amount = subtotal * discount;
        double net_total       = subtotal - discount_amount;

        // Round to 2 decimal places.
        double rounded = std::round(net_total * 100.0) / 100.0;

        std::ostringstream ss;
        ss << "{\\"total\\": "
           << std::defaultfloat << std::setprecision(15) << rounded
           << "}";
        std::cout << ss.str();
        return 0;
    }
""")

# ── Java variant — Strategy 3 (integer cent arithmetic) ─────────────────────
JAVA_CODE = textwrap.dedent("""\
    public class Main {
        public static void main(String[] args) {
            double subtotal = __SUBTOTAL__;
            double discount = __DISCOUNT__;

            // Strategy 3: integer cent arithmetic.
            long subtotal_cents  = Math.round(subtotal * 100.0);
            double retained_frac = 1.0 - discount;
            long final_cents     = Math.round(subtotal_cents * retained_frac);
            double total         = final_cents / 100.0;

            String total_str;
            if (total == Math.floor(total)) {
                total_str = String.format("%.1f", total);
            } else {
                total_str = String.valueOf(total);
            }
            System.out.println("{\\"total\\": " + total_str + "}");
        }
    }
""")

# ── Voter — pure-Python majority voter with 1e-4 float tolerance ────────────
VOTER_CODE = textwrap.dedent('''\
    def voter(out1, out2, out3):
        """Majority voter for {"total": float} NVP outputs.

        - Treats None inputs as absent (variant crashed or timed out).
        - Compares numeric values with 1e-4 absolute tolerance.
        - Returns the agreed dict if >=2 non-None outputs match, else None.
        """
        def _values_close(a, b, tol=1e-4):
            if a is None or b is None:
                return a is None and b is None
            if isinstance(a, bool) or isinstance(b, bool):
                return a == b
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                try:
                    return abs(float(a) - float(b)) <= tol
                except (TypeError, ValueError, OverflowError):
                    return False
            return a == b

        def _dicts_agree(o1, o2):
            if not (isinstance(o1, dict) and isinstance(o2, dict)):
                return False
            if set(o1.keys()) != set(o2.keys()):
                return False
            for k in o1:
                if not _values_close(o1[k], o2[k]):
                    return False
            return True

        candidates = [out1, out2, out3]
        live = [o for o in candidates if isinstance(o, dict)]
        if len(live) < 2:
            return None

        for i in range(len(live)):
            for j in range(i + 1, len(live)):
                if _dicts_agree(live[i], live[j]):
                    return live[i]

        return None
''')

ARTIFACT = {
    "task": TASK,
    "output_schema": OUTPUT_SCHEMA,
    "approaches": APPROACHES,
    "python_code": PYTHON_CODE,
    "cpp_code": CPP_CODE,
    "java_code": JAVA_CODE,
    "voter_code": VOTER_CODE,
    "log": [
        "[Design-Time] pre-generated (hand-crafted) NVP artifact for the billing demo.",
        "[Design-Time] Approaches: ['Direct multiplicative form', 'Subtractive form', 'Integer cent arithmetic']",
        "[Design-Time] Schema: ['total']",
        "[Design-Time] Placeholders __SUBTOTAL__ and __DISCOUNT__ substituted at runtime.",
    ],
}

with open(ARTIFACT_PATH, "w", encoding="utf-8") as f:
    json.dump(ARTIFACT, f, indent=2)
print(f"[BUILD] wrote {ARTIFACT_PATH}")
