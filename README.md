# CPG ISTA Agentic Platform

An AI-assisted engineering platform for CPG packaging analysis (bottles,
crates, transit-packed goods). Built per the architecture in
[`Cpg Ista Agent Architecture Prompt Doc.pdf`](./Cpg%20Ista%20Agent%20Architecture%20Prompt%20Doc.pdf).

The system behaves like a senior packaging engineer: it asks structured
questions, routes work to specialist agents, fetches real data through tools,
runs guarded deterministic calculations, produces explainable results, visualizes
geometry and risk in 3D, and **requires human approval before any final
analysis runs or report is produced**.

## Architecture at a glance

- **Backend**: FastAPI + SQLAlchemy + SQLite (Postgres-ready), trimesh for
  geometry parsing, Three.js scene payloads for visualization.
- **Orchestrator**: explicit state machine
  (`intake → clarification → plan_proposed → executing → review → final_approved → finalized`).
- **LLM role split**:
  - **DeepSeek-tier** (free OpenRouter model, default `meta-llama/llama-3.3-70b-instruct:free`,
    fallback chain) drives the **conversational intake** agent.
  - **Gemini** (default `gemini-3-pro` with `gemini-2.5-pro` / `gemini-2.0-flash`
    fallback) does the **reasoning / self-check** pass on the analysis snapshot.
- **Specialist agents**: Intake (LLM), Material lookup (DB-grounded), Transit
  envelope (deterministic mapping), Engineering Calculation (deterministic, every
  output carries formula + units + inputs), Surrogate Zone Risk (heuristic,
  always labeled approximate), Guardrail (blocks unsupported claims, missing
  units, NaNs), Reasoning self-check (LLM second pass), Report drafter
  (template-driven markdown).
- **Frontend**: vanilla ES modules + Three.js. Three-pane UI: chat + approval
  gate, 3D viewer with risk-zone tinting, results / report panel.
- **Audit log**: every important orchestrator action and tool invocation
  persisted to `audit_events`.

## Quickstart

```bash
# 1. Python 3.10+ required (3.7 won't work — Pydantic v2 needs 3.8+)
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure keys in .env
#    GEMINI_API_KEY        already populated
#    OPENROUTER_API_KEY    already populated (free OpenRouter key)
#    Models can be overridden via env (see .env comments)

# 3. Run
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8765

# 4. Open http://127.0.0.1:8765 in a browser
```

If either LLM key is missing the system still runs end-to-end via deterministic
stubs (intake falls back to a keyword-based classifier; reasoning is skipped
with a labeled warning). The deterministic engineering layer never depends on
an LLM.

## Try the smoke flow

1. Open the UI. A new case is created automatically.
2. Send: *"Analyze a 500ml HDPE oil bottle (0.6mm wall) for transit
   survivability. Shipping by truck and air. I have a STEP file. Use ISTA 3A."*
3. The intake agent populates the structured fields and proposes a plan.
4. Click **📎** and upload `Test Stp/oil bottle.stp`. Geometry parses; viewer loads.
5. Review the proposed plan, click **Approve and run**.
6. The orchestrator runs material lookup → transit envelope → deterministic
   calcs → surrogate risk map → Gemini self-check → draft report.
7. The 3D viewer tints the bottle by zone risk.
8. Click **Finalize** in the report card to close the case.

## Project layout

```
backend/
  main.py              FastAPI entry, CORS, static frontend mount
  config.py            settings.Settings — env-driven config
  db.py / models.py    SQLAlchemy 2.x ORM (Case, Message, GeometryAsset,
                       MaterialRecord, TransitProfile, AnalysisResult, AuditEvent)
  schemas.py           Pydantic schemas for API I/O AND every agent contract
  audit.py             log_event() helper
  seed.py              loads data/materials.json into the DB on startup
  llm/
    gemini_client.py     Reasoning role (Gemini, with model fallback chain)
    deepseek_client.py   Intake role (OpenAI-compatible, OpenRouter default)
  orchestrator/
    state_machine.py     legal stage transitions
    orchestrator.py      handle_user_message, get_proposed_plan, execute_approved_plan
  agents/
    intake.py            DeepSeek-tier conversational extraction (+ heuristic fallback)
    material.py          DB-grounded material lookup; never invents numbers
    transit.py           deterministic mode-mix → loading envelope
    calculation.py       drop energy / impact velocity / compression SF / thin-wall buckling
    surrogate.py         heuristic zone risk map (always labeled approximate)
    guardrail.py         blocks unsupported claims, missing units, NaNs
    reasoning.py         Gemini self-check + engineering narrative
    report.py            audit-friendly markdown report drafter
  services/
    geometry_service.py    trimesh for STL/OBJ/PLY/GLB; STEP gets header
                           parse + labeled bottle-primitive proxy
    visualization_service.py   builds the Three.js scene payload
  routes/
    cases.py             /api/cases, messages, upload, plan, approve, mesh,
                         visualization, report, finalize, audit
data/materials.json    seeded material DB (PET, HDPE, LDPE, PP, PVC, PS, Glass, Al, Corrugated, Kraft)
frontend/
  index.html  styles.css  app.js  viewer.js
storage/               per-case uploaded files and generated meshes
logs/                  uvicorn / runtime logs
```

## Non-negotiable constraints (section 22)

These are enforced in code, not just policy:

1. **Low-temperature LLM calls.** Intake `temperature=0.1`, Reasoning `0.05`.
2. **No ungrounded final answers.** Material lookups that miss the DB return
   `confidence: insufficient_data` and surface caveats.
3. **No final output without human approval.** `execute_approved_plan` raises
   `PermissionError` unless `approval_state == "plan_approved"`. Finalize is a
   separate explicit user action.
4. **No exact FEA claim.** STEP files always parse to a labeled
   `step (proxy)` summary; the surrogate risk map carries an explicit
   approximation warning.
5. **No compliance claim without source.** Guardrail rejects phrases like
   "ISTA compliant" / "FEA-validated" / "certified".
6. **No calculation without unit validation.** Every CalculationOutput carries
   `formula`, `inputs`, and `units`; Guardrail rejects any that don't.

## STEP file handling

trimesh does not natively mesh STEP B-rep without a heavy CAD library
(pythonocc / cadquery). The MVP:

1. Validates the file as STEP (ISO-10303 header check).
2. Parses the header for `FILE_NAME`, `FILE_DESCRIPTION`, `FILE_SCHEMA`.
3. Substitutes a procedural bottle proxy mesh for visualization, with
   `is_proxy: true` and `confidence: approximate` plainly visible in the
   scene payload, the geometry summary, and the report.

This is honest with the user and matches the architecture's directive: "Do not
promise exact stress results from a heuristic model." For real STEP meshing,
add `cadquery` or `pythonocc-core` and extend `geometry_service.parse()`.

## Extending

- **New material**: append to `data/materials.json` and restart.
- **New deterministic calc**: add a method to `CalculationAgent`, then call it
  from `Orchestrator.execute_approved_plan`. Guardrail will require formula +
  inputs + units automatically.
- **Real FEA solver**: wire it as a tool in `services/`, gate it behind the
  same approval flow, and surface its output as an `AnalysisResult` with
  `method_type="fea"` and `confidence="verified"`.
- **MCP tool servers** (section 10): each agent already returns a strict
  Pydantic schema, so wrapping them as MCP tools is mechanical.
