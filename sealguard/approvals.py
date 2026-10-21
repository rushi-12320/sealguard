"""Human-in-the-loop decisions. Only a named person can approve; the guardrails still have the last word."""
import json

from . import events, ledger
from .db import active_thresholds, now_iso, one, rows, tx
from .guardrails import authorize, scan_untrusted


def _load(conn, pid):
    p = one(conn, "SELECT * FROM proposals WHERE id=?", (pid,))
    if not p:
        raise ValueError("proposal not found")
    for k in ("payload", "evidence", "critic"):
        p[k] = json.loads(p[k]) if p[k] else None
    return p


def decide(conn, pid, decision, by, note=""):
    by = (by or "").strip()
    if not by:
        raise ValueError("a named approver is required")
    p = _load(conn, pid)
    if p["status"] not in ("pending_approval", "critic_blocked"):
        raise ValueError(f"proposal is already {p['status']}")
    actor = f"human:{by}"
    if decision == "reject":
        with tx(conn):
            conn.execute("UPDATE proposals SET status='rejected', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                         (by, now_iso(), note, pid))
            ledger.append(conn, "HUMAN_REJECTION", actor, pid, {"note": note})
        events.publish("decision", id=pid, decision="rejected", by=by)
        return {"ok": True, "status": "rejected"}
    if decision != "approve":
        raise ValueError("decision must be approve or reject")

    if p["kind"] == "maintenance":
        g = authorize(conn, actor, "approve_maintenance", pid, context={"approved": True})
        if g["decision"] != "allow":
            return {"ok": False, **g}
        with tx(conn):
            conn.execute("UPDATE proposals SET status='approved', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                         (by, now_iso(), note, pid))
            ledger.append(conn, "HUMAN_APPROVAL", actor, pid, {"kind": "maintenance", "note": note,
                                                                "work_order": f"WO-{pid[4:]}"})
        events.publish("decision", id=pid, decision="approved", by=by)
        return {"ok": True, "status": "approved", "work_order": f"WO-{pid[4:]}"}

    info = p["payload"]
    critic_status = "passed" if p["status"] == "pending_approval" else "blocked"
    ver, thr = active_thresholds(conn)
    if abs(thr.get(p["uv"], info["current"]) - info["current"]) > 1e-9:
        return {"ok": False, "decision": "block",
                "reasons": [f"threshold changed since the proposal ({info['current']} -> {thr[p['uv']]}); re-run the agents"]}
    g = authorize(conn, actor, "apply_threshold", pid,
                  context={"approved": True, "critic_status": critic_status, "change_pct": info["change_pct"]})
    if g["decision"] != "allow":
        return {"ok": False, **g}
    new_ver = ver + 1
    with tx(conn):
        for uv, v in thr.items():
            conn.execute("INSERT INTO thresholds VALUES(?,?,?,?,?,?,?)",
                         (new_ver, uv, info["proposed"] if uv == p["uv"] else v, now_iso(), by,
                          pid if uv == p["uv"] else None, note if uv == p["uv"] else "carried over"))
        conn.execute("UPDATE proposals SET status='applied', decided_by=?, decided_at=?, decision_note=? WHERE id=?",
                     (by, now_iso(), note, pid))
        conn.execute("UPDATE proposals SET status='superseded' WHERE kind='threshold_change' AND uv=? AND id<>? "
                     "AND status IN ('pending_approval')", (p["uv"], pid))
        ledger.append(conn, "HUMAN_APPROVAL", actor, pid, {"note": note})
        ledger.append(conn, "THRESHOLD_APPLIED", actor, f"thresholds:v{new_ver}",
                      {"uv": p["uv"], "from": info["current"], "to": info["proposed"], "proposal": pid})
    events.publish("decision", id=pid, decision="applied", by=by, version=new_ver)
    return {"ok": True, "status": "applied", "version": new_ver}


def rollback(conn, to_version, by, note=""):
    by = (by or "").strip()
    if not by:
        raise ValueError("a named approver is required")
    old = {r["uv"]: r["value"] for r in rows(conn, "SELECT uv,value FROM thresholds WHERE version=?", (to_version,))}
    if not old:
        raise ValueError("unknown version")
    g = authorize(conn, f"human:{by}", "rollback_threshold", f"thresholds:v{to_version}", context={"approved": True})
    if g["decision"] != "allow":
        return {"ok": False, **g}
    ver, _ = active_thresholds(conn)
    with tx(conn):
        for uv, v in old.items():
            conn.execute("INSERT INTO thresholds VALUES(?,?,?,?,?,?,?)",
                         (ver + 1, uv, v, now_iso(), by, None, f"rollback to v{to_version}: {note}"))
        ledger.append(conn, "THRESHOLD_ROLLBACK", f"human:{by}", f"thresholds:v{ver + 1}",
                      {"restored_version": to_version, "values": old, "note": note})
    return {"ok": True, "version": ver + 1}


def set_label(conn, capture_id, defect_type, locations, by, note="", not_inspectable=False):
    by = (by or "").strip()
    if not by:
        raise ValueError("labeller name is required")
    if not one(conn, "SELECT 1 x FROM captures WHERE id=?", (capture_id,)):
        raise ValueError("capture not found")
    g = authorize(conn, f"human:{by}", "set_label", capture_id)
    if g["decision"] != "allow":
        return {"ok": False, **g}
    flags = scan_untrusted(note)
    locs = ",".join(sorted(set(locations or []))) if defect_type != "good" else ""
    with tx(conn):
        conn.execute("UPDATE captures SET defect_type=?, gt_locations=?, gt_source='human', gt_by=?, gt_note=?, "
                     "not_inspectable=? WHERE id=?", (defect_type, locs, by, note, int(bool(not_inspectable)), capture_id))
        ledger.append(conn, "LABEL_SET", f"human:{by}", capture_id,
                      {"defect_type": defect_type, "locations": locs, "not_inspectable": bool(not_inspectable),
                       "untrusted_instruction_flags": len(flags)})
        if flags:
            ledger.append(conn, "UNTRUSTED_INSTRUCTION_IGNORED", "system", capture_id,
                          {"field": "label note", "patterns": flags,
                           "note": "instruction-like text in data is stored as data and never executed"})
    return {"ok": True, "flags": flags}
