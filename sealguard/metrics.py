"""Scores, labels and the numbers every agent and report uses."""
import math
from statistics import mean, pstdev

from .db import active_thresholds, rows
from .domain import DEFECT_LABELS, DEFECT_TYPES, FAIL, NOT_INSPECTABLE, PASS, UV_REGIONS, UVS


def quantile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def auroc(neg, pos):
    """Mann-Whitney U / (n_neg * n_pos), ties counted half."""
    if not neg or not pos:
        return None
    allv = sorted([(v, 0) for v in neg] + [(v, 1) for v in pos])
    ranks, i = {}, 0
    rank_sum_pos = 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        rank_sum_pos += r * sum(1 for k in range(i, j + 1) if allv[k][1] == 1)
        i = j + 1
    u = rank_sum_pos - len(pos) * (len(pos) + 1) / 2
    return u / (len(neg) * len(pos))


def labelled_regions(conn, uv=None, until=None):
    """Region-level rows of labelled, inspectable images with a ground-truth flag 'positive'."""
    sql = ("SELECT i.capture_id, i.uv, i.region, i.score, i.threshold, i.decision, i.exposure, c.ts, "
           "c.defect_type, c.gt_locations FROM images i JOIN captures c ON c.id=i.capture_id "
           "WHERE c.gt_source IS NOT NULL AND c.not_inspectable=0 AND i.exposure='OK' AND i.score IS NOT NULL")
    args = []
    if uv:
        sql += " AND i.uv=?"
        args.append(uv)
    if until:
        sql += " AND c.ts<=?"
        args.append(until)
    out = rows(conn, sql + " ORDER BY c.ts, i.capture_id, i.region", args)
    for r in out:
        r["positive"] = r["region"] in set((r["gt_locations"] or "").split(","))
    return out


def score_sets(conn, uv, until=None):
    rs = labelled_regions(conn, uv, until)
    return [r for r in rs if not r["positive"]], [r for r in rs if r["positive"]]


def recommend(neg, pos):
    """Threshold proposal from labelled scores (lists of floats)."""
    if not neg:
        return None
    gmax, gmean, gsd = max(neg), mean(neg), pstdev(neg) if len(neg) > 1 else 0.0
    res = {"n_good": len(neg), "n_defect": len(pos), "good_max": round(gmax, 3),
           "good_p99": round(quantile(neg, 0.99), 3), "defect_min": round(min(pos), 3) if pos else None,
           "auroc": round(auroc(neg, pos), 4) if pos else None}
    # robust upper edge of "good": 99.5th percentile, so a few outliers (misaligned packs) don't drive it
    gref = quantile(neg, 0.995)
    res["good_p995"] = round(gref, 3)
    if not pos:
        res.update(threshold=round(max(gref * 1.10, gmean + 4 * gsd), 2), method="good-only (p99.5 +10% / mean+4σ)")
    elif min(pos) > gref:
        res.update(threshold=round(min((gref + min(pos)) / 2, min(pos) - 0.01), 2),
                   method="midpoint between good p99.5 and lowest defect")
    else:
        res.update(threshold=round(min(pos) - 0.01, 2), method="overlap: just below lowest defect (zero escapes)")
    return res


def whatif(neg, pos, thr, limit=25):
    fp = [r for r in neg if r["score"] >= thr]
    fn = [r for r in pos if r["score"] < thr]
    n, p = len(neg), len(pos)
    return {"threshold": round(thr, 2), "false_rejects": len(fp), "escapes": len(fn),
            "false_reject_pct": round(100 * len(fp) / n, 3) if n else None,
            "escape_pct": round(100 * len(fn) / p, 3) if p else None,
            "detection_pct": round(100 * (p - len(fn)) / p, 3) if p else None,
            "n_good": n, "n_defect": p,
            "false_reject_examples": [f"{r['capture_id']}/{r['region']}" for r in fp[:limit]],
            "escape_examples": [f"{r['capture_id']}/{r['region']}" for r in fn[:limit]]}


def histogram(neg, pos, thr_list, bins=40):
    vals = [r["score"] for r in neg + pos]
    if not vals:
        return {"edges": [], "good": [], "defect": []}
    lo, hi = 0.0, max(max(vals), *thr_list) * 1.05
    w = (hi - lo) / bins
    g, d = [0] * bins, [0] * bins
    for r in neg:
        g[min(bins - 1, int((r["score"] - lo) / w))] += 1
    for r in pos:
        d[min(bins - 1, int((r["score"] - lo) / w))] += 1
    return {"edges": [round(lo + k * w, 2) for k in range(bins + 1)], "good": g, "defect": d}


def window_images(conn, since=None, until=None):
    sql = ("SELECT i.*, c.ts, c.verdict, c.defect_type, c.gt_locations, c.gt_source FROM images i "
           "JOIN captures c ON c.id=i.capture_id WHERE 1=1")
    args = []
    if since:
        sql += " AND c.ts>?"
        args.append(since)
    if until:
        sql += " AND c.ts<=?"
        args.append(until)
    return rows(conn, sql + " ORDER BY c.ts, i.capture_id, i.uv, i.region", args)


def defect_uv_table(conn):
    """Your customer report: per defect type x UV, rows by ground truth (Pass/Fail) vs prediction.
    Image level: a UV image is predicted FAIL if any of its regions failed; ground truth FAIL if a marked
    defect location lies in that UV's regions. Not-inspectable images are counted separately."""
    data = rows(conn, "SELECT i.capture_id, i.uv, i.region, i.decision, c.defect_type, c.gt_locations "
                      "FROM images i JOIN captures c ON c.id=i.capture_id "
                      "WHERE c.gt_source IS NOT NULL AND c.not_inspectable=0")
    per_image = {}
    for r in data:
        k = (r["capture_id"], r["uv"])
        e = per_image.setdefault(k, {"dtype": r["defect_type"] or "good", "locs": set((r["gt_locations"] or "").split(",")) - {""},
                                     "decisions": []})
        e["decisions"].append(r["decision"])
    agg = {}
    for (cid, uv), e in per_image.items():
        gt = FAIL if e["locs"] & set(UV_REGIONS[uv]) else PASS
        ds = e["decisions"]
        pred = FAIL if FAIL in ds else (PASS if PASS in ds else NOT_INSPECTABLE)
        a = agg.setdefault((e["dtype"], uv, gt), {"total": 0, "pass": 0, "fail": 0, "not_inspectable": 0})
        if pred == NOT_INSPECTABLE:
            a["not_inspectable"] += 1
            continue
        a["total"] += 1
        a["pass" if pred == PASS else "fail"] += 1
    out = []
    for dtype in DEFECT_TYPES:
        for uv in UVS:
            for gt in (PASS, FAIL):
                a = agg.get((dtype, uv, gt))
                if not a:
                    continue
                t = a["total"]
                out.append({"defect_type": dtype, "defect": DEFECT_LABELS.get(dtype, dtype), "uv": uv, "category": gt.title(),
                            **a, "overkill": a["fail"] if gt == PASS else 0, "escape": a["pass"] if gt == FAIL else 0,
                            "yield": round(a["pass"] / t, 4) if gt == PASS and t else None,
                            "detectability": round(a["fail"] / t, 4) if gt == FAIL and t else None})
    return out


def kpis(conn, since=None, until=None):
    caps = rows(conn, "SELECT id, verdict, gt_source, defect_type, gt_locations FROM captures WHERE 1=1"
                + (" AND ts>?" if since else "") + (" AND ts<=?" if until else ""),
                [x for x in (since, until) if x])
    imgs = window_images(conn, since, until)
    ver, thr = active_thresholds(conn)
    n = len(caps)
    verdicts = {}
    for c in caps:
        verdicts[c["verdict"]] = verdicts.get(c["verdict"], 0) + 1
    inspectable = n - verdicts.get("RECAPTURE", 0)
    per_uv = {}
    for uv in UVS:
        u = [i for i in imgs if i["uv"] == uv]
        ok = [i for i in u if i["exposure"] == "OK"]
        goods = [i["score"] for i in ok if i["decision"] == PASS]
        recent = goods[-150:]
        p99 = quantile(recent, 0.99) if recent else None
        per_uv[uv] = {"images": len(u), "fail_pct": round(100 * sum(i["decision"] == FAIL for i in u) / len(u), 2) if u else 0,
                      "overexposed_pct": round(100 * (len(u) - len(ok)) / len(u), 2) if u else 0,
                      "good_mean_recent": round(mean(recent), 2) if recent else None,
                      "good_p99_recent": round(p99, 2) if p99 else None, "threshold": thr.get(uv),
                      "headroom_pct": round(100 * (thr[uv] - p99) / thr[uv], 1) if p99 and thr.get(uv) else None}
    esc = over = 0
    for i in imgs:
        if i["gt_source"] is None or i["exposure"] != "OK":
            continue
        pos = i["region"] in set((i["gt_locations"] or "").split(","))
        esc += pos and i["decision"] == PASS
        over += (not pos) and i["decision"] == FAIL
    return {"captures": n, "verdicts": verdicts,
            "pass_pct": round(100 * verdicts.get("PASS", 0) / inspectable, 2) if inspectable else None,
            "recapture_pct": round(100 * verdicts.get("RECAPTURE", 0) / n, 2) if n else None,
            "unlabelled": sum(1 for c in caps if c["gt_source"] is None),
            "labelled_escapes_regions": esc, "labelled_overkill_regions": over,
            "threshold_version": ver, "thresholds": thr, "per_uv": per_uv}


def trend(conn, uv, since=None, until=None, bucket=20):
    imgs = [i for i in window_images(conn, since, until) if i["uv"] == uv]
    by_cap = {}
    for i in imgs:
        by_cap.setdefault(i["capture_id"], []).append(i)
    caps = list(by_cap.items())
    pts = []
    for k in range(0, len(caps), bucket):
        chunk = [i for _, lst in caps[k:k + bucket] for i in lst]
        ok = [i for i in chunk if i["exposure"] == "OK"]
        goods = [i["score"] for i in ok if i["decision"] == PASS]
        pts.append({"ts": chunk[-1]["ts"], "good_mean": round(mean(goods), 2) if goods else None,
                    "good_p95": round(quantile(goods, 0.95), 2) if goods else None,
                    "threshold": chunk[-1]["threshold"],
                    "fail_pct": round(100 * sum(i["decision"] == FAIL for i in chunk) / len(chunk), 2),
                    "overexposed_pct": round(100 * (len(chunk) - len(ok)) / len(chunk), 2)})
    return pts
