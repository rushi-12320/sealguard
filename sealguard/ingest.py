"""Import real inspection results (the uv_inspection_results.csv your test scripts write).

Column pattern per image: <UV>_<region>_<field>, e.g. UV2_P1_top_decision, UV2_P1_top_score,
UV2_P1_top_threshold, UV2_P1_top_exposure. A 'capture_id' column is required; 'timestamp' is optional.
Decision values other than PASS / FAIL (OVEREXPOSED, NOT_INSPECTABLE, ...) are stored as not inspectable.
"""
import csv
import io
import re

from . import ledger
from .db import now_iso, tx
from .domain import NOT_INSPECTABLE, UVS, capture_verdict

COL = re.compile(r"^(UV[1-5]|White)_(.+)_(decision|score|threshold|exposure|status|saturated_percent|sat_pct)$", re.I)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ingest_csv(conn, text, source="csv", defect_type=None, actor="operator"):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "capture_id" not in reader.fieldnames:
        raise ValueError("CSV needs a 'capture_id' column")
    fields = {}
    for col in reader.fieldnames:
        m = COL.match(col.strip())
        if m:
            uv, region, field = m.group(1).upper(), m.group(2), m.group(3).lower()
            fields.setdefault((uv, region), {})[field] = col
    if not fields:
        raise ValueError("no <UV>_<region>_decision / _score columns found")

    added, skipped, white = 0, 0, 0
    with tx(conn):
        for row in reader:
            cid = f"{source}:{row['capture_id']}".strip()
            if conn.execute("SELECT 1 FROM captures WHERE id=?", (cid,)).fetchone():
                skipped += 1
                continue
            images = []
            for (uv, region), cols in fields.items():
                if uv not in UVS:
                    white += 1
                    continue
                raw = (row.get(cols.get("decision", ""), "") or "").strip().upper()
                exp = (row.get(cols.get("exposure", cols.get("status", "")), "") or "OK").strip().upper() or "OK"
                if raw not in ("PASS", "FAIL", "OK", "NG"):
                    exp = exp if exp != "OK" else (raw or "UNKNOWN")
                dec = {"OK": "PASS", "NG": "FAIL"}.get(raw, raw) if exp == "OK" else NOT_INSPECTABLE
                if dec not in ("PASS", "FAIL"):
                    dec = NOT_INSPECTABLE
                images.append((cid, uv, region, _f(row.get(cols.get("score", ""))),
                               _f(row.get(cols.get("threshold", ""))), dec, exp,
                               _f(row.get(cols.get("sat_pct", cols.get("saturated_percent", "")))), None))
            if not images:
                continue
            verdict = capture_verdict([(i[2], i[5]) for i in images])
            good = (defect_type or "").lower() == "good"
            conn.execute("INSERT INTO captures(id,ts,source,verdict,defect_type,gt_locations,gt_source) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (cid, row.get("timestamp") or now_iso(), source, verdict, defect_type or None,
                          "" if good else None, "folder" if good else None))
            conn.executemany("INSERT INTO images(capture_id,uv,region,score,threshold,decision,exposure,sat_pct,"
                             "mean_brightness) VALUES(?,?,?,?,?,?,?,?,?)", images)
            added += 1
        ledger.append(conn, "DATA_INGESTED", actor, f"source:{source}",
                      {"added": added, "skipped_duplicates": skipped, "defect_type": defect_type,
                       "content_sha256": ledger.sha256(text)})
    return {"added": added, "skipped_duplicates": skipped, "white_columns_ignored": white,
            "needs_labels": 0 if (defect_type or "").lower() == "good" else added}
