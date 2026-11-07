"""Database engine, session factory and connection settings for the bingo web app.

The SQLite file lives next to this package by default (``bingo-generator/bingo_sheets.db``)
so the app works regardless of the working directory. Override it with the
``BINGO_DATABASE_URL`` environment variable, e.g. ``sqlite:////tmp/test.db``.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "bingo_sheets.db"

DATABASE_URL = os.environ.get("BINGO_DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    future=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):  # noqa: ANN001
        """SQLite ignores foreign keys unless they are enabled per connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
Base = declarative_base()


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
