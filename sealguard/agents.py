"""The agent pipeline: Planner -> Triage -> Drift -> Calibration -> Critic -> Reporter -> Auditor.

Agents are deterministic (same data -> same result), so every run can be replayed and proven.
They only read data and PROPOSE; every write goes through the guardrails (guardrails.authorize).
Each run records its input hash (data snapshot) and output hash (decisions) for replay.
"""
import json
import uuid
from datetime import datetime, timedelta
from statistics import mean, pstdev

from . import events, ledger, llm, metrics
from .db import active_thresholds, now_iso, one, rows, thresholds_at, tx
from .domain import FAIL, LOCATION_UVS, NOT_INSPECTABLE, PASS, UVS, product_of
from .guardrails import BudgetExceeded, authorize, evaluate, get_policy, scan_untrusted

DEFECT_CAUSES = {"CORROBORATED_DEFECT", "PROBABLE_DEFECT", "BORDERLINE"}
ACTIONS = {
    "IMAGE_QUALITY": "Recapture; check exposure / lamp of the listed UV",
    "MISALIGNMENT": "Check pouch placement / feeder, then recapture",
    "CORROBORATED_DEFECT": "Reject - defect confirmed by two camera views",
    "PROBABLE_DEFECT": "Reject - strong single-view defect signal",
    "BORDERLINE": "Manual review - score just above threshold",
}


# ------------------------------------------------------------------ run context
class RunContext:
    def __init__(self, conn, kind, params, dry=False):
        self.conn, self.kind, self.params, self.dry = conn, kind, params, dry
        self.run_id = ("REPLAY-" if dry else "RUN-") + uuid.uuid4().hex[:8].upper()
        self.policy = get_policy(conn)
        if "policy" in params:  # replay uses the policy that was active in the original run
            self.policy = {**self.policy, **params["policy"]}
        self.steps, self.n = [], 0
        self.created = {"incidents": [], "proposals": []}
        self._dry_ids = 0

    def step(self, agent, title, detail=None, citations=None, status="ok"):
        self.n += 1
        if self.n > self.policy["max_steps_per_run"]:
            raise BudgetExceeded(f"step budget of {self.policy['max_steps_per_run']} exceeded")
        s = {"n": self.n, "agent": agent, "title": title, "detail": detail or {}, "citations": citations or [],
             "status": status, "t": now_iso()}
        self.steps.append(s)
        if not self.dry:
            events.publish("agent_step", run_id=self.run_id, agent=agent, title=title, status=status)
        return s

    def authorize(self, agent, action, target, context=None):
        actor = f"agent:{agent}"
        if self.dry:
            return evaluate(self.policy, actor, action, context)
        return authorize(self.conn, actor, action, target, self.run_id, context, self.policy)

    def new_id(self, prefix):
        if self.dry:
            self._dry_ids += 1
            return f"DRY-{prefix}-{self._dry_ids}"
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# ------------------------------------------------------------------ data snapshot
class Snapshot:
    def __init__(self, conn, params):
        self.as_of, self.window_hours = params["as_of"], params["window_hours"]
        self.since = (datetime.fromisoformat(self.as_of) - timedelta(hours=self.window_hours)).isoformat()
        self.thresholds = thresholds_at(conn, params["threshold_version"])
        self.images = metrics.window_images(conn, self.since, self.as_of)
        self.captures = rows(conn, "SELECT * FROM captures WHERE ts>? AND ts<=? ORDER BY ts, id", (self.since, self.as_of))
        self.labelled = metrics.labelled_regions(conn, None, self.as_of)
        self.by_capture = {}
        for i in self.images:
            self.by_capture.setdefault(i["capture_id"], []).append(i)
        self.input_hash = ledger.sha256(ledger.canonical({
            "images": [(i["capture_id"], i["uv"], i["region"], i["score"], i["decision"], i["exposure"]) for i in self.images],
            "labels": [(r["capture_id"], r["region"], r["positive"], r["score"]) for r in self.labelled],
            "thresholds": self.thresholds}))

    def uv_sets(self, uv):
        lab = [r for r in self.labelled if r["uv"] == uv]
        return [r for r in lab if not r["positive"]], [r for r in lab if r["positive"]]


DEDUPE_HOURS = 6


def _recent_duplicate(ctx, table, kind, uv):
    """Same kind/UV raised in the DEDUPE_HOURS before this run started -> don't repeat it (deterministic on replay)."""
    ref = ctx.params.get("started_at")
    if not ref:
        return None
    since = (datetime.fromisoformat(ref) - timedelta(hours=DEDUPE_HOURS)).isoformat()
    return one(ctx.conn, f"SELECT id, title FROM {table} WHERE kind=? AND IFNULL(uv,'')=? AND created_at>=? "
                         "AND created_at<? ORDER BY created_at DESC LIMIT 1", (kind, uv or "", since, ref))


def _raise_incident(ctx, agent, kind, uv, severity, title, detail, evidence):
    dup = _recent_duplicate(ctx, "incidents", kind, uv)
    if dup:
        ctx.step(agent, f"Already reported as {dup['id']} - not repeated", {"title": title}, status="info")
        return None
    d = ctx.authorize(agent, "raise_incident", f"incident:{kind}:{uv}")
    if d["decision"] != "allow":
        ctx.step(agent, f"Incident blocked by guardrail: {title}", d, status="blocked")
        return None
    inc = {"id": ctx.new_id("INC"), "created_at": now_iso(), "kind": kind, "uv": uv, "severity": severity,
           "title": title, "detail": detail, "evidence": evidence, "status": "open", "run_id": ctx.run_id}
    if not ctx.dry:
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO incidents VALUES(:id,:created_at,:kind,:uv,:severity,:title,:detail,:evidence,"
                             ":status,:run_id)", {**inc, "detail": json.dumps(detail), "evidence": json.dumps(evidence)})
            ledger.append(ctx.conn, "INCIDENT_RAISED", f"agent:{agent}", inc["id"],
                          {"kind": kind, "uv": uv, "severity": severity, "title": title, "evidence_count": len(evidence)})
        events.publish("incident", id=inc["id"], severity=severity, title=title)
    ctx.created["incidents"].append(inc)
    return inc


def _create_proposal(ctx, agent, kind, uv, title, payload, evidence, action):
    dup = _recent_duplicate(ctx, "proposals", kind, uv)
    if dup:
        ctx.step(agent, f"{dup['id']} for {uv} was raised less than {DEDUPE_HOURS} h ago - not repeated",
                 {"title": title, "existing": dup["title"]}, status="info")
        return None
    context = {}
    if kind == "threshold_change":
        context["proposals_last_24h"] = _proposals_before(ctx.conn, uv, ctx.params["started_at"])
    d = ctx.authorize(agent, action, f"proposal:{kind}:{uv}", context)
    if d["decision"] != "allow":
        ctx.step(agent, f"Proposal blocked by guardrail: {title}", d, status="blocked")
        return None
    p = {"id": ctx.new_id("PRP"), "created_at": now_iso(), "kind": kind, "uv": uv, "title": title,
         "payload": payload, "evidence": evidence, "critic": None, "status": "critic_review", "run_id": ctx.run_id}
    if not ctx.dry:
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO proposals(id,created_at,kind,uv,title,payload,evidence,critic,status,run_id) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (p["id"], p["created_at"], kind, uv, title, json.dumps(payload), json.dumps(evidence),
                              None, p["status"], ctx.run_id))
            ledger.append(ctx.conn, "PROPOSAL_CREATED", f"agent:{agent}", p["id"],
                          {"kind": kind, "uv": uv, "title": title, "payload": payload})
        events.publish("proposal", id=p["id"], title=title)
    ctx.created["proposals"].append(p)
    return p


def _proposals_before(conn, uv, ref):
    since = (datetime.fromisoformat(ref) - timedelta(hours=24)).isoformat()
    r = one(conn, "SELECT COUNT(*) n FROM proposals WHERE kind='threshold_change' AND uv=? AND created_at>=? "
                  "AND created_at<?", (uv, since, ref))
    return r["n"]


# ------------------------------------------------------------------ agents
def planner(ctx, snap):
    pol = ctx.policy
    plan, skipped, calibrate = [], {}, []
    unlabelled = sum(1 for c in snap.captures if c["gt_source"] is None)
    for uv in UVS:
        neg, pos = snap.uv_sets(uv)
        if len(neg) >= pol["min_good_samples"] and len(pos) >= pol["min_defect_samples"]:
            calibrate.append(uv)
        else:
            skipped[uv] = f"not enough labelled data ({len(neg)} good / {len(pos)} defect regions; " \
                          f"need {pol['min_good_samples']} / {pol['min_defect_samples']})"
    plan = ["Triage every non-PASS capture in the window",
            "Check good-score drift and exposure rate for UV1-UV5",
            f"Calibrate thresholds for {', '.join(calibrate) or 'no UV'}",
            "Critic challenges every proposal against policy", "Reporter drafts shift report",
            "Auditor verifies ledger, citations and approvals"]
    detail = {"window": f"{snap.since} -> {snap.as_of}", "captures": len(snap.captures), "images": len(snap.images),
              "unlabelled_captures": unlabelled, "calibrate": calibrate, "skipped": skipped, "plan": plan,
              "threshold_version": ctx.params["threshold_version"]}
    ctx.step("planner", f"Plan: {len(snap.captures)} captures, calibrate {len(calibrate)} UV(s)", detail)
    if unlabelled:
        ctx.step("planner", f"{unlabelled} captures in window have no ground-truth label (labelling queue)",
                 {"unlabelled": unlabelled}, status="info")
    return {"calibrate": calibrate, "skipped": skipped, "unlabelled": unlabelled}


def classify_capture(imgs):
    fails = [i for i in imgs if i["decision"] == FAIL]
    if not fails:
        ni = sorted({i["uv"] for i in imgs if i["decision"] == NOT_INSPECTABLE})
        return {"cause": "IMAGE_QUALITY", "uvs": ni, "locations": [], "margin": None,
                "reason": f"no failing region; {', '.join(ni)} not inspectable (exposure)"}
    margin = {(i["uv"], i["region"]): i["score"] / i["threshold"] for i in fails}
    maxm = max(margin.values())
    by_loc = {}
    for i in imgs:
        by_loc.setdefault(i["region"], {})[i["uv"]] = i
    locations = sorted({i["region"] for i in fails})
    uvs = sorted({i["uv"] for i in fails})
    for prod in ("P1", "P2"):
        pf = [k for k in margin if product_of(k[1]) == prod]
        if len(pf) >= 3 and len({k[0] for k in pf}) >= 2 and max(margin[k] for k in pf) < 1.5:
            return {"cause": "MISALIGNMENT", "uvs": uvs, "locations": locations, "margin": round(maxm, 3),
                    "reason": f"{len(pf)} regions of {prod} fail together with moderate scores (max {maxm:.2f}x threshold)"}
    corroborated, conflicts = [], []
    for loc in locations:
        views = by_loc.get(loc, {})
        if len(LOCATION_UVS[loc]) == 2:
            decs = {uv: views[uv]["decision"] for uv in LOCATION_UVS[loc] if uv in views}
            if list(decs.values()).count(FAIL) == 2:
                corroborated.append(loc)
            elif PASS in decs.values():
                conflicts.append(loc)
    if corroborated:
        cause, reason = "CORROBORATED_DEFECT", f"{', '.join(corroborated)} failed in both camera views"
    elif maxm >= 1.2:
        cause, reason = "PROBABLE_DEFECT", f"score {maxm:.2f}x threshold" + \
            (f"; second view disagrees at {', '.join(conflicts)}" if conflicts else "")
    else:
        cause, reason = "BORDERLINE", f"score only {maxm:.2f}x threshold"
    return {"cause": cause, "uvs": uvs, "locations": locations, "margin": round(maxm, 3), "reason": reason}


def triage(ctx, snap):
    caps = {c["id"]: c for c in snap.captures}
    items, counts = [], {}
    agree = disagree = 0
    for cid, imgs in snap.by_capture.items():
        c = caps.get(cid)
        if not c or c["verdict"] == "PASS":
            continue
        res = classify_capture(imgs)
        res.update({"capture_id": cid, "verdict": c["verdict"], "action": ACTIONS[res["cause"]]})
        if c["gt_source"] is not None and res["cause"] != "IMAGE_QUALITY":
            gt_defect = bool((c["gt_locations"] or "").strip())
            res["ground_truth"] = "defect" if gt_defect else "good"
            ok = (res["cause"] in DEFECT_CAUSES) == gt_defect
            agree += ok
            disagree += not ok
        items.append(res)
        counts[res["cause"]] = counts.get(res["cause"], 0) + 1
    for cause in ("CORROBORATED_DEFECT", "PROBABLE_DEFECT", "BORDERLINE", "MISALIGNMENT", "IMAGE_QUALITY"):
        sel = [x for x in items if x["cause"] == cause]
        if sel:
            ctx.step("triage", f"{len(sel)} x {cause.replace('_', ' ').lower()} -> {ACTIONS[cause]}",
                     {"examples": sel[:8]}, citations=[x["capture_id"] for x in sel[:25]])
    acc = round(100 * agree / (agree + disagree), 1) if agree + disagree else None
    ctx.step("triage", f"Triage vs ground truth: {agree} agree, {disagree} disagree"
             + (f" ({acc}%)" if acc is not None else ""), {"agree": agree, "disagree": disagree, "accuracy_pct": acc},
             status="info")
    mis = [x for x in items if x["cause"] == "MISALIGNMENT"]
    if len(mis) >= 2:
        _raise_incident(ctx, "triage", "misalignment", None, "medium",
                        f"{len(mis)} captures look like pouch misalignment, not seal defects",
                        {"action": ACTIONS["MISALIGNMENT"]}, [x["capture_id"] for x in mis])
    border = [x for x in items if x["cause"] == "BORDERLINE"]
    if len(border) >= 3:
        _raise_incident(ctx, "triage", "manual_review", None, "low", f"{len(border)} borderline rejects need manual review",
                        {"action": ACTIONS["BORDERLINE"]}, [x["capture_id"] for x in border])
    return {"items": items, "counts": counts, "accuracy_pct": acc}


def drift(ctx, snap):
    pol, out = ctx.policy, {}
    for uv in UVS:
        imgs = [i for i in snap.images if i["uv"] == uv]
        if not imgs:
            continue
        goods = [i["score"] for i in imgs if i["exposure"] == "OK" and i["decision"] == PASS
                 and i["region"] not in set((i["gt_locations"] or "").split(","))]
        thr = snap.thresholds[uv]
        res = {"uv": uv, "samples": len(goods), "threshold": thr}
        if len(goods) >= 60:
            nb = max(40, len(goods) // 4)
            mu0, sd0 = mean(goods[:nb]), pstdev(goods[:nb]) or 1.0
            s, alarm = 0.0, None
            for k, x in enumerate(goods):
                s = max(0.0, s + (x - mu0) / sd0 - 0.5)
                if alarm is None and s > 8:
                    alarm = k
            recent = goods[-100:]
            shift = (mean(recent) - mu0) / sd0
            tail = goods[-200:]
            xm = (len(tail) - 1) / 2
            slope = sum((k - xm) * (v - mean(tail)) for k, v in enumerate(tail)) / \
                sum((k - xm) ** 2 for k in range(len(tail)))
            p99 = metrics.quantile(recent, 0.99)
            headroom = 100 * (thr - p99) / thr
            active = alarm is not None and shift >= pol["drift_sigma_alert"]
            res.update({"baseline_mean": round(mu0, 2), "baseline_sd": round(sd0, 2), "recent_mean": round(mean(recent), 2),
                        "shift_sigma": round(shift, 2), "cusum_alarm_at_sample": alarm, "slope_per_100": round(100 * slope, 2),
                        "recent_p99": round(p99, 2), "headroom_pct": round(headroom, 1), "drift_active": active,
                        "samples_to_threshold": int((thr - p99) / slope) if slope > 0 and p99 < thr else None})
        recent_imgs = imgs[-120:]
        ov_recent = sum(i["exposure"] != "OK" for i in recent_imgs)
        res["overexposed_pct_window"] = round(100 * sum(i["exposure"] != "OK" for i in imgs) / len(imgs), 2)
        res["overexposed_pct_recent"] = round(100 * ov_recent / len(recent_imgs), 2)
        peak, worst = 0.0, []
        for k in range(0, max(1, len(imgs) - 119), 20):
            w = imgs[k:k + 120]
            pct = 100 * sum(i["exposure"] != "OK" for i in w) / len(w)
            if pct > peak:
                peak, worst = pct, [i["capture_id"] for i in w if i["exposure"] != "OK"]
        res["overexposed_pct_peak"] = round(peak, 2)
        out[uv] = res
        if res.get("drift_active"):
            sev = "high" if res["headroom_pct"] < pol["headroom_alert_pct"] else "medium"
            title = f"{uv} good-score drift +{res['shift_sigma']}σ, headroom {res['headroom_pct']}%"
            ctx.step("drift", title, res, status="alert")
            ev = [i["capture_id"] for i in imgs if i["exposure"] == "OK"][-20:]
            _raise_incident(ctx, "drift", "drift", uv, sev, title,
                            {**res, "action": f"Inspect {uv} lamp / exposure; recalibrate only after maintenance. "
                                              "Do not raise the threshold to hide the drift."}, sorted(set(ev)))
            _create_proposal(ctx, "drift", "maintenance", uv, f"Inspect {uv} lamp and exposure (score drift)",
                             {"reason": "good-score drift", "stats": res}, sorted(set(ev)), "propose_maintenance")
        else:
            ctx.step("drift", f"{uv} stable" + (f" (shift {res['shift_sigma']}σ, headroom {res['headroom_pct']}%)"
                                                if "shift_sigma" in res else " (too few samples)"), res)
        if peak >= pol["exposure_alert_pct"]:
            title = f"{uv} exposure burst: up to {peak:.1f}% of images over-exposed"
            ctx.step("drift", title, {"peak_pct": peak, "recent_pct": res["overexposed_pct_recent"]}, status="alert",
                     citations=sorted(set(worst))[:25])
            _raise_incident(ctx, "drift", "exposure", uv, "high", title,
                            {"peak_pct": round(peak, 2), "action": f"Check {uv} lamp / exposure time; affected pouches need recapture"},
                            sorted(set(worst)))
            _create_proposal(ctx, "drift", "maintenance", uv, f"Check {uv} exposure settings / lamp (over-exposure burst)",
                             {"reason": "over-exposure burst", "peak_pct": round(peak, 2)}, sorted(set(worst)),
                             "propose_maintenance")
    return out


def calibration(ctx, snap, plan):
    pol, recs, proposals = ctx.policy, {}, []
    for uv in plan["calibrate"]:
        neg, pos = snap.uv_sets(uv)
        rec = metrics.recommend([r["score"] for r in neg], [r["score"] for r in pos])
        cur = snap.thresholds[uv]
        lim = pol["max_threshold_change_pct"] / 100
        proposed = round(min(max(rec["threshold"], cur * (1 - lim)), cur * (1 + lim)), 2)
        change = round(100 * (proposed - cur) / cur, 2)
        wi_cur, wi_new = metrics.whatif(neg, pos, cur), metrics.whatif(neg, pos, proposed)
        info = {"uv": uv, "current": cur, "recommended": rec["threshold"], "proposed": proposed, "change_pct": change,
                "clamped": proposed != rec["threshold"], "stats": rec, "whatif_current": wi_cur, "whatif_proposed": wi_new}
        recs[uv] = info
        if abs(change) < 3:
            ctx.step("calibration", f"{uv}: current threshold {cur} is fine (recommended {rec['threshold']})", info)
            continue
        dup = _recent_duplicate(ctx, "proposals", "threshold_change", uv)
        if dup:
            ctx.step("calibration", f"{uv}: would propose {cur} -> {proposed}, but {dup['id']} is still recent - not repeated",
                     info, status="info")
            continue
        why = []
        if wi_cur["escapes"]:
            why.append(f"current threshold lets {wi_cur['escapes']} labelled defect region(s) pass")
        if wi_cur["false_rejects"] > wi_new["false_rejects"]:
            why.append(f"{wi_cur['false_rejects'] - wi_new['false_rejects']} fewer false rejects")
        title = f"{uv} threshold {cur} -> {proposed} ({change:+.1f}%)"
        ctx.step("calibration", f"Proposing {title}" + (f": {'; '.join(why)}" if why else ""), info,
                 citations=[e.split('/')[0] for e in wi_cur["escape_examples"]])
        p = _create_proposal(ctx, "calibration", "threshold_change", uv, title, info,
                             sorted({e.split('/')[0] for e in wi_cur["escape_examples"] + wi_new["false_reject_examples"]}),
                             "propose_threshold")
        if p:
            proposals.append(p)
    return {"recs": recs, "proposals": proposals}


def critique(policy, info, drift_res):
    """Critic checks for one threshold proposal -> (status, findings). Pure function (used by red-team too)."""
    f = []

    def add(check, ok, detail, hard=True, warn_only=False):
        f.append({"check": check, "result": "pass" if ok else ("warn" if warn_only else "fail"),
                  "hard": hard and not warn_only, "detail": detail})

    st, wn = info["stats"], info["whatif_proposed"]
    add("Enough labelled data", st["n_good"] >= policy["min_good_samples"] and st["n_defect"] >= policy["min_defect_samples"],
        f"{st['n_good']} good / {st['n_defect']} defect regions")
    add("Zero escapes on labelled defects", wn["escapes"] <= policy["max_escapes_allowed"],
        f"{wn['escapes']} labelled defect region(s) would pass at {info['proposed']}")
    add("Step within limit", abs(info["change_pct"]) <= policy["max_threshold_change_pct"],
        f"{info['change_pct']:+.1f}% (limit ±{policy['max_threshold_change_pct']}%)")
    fr = wn["false_reject_pct"] or 0
    if fr > 3 * policy["max_false_reject_pct"]:
        add("False-reject rate", False, f"{fr}% of good regions would fail (> 3x limit)")
    else:
        add("False-reject rate", fr <= policy["max_false_reject_pct"], f"{fr}% (limit {policy['max_false_reject_pct']}%)",
            warn_only=True)
    d = (drift_res or {}).get(info["uv"], {})
    masking = bool(d.get("drift_active")) and info["proposed"] > info["current"]
    add("Not masking drift", not masking,
        f"{info['uv']} good scores drifted +{d.get('shift_sigma')}σ; raising the threshold would hide a lamp / "
        "exposure problem - fix the cause first" if masking else "no active drift conflict")
    if st.get("auroc") is not None:
        add("Good/defect separation", st["auroc"] >= policy["min_auroc_warning"], f"AUROC {st['auroc']}", warn_only=True)
    hard_fail = [x for x in f if x["result"] == "fail" and x["hard"]]
    return ("critic_blocked" if hard_fail else "pending_approval"), f


def critic(ctx, snap, drift_res):
    verdicts = []
    for p in ctx.created["proposals"]:
        if p["kind"] == "threshold_change":
            status, findings = critique(ctx.policy, p["payload"], drift_res)
        else:
            ok = bool(p["evidence"])
            status = "pending_approval" if ok else "critic_blocked"
            findings = [{"check": "Evidence attached", "result": "pass" if ok else "fail", "hard": True,
                         "detail": f"{len(p['evidence'])} capture(s) cited"}]
        p["status"], p["critic"] = status, findings
        fails = [x["check"] for x in findings if x["result"] == "fail"]
        warns = [x["check"] for x in findings if x["result"] == "warn"]
        ctx.step("critic", f"{'BLOCKED' if status == 'critic_blocked' else 'Passed'}: {p['title']}"
                 + (f" - failed: {', '.join(fails)}" if fails else "") + (f" - warnings: {', '.join(warns)}" if warns else ""),
                 {"proposal": p["id"], "findings": findings}, status="blocked" if fails else "ok")
        if not ctx.dry:
            with tx(ctx.conn):
                ctx.conn.execute("UPDATE proposals SET critic=?, status=? WHERE id=?", (json.dumps(findings), status, p["id"]))
                ledger.append(ctx.conn, "CRITIC_ASSESSMENT", "agent:critic", p["id"],
                              {"status": status, "failed": fails, "warnings": warns})
        verdicts.append({"uv": p["uv"], "kind": p["kind"], "status": status, "failed": fails})
    return verdicts


def reporter(ctx, snap, plan, tri, drift_res, cal, verdicts, kpi):
    cites = set()
    L = [f"# Shift report - {snap.as_of[:16].replace('T', ' ')} UTC",
         f"Window: last {snap.window_hours} h - {len(snap.captures)} captures - threshold version "
         f"v{ctx.params['threshold_version']} - run {ctx.run_id}", "", "## Line summary"]
    v = kpi["verdicts"]
    L += [f"- PASS {v.get('PASS', 0)}, PRODUCT_1 {v.get('PRODUCT_1', 0)}, PRODUCT_2 {v.get('PRODUCT_2', 0)}, "
          f"BOTH {v.get('BOTH', 0)}, RECAPTURE {v.get('RECAPTURE', 0)}",
          f"- Pass rate of inspectable captures: {kpi['pass_pct']}% - recapture rate {kpi['recapture_pct']}%",
          f"- Unlabelled captures waiting for ground truth: {plan['unlabelled']}", "", "## Reject triage"]
    for cause, n in sorted(tri["counts"].items(), key=lambda x: -x[1]):
        ex = [x["capture_id"] for x in tri["items"] if x["cause"] == cause][:5]
        cites.update(ex)
        L.append(f"- {cause.replace('_', ' ').title()}: {n} -> {ACTIONS[cause]} (e.g. {', '.join(ex)})")
    if tri["accuracy_pct"] is not None:
        L.append(f"- Triage agreement with ground-truth labels: {tri['accuracy_pct']}%")
    L += ["", "## Camera health (UV1-UV5)", "| UV | threshold | good mean | p99 | headroom | drift | over-exposed peak |",
          "|---|---|---|---|---|---|---|"]
    for uv, d in drift_res.items():
        L.append(f"| {uv} | {d['threshold']} | {d.get('recent_mean', '-')} | {d.get('recent_p99', '-')} | "
                 f"{d.get('headroom_pct', '-')}% | {'YES +' + str(d['shift_sigma']) + 'σ' if d.get('drift_active') else 'no'} | "
                 f"{d['overexposed_pct_peak']}% |")
    L += ["", "## Incidents"]
    for inc in ctx.created["incidents"]:
        cites.update(inc["evidence"][:5])
        L.append(f"- [{inc['severity'].upper()}] {inc['title']} ({len(inc['evidence'])} captures, e.g. "
                 f"{', '.join(inc['evidence'][:3])})")
    if not ctx.created["incidents"]:
        L.append("- none")
    L += ["", "## Proposals (need human decision)"]
    for p in ctx.created["proposals"]:
        fails = [x["check"] for x in (p["critic"] or []) if x["result"] == "fail"]
        L.append(f"- {p['title']} -> {'BLOCKED by Critic: ' + ', '.join(fails) if fails else 'pending human approval'}")
    if not ctx.created["proposals"]:
        L.append("- none")
    for uv, why in plan["skipped"].items():
        L.append(f"- {uv} calibration skipped: {why}")
    facts = {"captures": len(snap.captures), "verdicts": v, "pass_pct": kpi["pass_pct"], "recapture_pct": kpi["recapture_pct"],
             "triage": tri["counts"], "incidents": [i["title"] for i in ctx.created["incidents"]],
             "proposals": [{"title": p["title"], "status": p["status"]} for p in ctx.created["proposals"]],
             "window_hours": snap.window_hours}
    ai_text, ai_info = (None, {"used": False, "reason": "replay (deterministic only)"}) if ctx.dry else llm.summarize(facts)
    if ai_text:
        L[3:3] = ["## AI executive summary (numbers verified against agent facts)", ai_text, ""]
    body = "\n".join(L)
    d = ctx.authorize("reporter", "generate_report", "report:shift")
    rep = {"id": ctx.new_id("RPT"), "created_at": now_iso(), "kind": "shift", "title": L[0][2:], "body": body,
           "citations": sorted(cites), "ai": ai_info, "allowed": d["decision"] == "allow"}
    if d["decision"] != "allow":
        ctx.step("reporter", "Report not published - blocked by guardrail", d, status="blocked")
    else:
        ctx.step("reporter", f"Shift report drafted ({len(cites)} citations"
                 + (", AI summary added)" if ai_text else ")"), {"ai_summary": ai_info}, citations=sorted(cites)[:25])
    return rep


def auditor(ctx, rep):
    conn, findings = ctx.conn, []
    if not ctx.dry:
        chain = ledger.verify(conn)
        findings.append({"check": "Ledger hash chain intact", "ok": chain["ok"],
                         "detail": f"{chain['count']} entries" + ("" if chain["ok"] else f", broken at seq {chain['broken_at']}")})
    known = {r["id"] for r in rows(conn, "SELECT id FROM captures")}
    missing = [c for c in rep["citations"] if c not in known]
    findings.append({"check": "Every report citation exists", "ok": not missing,
                     "detail": f"{len(rep['citations'])} cited" + (f", missing {missing[:5]}" if missing else "")})
    for p in ctx.created["proposals"]:
        findings.append({"check": f"Proposal {p['id']} has Critic assessment", "ok": bool(p["critic"]), "detail": p["status"]})
    bad_versions = rows(conn, "SELECT t.version, t.uv, p.status, p.decided_by FROM thresholds t LEFT JOIN proposals p "
                              "ON p.id=t.proposal_id WHERE t.version>1 AND t.proposal_id IS NOT NULL "
                              "AND (p.status NOT IN ('applied','superseded') OR p.decided_by IS NULL)")
    findings.append({"check": "Every applied threshold has a human approval", "ok": not bad_versions,
                     "detail": "all versions traceable" if not bad_versions else f"{len(bad_versions)} untraceable"})
    blocked_applied = one(conn, "SELECT COUNT(*) n FROM thresholds t JOIN proposals p ON p.id=t.proposal_id "
                                "WHERE p.critic LIKE '%\"result\": \"fail\"%' AND p.kind='threshold_change'")["n"]
    findings.append({"check": "No Critic-blocked proposal was applied", "ok": blocked_applied == 0, "detail": f"{blocked_applied} found"})
    ok = all(x["ok"] for x in findings)
    verdict = "AUDIT_PASSED" if ok else "AUDIT_FAILED"
    ctx.step("auditor", f"{verdict}: {sum(x['ok'] for x in findings)}/{len(findings)} checks", {"findings": findings},
             status="ok" if ok else "alert")
    rep["audit"] = {"verdict": verdict, "findings": findings}
    if not ctx.dry and rep["allowed"]:
        with tx(conn):
            conn.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?)",
                         (rep["id"], rep["created_at"], "shift", rep["title"], rep["body"], json.dumps(rep["citations"]),
                          json.dumps(rep["audit"]), ctx.run_id))
            ledger.append(conn, "REPORT_SEALED", "agent:auditor", rep["id"],
                          {"sha256": ledger.sha256(rep["body"]), "verdict": verdict, "citations": len(rep["citations"])})
    return rep["audit"]


# ------------------------------------------------------------------ orchestration
def _digest(plan, tri, drift_res, cal, verdicts):
    return {"plan": {"calibrate": plan["calibrate"], "unlabelled": plan["unlabelled"]},
            "triage": sorted((x["capture_id"], x["cause"]) for x in tri["items"]),
            "drift": {uv: {k: d.get(k) for k in ("drift_active", "shift_sigma", "headroom_pct", "overexposed_pct_peak")}
                      for uv, d in drift_res.items()},
            "calibration": {uv: {"recommended": r["recommended"], "proposed": r["proposed"]} for uv, r in cal["recs"].items()},
            "critic": sorted((v["uv"] or "", v["kind"], v["status"]) for v in verdicts)}


def run_cycle(conn, as_of=None, window_hours=None, params=None, dry=False):
    pol = get_policy(conn)
    if params is None:
        ver, _ = active_thresholds(conn)
        last = one(conn, "SELECT MAX(ts) ts FROM captures")
        params = {"as_of": as_of or (last["ts"] if last and last["ts"] else now_iso()),
                  "window_hours": float(window_hours or pol["data_window_hours"]), "threshold_version": ver,
                  "started_at": now_iso(),
                  "policy": {k: pol[k] for k in pol if k not in ("permissions", "agents_paused")}}
    ctx = RunContext(conn, "replay" if dry else "cycle", params, dry)
    started = now_iso()
    if not dry:
        with tx(conn):
            conn.execute("INSERT INTO agent_runs(id,kind,started_at,status,params) VALUES(?,?,?,?,?)",
                         (ctx.run_id, "cycle", started, "running", json.dumps(params)))
            ledger.append(conn, "RUN_STARTED", "orchestrator", ctx.run_id, params)
        events.publish("run", run_id=ctx.run_id, status="running")
    status, digest, out = "completed", None, {}
    snap = Snapshot(conn, params)
    try:
        d = ctx.authorize("planner", "read_data", "captures")
        if d["decision"] != "allow":
            raise BudgetExceeded("read access denied")
        plan = planner(ctx, snap)
        tri = triage(ctx, snap)
        drift_res = drift(ctx, snap)
        cal = calibration(ctx, snap, plan)
        verdicts = critic(ctx, snap, drift_res)
        kpi = metrics.kpis(conn, snap.since, snap.as_of)
        rep = reporter(ctx, snap, plan, tri, drift_res, cal, verdicts, kpi)
        audit = auditor(ctx, rep)
        digest = _digest(plan, tri, drift_res, cal, verdicts)
        out = {"plan": plan, "triage_counts": tri["counts"], "triage_accuracy_pct": tri["accuracy_pct"],
               "drift": drift_res, "calibration": cal["recs"], "critic": verdicts, "report_id": rep["id"],
               "audit": audit, "incidents": [i["id"] for i in ctx.created["incidents"]],
               "proposals": [p["id"] for p in ctx.created["proposals"]]}
    except BudgetExceeded as e:
        status = "aborted"
        ctx.steps.append({"n": ctx.n + 1, "agent": "orchestrator", "title": f"Run aborted by guardrail: {e}",
                          "detail": {}, "citations": [], "status": "blocked", "t": now_iso()})
    output_hash = ledger.sha256(ledger.canonical(digest)) if digest else None
    result = {"run_id": ctx.run_id, "status": status, "params": params, "input_hash": snap.input_hash,
              "output_hash": output_hash, "digest": digest, "steps": ctx.steps, **out}
    if not dry:
        with tx(conn):
            conn.execute("UPDATE agent_runs SET finished_at=?, status=?, steps=?, input_hash=?, output_hash=?, summary=? "
                         "WHERE id=?", (now_iso(), status, json.dumps(ctx.steps, default=str), snap.input_hash, output_hash,
                                        json.dumps({**out, "digest": digest}, default=str), ctx.run_id))
            ledger.append(conn, "RUN_COMPLETED", "orchestrator", ctx.run_id,
                          {"status": status, "input_hash": snap.input_hash, "output_hash": output_hash,
                           "steps": len(ctx.steps)})
        events.publish("run", run_id=ctx.run_id, status=status)
    return result


def replay(conn, run_id):
    orig = one(conn, "SELECT * FROM agent_runs WHERE id=?", (run_id,))
    if not orig or orig["kind"] != "cycle" or not orig["output_hash"]:
        raise ValueError("unknown or unfinished run")
    params = json.loads(orig["params"])
    res = run_cycle(conn, params=params, dry=True)
    o_digest = json.loads(orig["summary"]).get("digest") or {}
    r_digest = json.loads(json.dumps(res["digest"] or {}, default=str))  # same JSON form as the stored one
    diffs = [k for k in set(r_digest) | set(o_digest) if r_digest.get(k) != o_digest.get(k)]
    match = res["output_hash"] == orig["output_hash"]
    rec = {"replay_id": res["run_id"], "replay_of": run_id, "match": match,
           "input_match": res["input_hash"] == orig["input_hash"], "original_output_hash": orig["output_hash"],
           "replay_output_hash": res["output_hash"], "original_input_hash": orig["input_hash"],
           "replay_input_hash": res["input_hash"], "differing_sections": diffs,
           "explanation": "Identical decisions from identical data - the run is reproducible." if match else
           ("Input data changed since the original run (e.g. new labels), so decisions differ in: " + ", ".join(diffs))
           if res["input_hash"] != orig["input_hash"] else "Same data but different decisions: " + ", ".join(diffs)}
    with tx(conn):
        conn.execute("INSERT INTO agent_runs(id,kind,started_at,finished_at,status,steps,input_hash,output_hash,summary,"
                     "params,replay_of) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (res["run_id"], "replay", now_iso(), now_iso(), "match" if match else "diverged",
                      json.dumps(res["steps"], default=str), res["input_hash"], res["output_hash"], json.dumps(rec),
                      orig["params"], run_id))
        ledger.append(conn, "REPLAY_EXECUTED", "orchestrator", run_id,
                      {"replay_id": res["run_id"], "match": match, "input_match": rec["input_match"]})
    return rec
