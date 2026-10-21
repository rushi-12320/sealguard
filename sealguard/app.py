"""SealGuard API + web UI.   Run:  python -m sealguard   (or  uvicorn sealguard.app:app)"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agents, approvals, db, events, ingest, ledger, llm, metrics, redteam, reports, simulator
from .domain import DEFECT_LABELS, DEFECT_TYPES, LOCATION_UVS, LOCATIONS, UV_REGIONS, UVS, uv_labels_from_locations
from .guardrails import DEFAULT_POLICY, get_policy, set_policy

STATIC = os.path.join(os.path.dirname(__file__), "..", "static")


@asynccontextmanager
async def lifespan(_app):
    c = db.connect()
    if simulator.ensure_initialized(c):
        agents.run_cycle(c)  # first run so the dashboard has agent results
    c.close()
    yield


app = FastAPI(title="SealGuard - Agentic Quality Ops Center", version="1.0.0", lifespan=lifespan)


def read_only_mode():
    return os.environ.get("SEALGUARD_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


@app.middleware("http")
async def enforce_read_only_demo(request, call_next):
    if read_only_mode() and request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse(
            status_code=403,
            content={"detail": "This public demo is read-only."},
        )
    return await call_next(request)


def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


def _j(v, default=None):
    return json.loads(v) if v else default


def _window(c, hours=None):
    last = db.one(c, "SELECT MAX(ts) ts FROM captures")
    until = last["ts"] if last and last["ts"] else db.now_iso()
    hours = hours or get_policy(c)["data_window_hours"]
    return (datetime.fromisoformat(until) - timedelta(hours=hours)).isoformat(), until


# ------------------------------------------------------------------ overview
@app.get("/api/health")
def health(c=Depends(conn)):
    return {"ok": True, "captures": db.one(c, "SELECT COUNT(*) n FROM captures")["n"], "llm": llm.available()}


@app.get("/api/domain")
def domain():
    return {"uvs": UVS, "uv_regions": UV_REGIONS, "locations": LOCATIONS, "location_uvs": LOCATION_UVS,
            "defect_types": DEFECT_TYPES, "defect_labels": DEFECT_LABELS}


@app.get("/api/overview")
def overview(hours: float = None, c=Depends(conn)):
    since, until = _window(c, hours)
    k = metrics.kpis(c, since, until)
    chain = ledger.verify(c)
    last_run = db.one(c, "SELECT id, started_at, status, output_hash FROM agent_runs WHERE kind='cycle' "
                         "ORDER BY started_at DESC LIMIT 1")
    return {"window": {"since": since, "until": until}, "kpis": k,
            "ledger": {k2: chain[k2] for k2 in ("ok", "count", "merkle_root", "broken_at")},
            "policy": {"agents_paused": get_policy(c)["agents_paused"]},
            "pending_proposals": db.one(c, "SELECT COUNT(*) n FROM proposals WHERE status='pending_approval'")["n"],
            "blocked_proposals": db.one(c, "SELECT COUNT(*) n FROM proposals WHERE status='critic_blocked'")["n"],
            "open_incidents": db.one(c, "SELECT COUNT(*) n FROM incidents WHERE status='open'")["n"],
            "unlabelled": db.one(c, "SELECT COUNT(*) n FROM captures WHERE gt_source IS NULL")["n"],
            "last_run": last_run, "llm": llm.available(), "read_only": read_only_mode()}


@app.get("/api/trend/{uv}")
def trend(uv: str, hours: float = None, bucket: int = 20, c=Depends(conn)):
    since, until = _window(c, hours)
    return metrics.trend(c, uv, since, until, bucket)


# ------------------------------------------------------------------ captures & labels
@app.get("/api/captures")
def captures(limit: int = 60, offset: int = 0, verdict: str = None, unlabelled: bool = False, q: str = None,
             c=Depends(conn)):
    sql, args = "SELECT * FROM captures WHERE 1=1", []
    if verdict:
        sql += " AND verdict=?"
        args.append(verdict)
    if unlabelled:
        sql += " AND gt_source IS NULL"
    if q:
        sql += " AND id LIKE ?"
        args.append(f"%{q}%")
    total = db.one(c, sql.replace("SELECT *", "SELECT COUNT(*) n"), args)["n"]
    caps = db.rows(c, sql + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset])
    ids = [x["id"] for x in caps]
    imgs = db.rows(c, f"SELECT capture_id, uv, region, decision, score, threshold, exposure FROM images "
                      f"WHERE capture_id IN ({','.join('?' * len(ids))})", ids) if ids else []
    grid = {}
    for i in imgs:
        grid.setdefault(i["capture_id"], {}).setdefault(i["uv"], []).append(i)
    for x in caps:
        g = grid.get(x["id"], {})
        x["uv"] = {uv: ("FAIL" if any(i["decision"] == "FAIL" for i in g.get(uv, [])) else
                        "NOT_INSPECTABLE" if any(i["decision"] == "NOT_INSPECTABLE" for i in g.get(uv, [])) else
                        "PASS" if g.get(uv) else None) for uv in UVS}
    return {"total": total, "items": caps}


@app.get("/api/captures/{cid}")
def capture(cid: str, c=Depends(conn)):
    cap = db.one(c, "SELECT * FROM captures WHERE id=?", (cid,))
    if not cap:
        raise HTTPException(404, "capture not found")
    imgs = db.rows(c, "SELECT * FROM images WHERE capture_id=? ORDER BY uv, region", (cid,))
    locs = [x for x in (cap["gt_locations"] or "").split(",") if x]
    return {"capture": cap, "images": imgs, "triage": agents.classify_capture(imgs) if cap["verdict"] != "PASS" else None,
            "uv_ground_truth": uv_labels_from_locations(locs) if cap["gt_source"] else None}


class LabelIn(BaseModel):
    defect_type: str
    locations: list = Field(default_factory=list)
    by: str
    note: str = ""
    not_inspectable: bool = False


@app.post("/api/captures/{cid}/label")
def label(cid: str, body: LabelIn, c=Depends(conn)):
    if body.defect_type not in DEFECT_TYPES:
        raise HTTPException(400, "unknown defect type")
    if body.defect_type != "good" and not body.locations and not body.not_inspectable:
        raise HTTPException(400, "mark at least one defect location")
    if any(x not in LOCATIONS for x in body.locations):
        raise HTTPException(400, "unknown location")
    try:
        res = approvals.set_label(c, cid, body.defect_type, body.locations, body.by, body.note, body.not_inspectable)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["uv_ground_truth"] = uv_labels_from_locations(body.locations if body.defect_type != "good" else [])
    return res


class TickIn(BaseModel):
    n: int = 10


@app.post("/api/sim/tick")
def sim_tick(body: TickIn, c=Depends(conn)):
    res = simulator.generate(c, max(1, min(body.n, 500)))
    events.publish("captures", count=res["generated"])
    return res


class IngestIn(BaseModel):
    content: str
    source: str = "csv"
    defect_type: Optional[str] = None
    by: str = "operator"


@app.post("/api/ingest/csv")
def ingest_csv(body: IngestIn, c=Depends(conn)):
    if body.defect_type and body.defect_type not in DEFECT_TYPES:
        raise HTTPException(400, "unknown defect type")
    try:
        res = ingest.ingest_csv(c, body.content, body.source.replace(" ", "_") or "csv", body.defect_type, f"human:{body.by}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    events.publish("captures", count=res["added"])
    return res


# ------------------------------------------------------------------ calibration
@app.get("/api/calibration/{uv}")
def calibration(uv: str, threshold: float = None, c=Depends(conn)):
    if uv not in UVS:
        raise HTTPException(404, "unknown UV")
    neg, pos = metrics.score_sets(c, uv)
    _, thr = db.active_thresholds(c)
    cur = thr.get(uv)
    rec = metrics.recommend([r["score"] for r in neg], [r["score"] for r in pos])
    t = threshold if threshold is not None else (rec["threshold"] if rec else cur)
    return {"uv": uv, "current": cur, "recommendation": rec, "whatif_current": metrics.whatif(neg, pos, cur),
            "whatif": metrics.whatif(neg, pos, t),
            "histogram": metrics.histogram(neg, pos, [cur, t] + ([rec["threshold"]] if rec else []))}


# ------------------------------------------------------------------ agents & runs
class RunIn(BaseModel):
    window_hours: Optional[float] = None


@app.post("/api/agents/run")
def run_agents(body: RunIn, c=Depends(conn)):
    res = agents.run_cycle(c, window_hours=body.window_hours)
    return {k: res[k] for k in ("run_id", "status", "input_hash", "output_hash", "steps")} | \
        {k: res.get(k) for k in ("audit", "critic", "triage_counts", "report_id")}


@app.get("/api/runs")
def runs(c=Depends(conn)):
    rs = db.rows(c, "SELECT id, kind, started_at, finished_at, status, input_hash, output_hash, replay_of, summary "
                    "FROM agent_runs ORDER BY started_at DESC LIMIT 100")
    for r in rs:
        s = _j(r.pop("summary"), {})
        r["audit"] = (s.get("audit") or {}).get("verdict") if r["kind"] == "cycle" else None
        r["match"] = s.get("match") if r["kind"] == "replay" else None
    return rs


@app.get("/api/runs/{rid}")
def run(rid: str, c=Depends(conn)):
    r = db.one(c, "SELECT * FROM agent_runs WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "run not found")
    for k in ("steps", "summary", "params"):
        r[k] = _j(r[k])
    return r


@app.post("/api/runs/{rid}/replay")
def replay(rid: str, c=Depends(conn)):
    try:
        return agents.replay(c, rid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/incidents")
def incidents(c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM incidents ORDER BY created_at DESC LIMIT 200")
    for r in rs:
        r["detail"], r["evidence"] = _j(r["detail"], {}), _j(r["evidence"], [])
    return rs


class AckIn(BaseModel):
    by: str


@app.post("/api/incidents/{iid}/ack")
def ack(iid: str, body: AckIn, c=Depends(conn)):
    if not body.by.strip():
        raise HTTPException(400, "name required")
    with db.tx(c):
        n = c.execute("UPDATE incidents SET status='acknowledged' WHERE id=? AND status='open'", (iid,)).rowcount
        if n:
            ledger.append(c, "INCIDENT_ACKNOWLEDGED", f"human:{body.by}", iid, {})
    return {"ok": bool(n)}


# ------------------------------------------------------------------ approvals
@app.get("/api/proposals")
def proposals(c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM proposals ORDER BY created_at DESC LIMIT 200")
    for r in rs:
        r["payload"], r["evidence"], r["critic"] = _j(r["payload"], {}), _j(r["evidence"], []), _j(r["critic"], [])
    return rs


class DecisionIn(BaseModel):
    decision: str
    by: str
    note: str = ""


@app.post("/api/proposals/{pid}/decision")
def decision(pid: str, body: DecisionIn, c=Depends(conn)):
    try:
        return approvals.decide(c, pid, body.decision, body.by, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/thresholds")
def thresholds(c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM thresholds ORDER BY version DESC, uv")
    out = {}
    for r in rs:
        v = out.setdefault(r["version"], {"version": r["version"], "created_at": r["created_at"],
                                          "created_by": r["created_by"], "values": {}, "changes": []})
        v["values"][r["uv"]] = r["value"]
        if r["proposal_id"] or (r["note"] or "").startswith("rollback"):
            v["changes"].append({"uv": r["uv"], "proposal_id": r["proposal_id"], "note": r["note"]})
    return list(out.values())


class RollbackIn(BaseModel):
    version: int
    by: str
    note: str = ""


@app.post("/api/thresholds/rollback")
def rollback(body: RollbackIn, c=Depends(conn)):
    try:
        return approvals.rollback(c, body.version, body.by, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ guardrails
@app.get("/api/policy")
def policy(c=Depends(conn)):
    return {"policy": get_policy(c), "defaults": DEFAULT_POLICY}


class PolicyIn(BaseModel):
    changes: dict
    by: str


@app.put("/api/policy")
def put_policy(body: PolicyIn, c=Depends(conn)):
    if not body.by.strip():
        raise HTTPException(400, "administrator name required")
    return set_policy(c, body.changes, body.by.strip())


class KillIn(BaseModel):
    paused: bool
    by: str


@app.post("/api/policy/killswitch")
def killswitch(body: KillIn, c=Depends(conn)):
    if not body.by.strip():
        raise HTTPException(400, "name required")
    res = set_policy(c, {"agents_paused": body.paused}, body.by.strip())
    events.publish("killswitch", paused=body.paused, by=body.by)
    return res


@app.get("/api/guard-log")
def guard_log(limit: int = 200, c=Depends(conn)):
    rs = db.rows(c, "SELECT * FROM guard_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rs:
        r["reasons"] = _j(r["reasons"], [])
    return rs


@app.post("/api/redteam")
def run_redteam(c=Depends(conn)):
    return redteam.run_suite(c)


# ------------------------------------------------------------------ ledger
@app.get("/api/ledger")
def ledger_list(limit: int = 100, offset: int = 0, event_type: str = None, c=Depends(conn)):
    sql, args = "SELECT * FROM ledger", []
    if event_type:
        sql += " WHERE event_type=?"
        args.append(event_type)
    total = db.one(c, sql.replace("SELECT *", "SELECT COUNT(*) n"), args)["n"]
    rs = db.rows(c, sql + " ORDER BY seq DESC LIMIT ? OFFSET ?", args + [limit, offset])
    types = [r["event_type"] for r in db.rows(c, "SELECT DISTINCT event_type FROM ledger ORDER BY event_type")]
    return {"total": total, "items": rs, "types": types}


@app.get("/api/ledger/verify")
def ledger_verify(c=Depends(conn)):
    return ledger.verify(c)


@app.get("/api/ledger/tamper-demo")
def tamper(seq: int = None, c=Depends(conn)):
    return ledger.tamper_demo(c, seq)


# ------------------------------------------------------------------ reports
@app.get("/api/reports")
def report_list(c=Depends(conn)):
    rs = db.rows(c, "SELECT id, created_at, kind, title, audit, run_id FROM reports ORDER BY created_at DESC LIMIT 100")
    for r in rs:
        r["audit"] = (_j(r["audit"], {}) or {}).get("verdict")
    return rs


@app.get("/api/reports/defect-uv")
def defect_uv(c=Depends(conn)):
    return metrics.defect_uv_table(c)


@app.get("/api/reports/defect-uv.xlsx")
def defect_uv_xlsx(by: str = "web", c=Depends(conn)):
    content = reports.defect_uv_xlsx(c, by)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=defect_uv_report.xlsx"})


@app.get("/api/reports/{rid}")
def report(rid: str, c=Depends(conn)):
    r = db.one(c, "SELECT * FROM reports WHERE id=?", (rid,))
    if not r:
        raise HTTPException(404, "report not found")
    r["citations"], r["audit"] = _j(r["citations"], []), _j(r["audit"], {})
    r["sha256"] = ledger.sha256(r["body"])
    return r


# ------------------------------------------------------------------ live events
@app.get("/api/events/recent")
def events_recent():
    return events.recent(60)


@app.get("/api/events/stream")
async def stream(last_id: int = 0):
    async def gen():
        last = last_id
        for _ in range(3600):
            for e in events.since(last):
                last = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            yield ": keep-alive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------ web UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
