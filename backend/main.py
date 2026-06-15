"""FastAPI entrypoint. Run with:

    uvicorn backend.main:app --reload
"""
from __future__ import annotations
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT
from .db import SessionLocal, init_db
from .routes.auth import router as auth_router
from .routes.cases import router as cases_router
from .routes.extras import router as extras_router
from .seed import seed_admin, seed_materials, topup_all_users


app = FastAPI(title="PackTwin.ai", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # dev only
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(cases_router, prefix="/api")
app.include_router(extras_router, prefix="/api")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    db = SessionLocal()
    try:
        n = seed_materials(db)
        if n:
            print(f"[startup] Seeded {n} materials.")
        if seed_admin(db):
            print("[startup] Seeded admin user.")
        # Universal floor: every non-admin user gets at least 20 tokens.
        # Idempotent — only credits users currently below the floor.
        topped = topup_all_users(db)
        if topped:
            print(f"[startup] Topped up {topped} users to the 20-token floor.")
    finally:
        db.close()


@app.get("/api/health")
def health():
    from .config import settings
    from .llm.gemini_client import get_gemini
    g = get_gemini()
    # The UI shows a friendly status; the technical detail is here for ops.
    intake_ok = g.available
    reasoning_ok = g.available
    if intake_ok and reasoning_ok:
        ui_label = "All systems nominal"
    elif intake_ok or reasoning_ok:
        ui_label = "Operating with reduced capability"
    else:
        ui_label = "Operating in offline / stub mode"
    return {
        "status": "ok",
        "ui_label": ui_label,
        "intake_llm":   {"provider": "gemini", "model": settings.GEMINI_INTAKE_MODEL,    "available": intake_ok},
        "reasoning_llm":{"provider": "gemini", "model": settings.GEMINI_REASONING_MODEL, "available": reasoning_ok},
    }


# Serve company assets (logos, etc.) under /assets
assets_dir = PROJECT_ROOT / "Assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

# Serve the frontend at "/" (mounted last so /api and /assets take priority)
frontend_dir = PROJECT_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
