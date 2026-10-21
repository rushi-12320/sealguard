# SealGuard — Agentic Quality Ops Center

An agentic AI control room for the 5-camera UV seal inspection line (PatchCore models UV1–UV5, two pouches per capture).
Agents investigate rejects, watch for camera drift, propose threshold changes and write the shift report.
Guardrails decide what they are allowed to do, people approve anything that touches production, and every
step is sealed in a hash-chained audit ledger that can be verified and replayed.

## Run it

```
pip install -r requirements.txt
python -m sealguard            # http://127.0.0.1:8000
```
Windows: double-click `run.bat`. First start creates `data/sealguard.db` with 12 h of simulated line data
and runs the agents once. Delete that file to start over. Tests: `python -m pytest -q tests`.

Type your name in the **Operator** box (top right): approvals, labels, policy changes and the kill switch are signed with it.

## Demo

**Live demo:** Not deployed yet. To create a public demo after pushing this repository to GitHub, sign in to Render,
choose **New → Blueprint**, and select this repository. Render will read `render.yaml` and create the free,
read-only demo service. Once deployment succeeds, add the service's `https://...onrender.com` URL here.

Render's free web service sleeps after 15 minutes without traffic and loses local filesystem changes when it sleeps
or redeploys. The SQLite database is in `/tmp`; the demo starts with generated sample data and must not be used for
real inspection data. The public service is unauthenticated; writes are disabled by `SEALGUARD_READ_ONLY=true`.
Render documents its free tier as intended for testing and hobby use, not production.

You can also use the [Cloud Run demo instructions](#free-public-demo-on-google-cloud-run); Cloud Run requires a
Google Cloud project with billing enabled, and its free usage allowance does not guarantee a $0 bill.

## Free public demo on Google Cloud Run

The repository includes a Dockerfile for a small, single-instance demo. Cloud Run's free usage allowance is
subject to Google's current terms, region, and billing configuration; it is not a guaranteed $0 bill. Set a
budget alert in Google Cloud before deploying.

1. Install and authenticate the Google Cloud CLI, select a project with billing enabled, and enable Cloud Run
   and Cloud Build when prompted:

   ```
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```

2. From the repository root, deploy the public, read-only demo:

   ```
   gcloud run deploy sealguard \
     --source . \
     --region us-central1 \
     --allow-unauthenticated \
     --port 8080 \
     --max 1 \
     --concurrency 10 \
     --memory 512Mi \
     --set-env-vars SEALGUARD_DB=/tmp/sealguard.db,SEALGUARD_READ_ONLY=true
   ```

   Use a region available to your project. The command prints the public service URL when deployment completes.

**Demo limitations:** Cloud Run's local container filesystem (including `/tmp/sealguard.db`) is temporary, not
backed up, and not shared durable storage. The seeded/sample data and any changes disappear when the instance is
replaced; do not import real inspection data. `SEALGUARD_READ_ONLY=true` blocks API write methods while leaving
the dashboard readable. The app is still unauthenticated, so treat all displayed data as public. Setting
`--max 1` limits scale-out but does not make SQLite durable or guarantee a free bill.

For real production use, do not deploy this demo configuration as-is. Add authenticated identities and roles,
use durable database storage with backups and migrations, keep the public API protected, configure monitoring
and alerting, and validate recovery and access controls before connecting production inspection data.

## What is in it

| Area | What it does |
|---|---|
| Command Center | KPIs, per-UV headroom (how far good scores sit below the threshold), score trend vs threshold, open incidents |
| Line Monitor | Every capture with UV1–UV5 results; click for the seal-region map, region scores, triage and ground truth. Live feed button |
| Agent Ops | Runs the 7-agent pipeline, shows every step with citations; run history with input/output hashes and **Replay** |
| Calibration Lab | Good vs defect score histogram per UV; drag a threshold to see exactly which pouches escape or are falsely rejected |
| Approvals | Proposals with Critic findings; approve / reject (signed); threshold versions with rollback |
| Guardrails | Kill switch, policy limits, permission tiers, guard-decision log, **red-team suite** (17 attacks) |
| Audit Ledger | SHA-256 hash chain + Merkle root, verify, tamper test (on an in-memory copy) |
| Labelling | Click the seal side where the defect is — the UV1–UV5 ground truth is derived from the camera layout |
| Reports | Sealed shift reports; defect type × UV table (Pass/Fail rows, Overkill, Escape, Yield, Detectability) + Excel export |
| Data Sources | Simulator, and CSV import of your real `uv_inspection_results.csv` |

## The agents (deterministic — same data gives the same result)

1. **Planner** – checks the data window and which UVs have enough labelled data to calibrate.
2. **Triage** – classifies every non-PASS capture: *corroborated defect* (failed in both camera views of a seal side),
   *probable defect*, *borderline*, *misalignment* (many regions of one pouch fail moderately) or *image quality*.
   Compares itself with ground truth.
3. **Drift** – CUSUM + shift in σ of good scores per UV, headroom to threshold, over-exposure bursts → incidents and maintenance proposals.
4. **Calibration** – recommends thresholds from labelled scores, capped at the policy step limit, with a what-if for current vs proposed.
5. **Critic** – red-teams every proposal: enough evidence, zero escapes, step limit, false-reject rate, separation (AUROC),
   and **drift masking** (raising a threshold while a camera drifts would hide a lamp problem).
6. **Reporter** – shift report where every statement cites capture IDs. Optional AI summary (see below).
7. **Auditor** – verifies the ledger chain, every citation, every applied threshold's human approval; seals the report.

## Guardrails (enforced in code, `sealguard/guardrails.py`)

- Default deny for unknown actions; `delete_data`, `modify_ledger`, `control_plc`, `retrain_and_deploy_model` are hard-forbidden.
- Agents can only *propose*; `apply_threshold` needs a named human, a passed Critic review and a step within the limit — checked again at apply time.
- Kill switch, proposal rate limit, step budget per run, de-duplication of incidents/proposals.
- Text inside data (label notes, CSV fields) that looks like instructions is flagged and never executed.

## Using your real data

Import `uv_inspection_results.csv` on **Data Sources**. Columns: `capture_id` plus `<UV>_<region>_decision`,
`_score`, `_threshold` (optional `_exposure`), e.g. `UV2_P1_top_score`. Regions: `P1_top, P1_left, P1_bottom, P1_right,
P2_left, P2_top, P2_right, P2_bottom`. Choose the folder's defect type when importing a test set, then mark the
locations on **Labelling**. Camera → region layout is in `sealguard/domain.py` (`UV_REGIONS`) — adjust it if your ROIs differ.

## Optional AI summary

`pip install anthropic`, set `ANTHROPIC_API_KEY` (and optionally `SEALGUARD_LLM_MODEL`). The model only gets the agents'
computed facts; its text is rejected if it contains any number not in those facts. It never makes decisions.

## Before real production use

Add authentication / roles (names are currently self-declared), run behind HTTPS, back up `data/`, and connect the
inspection PC's result writer to the CSV import (or a small API call) instead of the simulator.
