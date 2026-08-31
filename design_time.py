"""
design_time.py — Runs ONCE, offline, before deployment. Calls the LLM 4×:
  primary -> alt-1 -> alt-2 -> acceptance test, validates each, and writes
  artifact_calculate_discounted_total.json.

The runtime (app.py / recovery_engine.py) never calls the LLM — it only loads
this artifact. A pre-generated artifact already ships in this folder so the PoC
runs without an API key; re-run this file with GROQ_API_KEY set to regenerate.

Usage:  GROQ_API_KEY=sk-... python design_time.py
"""

import json, os, re, time, textwrap, requests

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
ARTIFACT_OUT = "artifact_calculate_discounted_total.json"

SPEC = textwrap.dedent('''\
    def calculate_discounted_total(subtotal: float, discount: float) -> float:
        """
        Return the order total after applying a fractional discount to the subtotal.
        discount=0.1 means 10% off. Round the result to 2 decimal places.

        >>> calculate_discounted_total(100.0, 0.1)
        90.0
        >>> calculate_discounted_total(0.0, 0.5)
        0.0
        >>> calculate_discounted_total(47.97, 0.1)
        43.17
        """
''')

ENTRY = "calculate_discounted_total"
TEST_ARGS = (100.0, 0.1)
EXPECTED = 90.0

SYS_CODE = """Output ONLY a Python function in one ```python block.
No explanations. Must begin with def and contain a return statement."""

SYS_ALT = """Write a FUNCTIONALLY EQUIVALENT but ALGORITHMICALLY DIFFERENT version.
FORBIDDEN APPROACHES: do not reuse the control flow or expression structure of
any version shown to you. Output ONLY one ```python block. The function MUST end
with a return statement."""

SYS_AT = """Write an acceptance test with EXACTLY this signature:
  def acceptance_test(result, *args) -> bool:
`result` is the NUMERIC return value of the function under test.
*args are the original inputs (subtotal, discount).
Return False for None / non-numeric / out-of-range results. Use the doctest
examples as ground truth. Wrap everything in try/except. Output ONLY one
```python block."""


def llm_call(system, user, retries=3):
    if not GROQ_API_KEY:
        raise SystemExit("Set GROQ_API_KEY to run design_time (or use the "
                         "pre-generated artifact that already ships here).")
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": GROQ_MODEL,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": user}],
                      "max_tokens": 800, "temperature": 0.2},
                timeout=60)
            data = r.json()
            if "choices" not in data:
                raise RuntimeError(f"Groq error {r.status_code}: {data}")
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  [WARN] attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    raise RuntimeError("LLM call failed after retries.")


def extract_code(t):
    m = re.search(r'```(?:python)?\s*(.*?)\s*```', t, re.DOTALL)
    code = m.group(1).strip() if m else t.strip()
    lines = code.splitlines()
    while lines and lines[0].strip().startswith("```"): lines = lines[1:]
    while lines and lines[-1].strip().startswith("```"): lines = lines[:-1]
    return "\n".join(lines).strip()


def validate_fn(src, label):
    ns = {}
    exec(compile(src, f"<{label}>", "exec"), ns)
    if ENTRY not in ns:
        raise ValueError(f"[{label}] {ENTRY} not defined")
    got = ns[ENTRY](*TEST_ARGS)
    if got is None:
        raise ValueError(f"[{label}] returned None (missing return?)")
    if abs(got - EXPECTED) > 0.02:
        raise ValueError(f"[{label}] wrong result {got}, expected {EXPECTED}")
    print(f"  [VALIDATE] {label}: {ENTRY}{TEST_ARGS} = {got} ✓")


def validate_at(src, label="at_code"):
    ns = {}
    exec(compile(src, f"<{label}>", "exec"), ns)
    at = ns["acceptance_test"]
    if at(90.0, 100.0, 0.1) is not True:
        raise ValueError("AT rejected a known-good result")
    if at(None, 100.0, 0.1) is not False:
        raise ValueError("AT accepted None")
    print(f"  [VALIDATE] {label}: signature + logic OK ✓")


def gen(label, system, user, is_at=False, attempts=3):
    for a in range(1, attempts + 1):
        print(f"  [GEN] {label} (attempt {a}/{attempts})")
        code = extract_code(llm_call(system, user))
        try:
            validate_at(code, label) if is_at else validate_fn(code, label)
            return code
        except ValueError as e:
            print(f"  [WARN] {e}")
            user += f"\n\nYour last attempt failed: {e}\nFix it."
            time.sleep(2)
    raise RuntimeError(f"could not generate valid {label}")


def main():
    print("[DESIGN TIME] primary...")
    primary = gen("primary", SYS_CODE, f"Write this function:\n{SPEC}")
    time.sleep(3)
    print("[DESIGN TIME] alt-1...")
    alt1 = gen("alt-1", SYS_ALT, f"Spec:\n{SPEC}\n\nDo NOT replicate:\n{primary}")
    time.sleep(3)
    print("[DESIGN TIME] alt-2...")
    alt2 = gen("alt-2", SYS_ALT,
               f"Spec:\n{SPEC}\n\nDo NOT replicate:\n{primary}\n\n---\n\n{alt1}")
    time.sleep(3)
    print("[DESIGN TIME] acceptance test...")
    at = gen("at_code", SYS_AT, f"Function spec:\n{SPEC}", is_at=True)

    artifact = {
        "entry_point": ENTRY, "spec": SPEC,
        "primary_code": primary, "alternates": [alt1, alt2], "at_code": at,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(ARTIFACT_OUT, "w") as f:
        json.dump(artifact, f, indent=2)
    print(f"[DESIGN TIME] Saved {ARTIFACT_OUT}. LLM will not be called at runtime.")


if __name__ == "__main__":
    main()
