"""Optional AI-written executive summary, with a grounding guard.

Off unless ANTHROPIC_API_KEY is set and the `anthropic` package is installed. The model only gets
facts the agents computed, and its text is rejected if it contains any number that is not in those
facts (no invented figures). The model never decides anything: thresholds, approvals and verdicts
stay with the deterministic agents, the guardrails and people.
"""
import json
import os
import re

MODEL = os.environ.get("SEALGUARD_LLM_MODEL", "claude-sonnet-5-5")
SYSTEM = ("You write a short shift summary (max 5 sentences) for a quality engineer of a UV seal inspection "
          "line. Use ONLY numbers that appear in FACTS. Do not recommend changing thresholds yourself; say that "
          "pending proposals need human review. Plain text, no markdown.")


def available():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _call_model(facts_json):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=400, system=SYSTEM,
                                 messages=[{"role": "user", "content": f"FACTS:\n{facts_json}"}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _numbers(obj, out):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, out)
    elif isinstance(obj, str):
        for m in re.findall(r"-?\d+(?:\.\d+)?", obj):
            out.add(float(m))


def grounding_check(text, facts):
    allowed = set()
    _numbers(facts, allowed)
    cleaned = re.sub(r"\b(UV|P)\d\b|\bC\d{6}\b|RUN-[0-9A-F]+", " ", text)
    bad = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", cleaned):
        v = float(m)
        if not any(abs(v - a) <= max(0.051, abs(a) * 0.01) for a in allowed):
            bad.append(m)
    return {"grounded": not bad, "ungrounded_numbers": bad}


def summarize(facts, call=None):
    """-> (text or None, info)"""
    call = call or (_call_model if available() else None)
    if call is None:
        return None, {"used": False, "reason": "LLM not configured"}
    try:
        text = call(json.dumps(facts, sort_keys=True, default=str)).strip()
    except Exception as e:  # network, quota ... -> report still works without it
        return None, {"used": False, "reason": f"LLM call failed: {e}"}
    check = grounding_check(text, facts)
    if not check["grounded"]:
        return None, {"used": False, "reason": "rejected by grounding guard", **check}
    return text, {"used": True, "model": MODEL, **check}
