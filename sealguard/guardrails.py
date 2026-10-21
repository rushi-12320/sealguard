"""Policy engine. Every agent or human action that changes something goes through authorize().

Rules are enforced in code, never left to an agent's judgement:
  * unknown actions are denied (default deny)
  * forbidden actions are always blocked (delete data, edit the ledger, drive the PLC ...)
  * agents can only PROPOSE production changes; applying one needs a named human approval
  * a proposal the Critic blocked can never be applied
  * threshold steps above the policy limit are blocked
  * a kill switch pauses every agent write
  * rate limits on proposals, a step budget per agent run
"""
import copy
import json
import re
from datetime import datetime, timedelta

from . import ledger
from .db import get_setting, now_iso, one, set_setting, tx

DEFAULT_POLICY = {
    "agents_paused": False,
    "max_threshold_change_pct": 20.0,
    "min_good_samples": 30,
    "min_defect_samples": 5,
    "max_escapes_allowed": 0,
    "max_false_reject_pct": 2.0,
    "min_auroc_warning": 0.98,
    "max_proposals_per_uv_per_day": 3,
    "max_steps_per_run": 400,
    "data_window_hours": 12,
    "exposure_alert_pct": 5.0,
    "drift_sigma_alert": 1.0,
    "headroom_alert_pct": 15.0,
    "permissions": {
        "read_data": "auto",
        "raise_incident": "auto",
        "propose_threshold": "auto",
        "propose_maintenance": "auto",
        "generate_report": "auto",
        "set_label": "human",
        "apply_threshold": "human_approval",
        "rollback_threshold": "human_approval",
        "approve_maintenance": "human_approval",
        "stop_line": "human_approval",
        "change_policy": "admin",
        "delete_data": "forbidden",
        "modify_ledger": "forbidden",
        "control_plc": "forbidden",
        "retrain_and_deploy_model": "forbidden",
    },
}

WRITE_ACTIONS_LOGGED = {"apply_threshold", "rollback_threshold", "approve_maintenance", "stop_line", "change_policy"}

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above)? ?(instructions|rules|policy|policies)",
    r"(disable|bypass|turn off|skip) (the )?(guard|guardrail|critic|approval|policy|audit)",
    r"(apply|set|change|lower|raise) (the )?threshold",
    r"you are now", r"system prompt", r"approve (this|it|the proposal)", r"auto[- ]?approve",
]


class BudgetExceeded(Exception):
    pass


def get_policy(conn):
    pol = copy.deepcopy(DEFAULT_POLICY)
    stored = get_setting(conn, "policy", {}) or {}
    perms = {**pol["permissions"], **stored.get("permissions", {})}
    pol.update({k: v for k, v in stored.items() if k != "permissions"})
    pol["permissions"] = perms
    return pol


def set_policy(conn, changes, by):
    """Admin change; forbidden permissions can't be relaxed from the UI."""
    decision = authorize(conn, f"human:{by}", "change_policy", "policy", context={"admin": True})
    if decision["decision"] != "allow":
        return {"ok": False, **decision}
    old = get_policy(conn)
    new = copy.deepcopy(old)
    for k, v in changes.items():
        if k == "permissions":
            for a, p in v.items():
                if DEFAULT_POLICY["permissions"].get(a) == "forbidden" and p != "forbidden":
                    return {"ok": False, "decision": "block", "reasons": [f"'{a}' is hard-forbidden and cannot be enabled"]}
                new["permissions"][a] = p
        elif k in DEFAULT_POLICY:
            new[k] = type(DEFAULT_POLICY[k])(v)
    diff = {k: {"from": old[k], "to": new[k]} for k in new if k != "permissions" and old[k] != new[k]}
    with tx(conn):
        set_setting(conn, "policy", new)
        ledger.append(conn, "POLICY_CHANGED", f"human:{by}", "policy", diff or {"note": "no change"})
    return {"ok": True, "diff": diff, "policy": new}


def evaluate(policy, actor, action, context=None):
    """Pure decision (no logging): -> {'decision': allow|needs_approval|block, 'reasons': [...]}"""
    context = context or {}
    is_agent = actor.startswith("agent:")
    perm = policy["permissions"].get(action)
    reasons = []
    if perm is None:
        return {"decision": "block", "reasons": [f"unknown action '{action}' (default deny)"]}
    if perm == "forbidden":
        return {"decision": "block", "reasons": [f"'{action}' is forbidden by policy"]}
    if is_agent and policy.get("agents_paused") and action != "read_data":
        return {"decision": "block", "reasons": ["kill switch: all agent actions are paused"]}
    if perm == "admin":
        if is_agent or not context.get("admin"):
            return {"decision": "block", "reasons": [f"'{action}' needs a human administrator"]}
    if perm == "human" and is_agent:
        return {"decision": "block", "reasons": [f"'{action}' can only be done by a person"]}
    if action == "apply_threshold":
        if context.get("critic_status") != "passed":
            reasons.append("Critic did not pass this proposal")
        cp = abs(float(context.get("change_pct", 0)))
        if cp > policy["max_threshold_change_pct"]:
            reasons.append(f"change {cp:.1f}% exceeds limit {policy['max_threshold_change_pct']}%")
        if reasons:
            return {"decision": "block", "reasons": reasons}
    if action == "propose_threshold" and context.get("proposals_last_24h", 0) >= policy["max_proposals_per_uv_per_day"]:
        return {"decision": "block", "reasons": ["proposal rate limit reached for this UV (24 h)"]}
    if perm == "human_approval":
        if is_agent:
            return {"decision": "needs_approval", "reasons": ["agents may only propose; a named person must approve"]}
        if not context.get("approved"):
            return {"decision": "needs_approval", "reasons": ["no recorded human approval"]}
    return {"decision": "allow", "reasons": reasons or ["permitted by policy"]}


def authorize(conn, actor, action, target, run_id=None, context=None, policy=None):
    """Evaluate and record. Blocks, approval requests and privileged writes are also sealed in the ledger."""
    policy = policy or get_policy(conn)
    res = evaluate(policy, actor, action, context)
    with tx(conn):
        conn.execute("INSERT INTO guard_log(ts,actor,action,target,decision,reasons,run_id) VALUES(?,?,?,?,?,?,?)",
                     (now_iso(), actor, action, target, res["decision"], json.dumps(res["reasons"]), run_id))
        if res["decision"] != "allow" or action in WRITE_ACTIONS_LOGGED:
            ledger.append(conn, "GUARD_DECISION", actor, target,
                          {"action": action, "decision": res["decision"], "reasons": res["reasons"], "run_id": run_id})
    return res


def proposals_last_24h(conn, uv, as_of=None):
    ref = datetime.fromisoformat(as_of) if as_of else datetime.fromisoformat(now_iso())
    since = (ref - timedelta(hours=24)).isoformat()
    r = one(conn, "SELECT COUNT(*) n FROM proposals WHERE kind='threshold_change' AND uv=? AND created_at>=?",
            (uv, since))
    return r["n"] if r else 0


def scan_untrusted(text):
    """Finds instruction-like text inside DATA (label notes, CSV fields). Such text is never executed."""
    if not text:
        return []
    low = text.lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, low)]
