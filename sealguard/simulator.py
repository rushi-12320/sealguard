"""Deterministic line simulator: two pouches per capture, five UV cameras, region scores like PatchCore.

Built-in scenario (so the agents have something real to find):
  * normal good-score spread per UV, a rare overkill tail
  * seal defects of 5 types at marked locations, a few faint ones that escape the threshold
  * UV3 lamp ageing: good scores drift upwards from capture 360 on
  * UV5 exposure burst: many over-exposed images between captures 420 and 470
  * occasional pouch misalignment: many regions of one product fail together, but the seal is fine
  * ~10 % of captures left unlabelled for the labelling workbench
"""
import random
from datetime import datetime, timedelta, timezone

from . import ledger
from .db import active_thresholds, get_setting, now_iso, one, set_setting, tx
from .domain import DEFAULT_THRESHOLDS, UV_REGIONS, UVS, capture_verdict, decide

GOOD = {"UV1": (48, 6), "UV2": (30, 6), "UV3": (62, 7), "UV4": (50, 7), "UV5": (55, 8)}
DEFECT_WEIGHTS = [("burn_through", 3), ("product_in_seal", 3), ("channeling", 3),
                  ("webbing_shift", 2), ("channeling_misalignment", 1)]
DEFECT_RATE = 0.08
INTERVAL_S = 60
DRIFT_UV, DRIFT_START, DRIFT_PER_CAPTURE, DRIFT_MAX = "UV3", 360, 0.07, 30.0
BURST_UV, BURST_RANGE, BURST_P = "UV5", (420, 470), 0.45


def _pick_locations(rng, dtype):
    if dtype == "burn_through":
        return [rng.choice(["P1_top", "P1_left", "P1_bottom", "P1_right", "P2_left", "P2_top", "P2_right", "P2_bottom"])]
    if dtype == "product_in_seal":
        return [rng.choice(["P1_top", "P1_bottom", "P2_top", "P2_bottom"])]
    if dtype == "channeling":
        return [rng.choice(["P1_left", "P1_right", "P2_left", "P2_right"])]
    if dtype == "webbing_shift":
        return [rng.choice(["P1_top", "P1_bottom", "P2_top", "P2_bottom", "P1_left", "P2_right"])]
    p = rng.choice(["P1", "P2"])  # channeling + misalignment: two sides of one product
    return [f"{p}_left", f"{p}_bottom"] if p == "P1" else [f"{p}_right", f"{p}_top"]


def make_capture(seed, index, ts, thresholds):
    rng = random.Random(seed * 1_000_003 + index)
    dtype = "good"
    if rng.random() < DEFECT_RATE:
        dtype = rng.choices([d for d, _ in DEFECT_WEIGHTS], [w for _, w in DEFECT_WEIGHTS])[0]
    locations = [] if dtype == "good" else _pick_locations(rng, dtype)
    misaligned = rng.random() < 0.012
    mis_product = rng.choice(["P1", "P2"])
    faint = dtype in ("channeling", "webbing_shift") and rng.random() < 0.18
    drift = min(DRIFT_MAX, max(0.0, (index - DRIFT_START) * DRIFT_PER_CAPTURE))

    images = []
    for uv in UVS:
        thr = thresholds[uv]
        in_burst = uv == BURST_UV and BURST_RANGE[0] <= index <= BURST_RANGE[1] and rng.random() < BURST_P
        over = in_burst or rng.random() < 0.004
        for region in UV_REGIONS[uv]:
            mu, sd = GOOD[uv]
            score = max(1.0, rng.gauss(mu + (drift if uv == DRIFT_UV else 0.0), sd))
            if rng.random() < 0.003:
                score *= rng.uniform(1.8, 2.4)  # rare overkill tail
            if region in locations:
                score = thr * (rng.uniform(0.70, 0.97) if faint else rng.uniform(1.25, 3.2))
            if misaligned and region.startswith(mis_product):
                score = max(score, thr * rng.uniform(0.95, 1.4))
            exposure, sat, mean_b = "OK", round(rng.uniform(0, 1.5), 2), round(rng.gauss(122, 8), 1)
            if over:
                exposure, sat, mean_b = "OVEREXPOSED", round(rng.uniform(8, 30), 2), round(rng.uniform(205, 240), 1)
                score *= rng.uniform(1.5, 3.5)  # glare looks anomalous to PatchCore
            score = round(score, 2)
            images.append({"uv": uv, "region": region, "score": score, "threshold": thr,
                           "decision": decide(score, thr, exposure), "exposure": exposure,
                           "sat_pct": sat, "mean_brightness": mean_b})
    labelled = rng.random() >= 0.10
    cap = {"id": f"C{index:06d}", "ts": ts, "source": "simulator",
           "verdict": capture_verdict([(i["region"], i["decision"]) for i in images]),
           "defect_type": dtype if labelled else None,
           "gt_locations": ",".join(locations) if labelled else None,
           "gt_source": "simulator" if labelled else None,
           "_truth": {"defect_type": dtype, "locations": locations, "misaligned": misaligned}}
    return cap, images


def _insert(conn, cap, images):
    conn.execute("INSERT OR IGNORE INTO captures(id,ts,source,verdict,defect_type,gt_locations,gt_source) "
                 "VALUES(?,?,?,?,?,?,?)", (cap["id"], cap["ts"], cap["source"], cap["verdict"],
                                           cap["defect_type"], cap["gt_locations"], cap["gt_source"]))
    conn.executemany("INSERT OR IGNORE INTO images(capture_id,uv,region,score,threshold,decision,exposure,sat_pct,"
                     "mean_brightness) VALUES(?,?,?,?,?,?,?,?,?)",
                     [(cap["id"], i["uv"], i["region"], i["score"], i["threshold"], i["decision"], i["exposure"],
                       i["sat_pct"], i["mean_brightness"]) for i in images])


def ensure_initialized(conn, n=720, seed=7):
    """Genesis entry, threshold version 1 and a 12-hour demo history (only on an empty database)."""
    if one(conn, "SELECT 1 x FROM ledger LIMIT 1"):
        return False
    with tx(conn):
        ledger.append(conn, "GENESIS", "system", "ledger", {"note": "SealGuard audit ledger created"})
        for uv, v in DEFAULT_THRESHOLDS.items():
            conn.execute("INSERT INTO thresholds VALUES(1,?,?,?,?,?,?)",
                         (uv, v, now_iso(), "system", None, "initial thresholds"))
        ledger.append(conn, "THRESHOLDS_INITIALIZED", "system", "thresholds:v1", DEFAULT_THRESHOLDS)
        set_setting(conn, "sim_state", {"seed": seed, "next_index": 0})
    generate(conn, n, end=datetime.now(timezone.utc))
    return True


def generate(conn, n, end=None):
    """Append n captures. With `end`, they are spaced so the last one lands at `end`; else they follow on."""
    state = get_setting(conn, "sim_state", {"seed": 7, "next_index": 0})
    _, thr = active_thresholds(conn)
    thr = {**DEFAULT_THRESHOLDS, **thr}
    if end is not None:
        t0 = end - timedelta(seconds=INTERVAL_S * (n - 1))
    else:
        last = one(conn, "SELECT MAX(ts) ts FROM captures")
        t0 = (datetime.fromisoformat(last["ts"]) + timedelta(seconds=INTERVAL_S)) if last and last["ts"] \
            else datetime.now(timezone.utc)
    start_index = state["next_index"]
    counts = {}
    with tx(conn):
        for k in range(n):
            ts = (t0 + timedelta(seconds=INTERVAL_S * k)).replace(microsecond=0).isoformat()
            cap, images = make_capture(state["seed"], start_index + k, ts, thr)
            _insert(conn, cap, images)
            counts[cap["verdict"]] = counts.get(cap["verdict"], 0) + 1
        state["next_index"] = start_index + n
        set_setting(conn, "sim_state", state)
        ledger.append(conn, "DATA_SIMULATED", "simulator", f"captures:{start_index}-{start_index + n - 1}",
                      {"count": n, "verdicts": counts})
    return {"generated": n, "first_index": start_index, "verdicts": counts}
