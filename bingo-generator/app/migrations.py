"""Ordered schema migrations for the bingo database.

Each migration is plain SQL, applied at most once and recorded in
``schema_migrations``. The runner is dependency-free and works on SQLite (the
app default) and other SQLAlchemy-compatible engines. It runs automatically on
app startup and from ``python -m app.setup_db``.

To add a schema change, append a new ``Migration`` with the next version number;
never edit an already-applied migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL,
    applied_at VARCHAR NOT NULL
)
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS bingo_question_list (
                id INTEGER PRIMARY KEY,
                text VARCHAR NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS multiple_choice_question_list (
                id INTEGER PRIMARY KEY,
                question VARCHAR NOT NULL,
                options JSON NOT NULL,
                answer_key VARCHAR NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bingo_sheet (
                id INTEGER PRIMARY KEY,
                student_name VARCHAR NOT NULL,
                parent_name VARCHAR NOT NULL,
                parent_email VARCHAR NOT NULL,
                parent_whatsapp VARCHAR NOT NULL,
                question_list JSON NOT NULL,
                multiple_choice_question_id INTEGER NOT NULL
                    REFERENCES multiple_choice_question_list (id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_bingo_sheet_mcq
                ON bingo_sheet (multiple_choice_question_id)
            """,
        ),
    ),
    Migration(
        version=2,
        name="users_and_sheet_ownership",
        statements=(
            # Google accounts. Email is the identity; role is admin or user.
            """
            CREATE TABLE IF NOT EXISTS app_user (
                id INTEGER PRIMARY KEY,
                email VARCHAR NOT NULL,
                role VARCHAR NOT NULL DEFAULT 'user',
                created_at VARCHAR NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ix_app_user_email
                ON app_user (email)
            """,
            # Public sheet identifier, owner email and saved board progress.
            # Added as nullable and backfilled because ALTER TABLE cannot add
            # NOT NULL columns to a populated SQLite table.
            "ALTER TABLE bingo_sheet ADD COLUMN sheet_id VARCHAR",
            "ALTER TABLE bingo_sheet ADD COLUMN assigned_email VARCHAR",
            "ALTER TABLE bingo_sheet ADD COLUMN marked_cells JSON",
            """
            UPDATE bingo_sheet
               SET sheet_id = 'CB-' || substr('000000' || CAST(id AS TEXT), -6)
             WHERE sheet_id IS NULL OR sheet_id = ''
            """,
            "UPDATE bingo_sheet SET marked_cells = '[]' WHERE marked_cells IS NULL",
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ix_bingo_sheet_sheet_id
                ON bingo_sheet (sheet_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS ix_bingo_sheet_assigned_email
                ON bingo_sheet (assigned_email)
            """,
        ),
    ),
)


def applied_versions(engine: Engine) -> set[int]:
    """Return the versions already recorded as applied."""
    with engine.begin() as connection:
        connection.execute(text(SCHEMA_MIGRATIONS_DDL))
        rows = connection.execute(text("SELECT version FROM schema_migrations")).scalars().all()
    return {int(version) for version in rows}


def run_migrations(engine: Engine) -> list[int]:
    """Apply every pending migration in order and return the versions applied."""
    applied = applied_versions(engine)
    newly_applied: list[int] = []
    for migration in sorted(MIGRATIONS, key=lambda item: item.version):
        if migration.version in applied:
            continue
        with engine.begin() as connection:
            for statement in migration.statements:
                connection.execute(text(statement))
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (:version, :name, :applied_at)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        newly_applied.append(migration.version)
    return newly_applied
