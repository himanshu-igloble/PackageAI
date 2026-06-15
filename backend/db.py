"""SQLAlchemy engine + session factory. SQLite for the MVP; swap to Postgres later."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


engine = create_engine(
    settings.DB_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if settings.DB_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a session and ensures it closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if missing. Called on app startup.

    For SQLite we also run a tiny in-place migration: add any columns the
    current ORM declares that are missing from a pre-existing table. This lets
    us extend `materials` (new PCR / carbon columns) without forcing users to
    drop and recreate cpg_ista.db.
    """
    from sqlalchemy import inspect, text
    from . import models  # noqa: F401  (registers models on Base)

    Base.metadata.create_all(engine)

    if not settings.DB_URL.startswith("sqlite"):
        return

    insp = inspect(engine)
    # Map SQLAlchemy column types to SQLite affinities for the ALTER TABLE.
    type_map = {
        "BOOLEAN":  "BOOLEAN",
        "INTEGER":  "INTEGER",
        "FLOAT":    "REAL",
        "VARCHAR":  "TEXT",
        "TEXT":     "TEXT",
        "DATETIME": "DATETIME",
        "JSON":     "JSON",
    }
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                sql_type = type_map.get(col.type.__class__.__name__.upper(), "TEXT")
                default = col.default.arg if col.default is not None and hasattr(col.default, "arg") else None
                default_sql = ""
                if isinstance(default, bool):
                    default_sql = f" DEFAULT {1 if default else 0}"
                elif isinstance(default, (int, float)):
                    default_sql = f" DEFAULT {default}"
                elif isinstance(default, str):
                    default_sql = f" DEFAULT '{default}'"
                try:
                    conn.execute(text(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {sql_type}{default_sql}'
                    ))
                except Exception:
                    # Best-effort: ignore if SQLite refuses (e.g. NOT NULL without default).
                    pass
