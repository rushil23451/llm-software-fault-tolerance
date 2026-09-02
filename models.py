"""
models.py — all models via Groq, single API key.

Verified model IDs from https://console.groq.com/docs/models:

    openai/gpt-oss-120b                          (production)
    llama-3.3-70b-versatile                      (production)
    openai/gpt-oss-20b                           (production)
    meta-llama/llama-4-scout-17b-16e-instruct    (preview)
    moonshotai/kimi-k2-instruct-0905             (preview)

Set ONE env var:
    GROQ_API_KEY=gsk_...
"""

import os
import re
import time

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

_MODEL_IDS = {
    "manager":     "openai/gpt-oss-120b",
    "generator_A": "llama-3.3-70b-versatile",
    "generator_B": "meta-llama/llama-4-scout-17b-16e-instruct",
    "generator_C": "openai/gpt-oss-120b",
    "judge":       "openai/gpt-oss-120b",
}


# Lazy singleton — one Groq client per process, reuses TCP/TLS connections.
_groq_client = None

def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        import groq
        _groq_client = groq.Groq(api_key=os.environ.get("GROQ_API_KEY"))
    return _groq_client


def _make_groq_caller(role: str):
    # Capture the exact model ID for this role safely
    model = _MODEL_IDS[role]

    def _call(system: str, user: str, max_tokens: int) -> str:
        # Only force JSON mode for the manager and judge
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }
        if role in ["manager", "judge"]:
            kwargs["response_format"] = {"type": "json_object"}

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = _get_groq_client().chat.completions.create(**kwargs)
                return resp.choices[0].message.content
            except Exception as e:
                # Catch rate-limit responses and apply exponential backoff
                if "429" in str(e) and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
    return _call


_ROLES: dict[str, tuple] = {
    role: (_make_groq_caller(role), lambda r=role: _MODEL_IDS[r])
    for role in _MODEL_IDS
}


def generate(role: str, system: str, user: str, max_tokens: int = 2000) -> str:
    if role not in _ROLES:
        raise ValueError(f"Unknown role '{role}'. Available: {list(_ROLES)}")
    fn, _ = _ROLES[role]
    raw_output = fn(system, user, max_tokens)

    # Strip <tool_call> artifacts globally to prevent JSON parsing errors
    raw_output = re.sub(r'<tool_call>.*?</tool_call>', '', raw_output, flags=re.DOTALL)
    raw_output = raw_output.replace('<tool_call>', '')

    return raw_output


def get_assignment_summary() -> str:
    lines = ["Model assignment (all via Groq — single API key):"]
    for role, (_, name_fn) in _ROLES.items():
        lines.append(f"  {role:<14} → {name_fn()}")
    return "\n".join(lines)