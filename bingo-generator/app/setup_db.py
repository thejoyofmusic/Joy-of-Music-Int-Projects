"""Create/upgrade the bingo database and load the reference content.

Usage:
    python -m app.setup_db

Runs every pending migration (see ``app.migrations``), then seeds the clue and
MCQ pools from the bundled .docx files if those tables are empty. Safe to run
repeatedly; it never drops or rewrites existing data.
"""

from __future__ import annotations

from app.database import DATABASE_URL, SessionLocal, engine
from app.migrations import run_migrations
from app.seed import seed_from_docx


def setup_database() -> dict[str, object]:
    """Apply migrations and seed reference data. Returns a summary dict."""
    applied = run_migrations(engine)
    with SessionLocal() as db:
        seeded = seed_from_docx(db)
    return {"database_url": DATABASE_URL, "migrations_applied": applied, **seeded}


def main() -> None:
    result = setup_database()
    print(f"Database        : {result['database_url']}")
    migrations = result["migrations_applied"]
    print(f"Migrations      : {migrations if migrations else 'already up to date'}")
    print(f"Clues added     : {result['bingo_questions']}")
    print(f"MCQs added      : {result['multiple_choice_questions']}")


if __name__ == "__main__":
    main()
