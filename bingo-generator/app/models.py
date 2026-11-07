"""SQLAlchemy models for the bingo web app.

Data model
----------
Global content:
    * ``BingoQuestionList`` — the pool of bingo clues (``text``).
    * ``MultipleChoiceQuestionList`` — the pool of MCQs used for the C3 free
      space (``question``, four ``options``, ``answer_key`` letter).

Accounts:
    * ``User`` — one Google account, identified by email. ``role`` is ``admin``
      or ``user``; admin emails are also controlled by ``ADMIN_EMAILS`` at
      sign-in time (see ``app.auth``).

Per-student sheets:
    * ``BingoSheet`` — one printable/playable board. ``sheet_id`` is the public,
      human-searchable identifier used in URLs; ``assigned_email`` links the
      sheet to the user allowed to play it (NULL means only admins can see it).
      ``question_list`` holds exactly 24 clue ids in reading order for the cells
      A1..E5, skipping the free space C3 (position 13), and
      ``multiple_choice_question_id`` links the MCQ for that C3 square.
      ``marked_cells`` persists the player's progress.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship, validates

from app.database import Base

# Board cells in reading order, skipping the free space C3. Index 0 -> "A1".
GRID_POSITIONS: tuple[str, ...] = tuple(
    f"{row}{column}"
    for row in "ABCDE"
    for column in range(1, 6)
    if not (row == "C" and column == 3)
)
GRID_CELL_COUNT = len(GRID_POSITIONS)  # 24
MCQ_POSITION = "C3"

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


def normalize_email(value: Any) -> str:
    """Canonical form for comparing user / assignment emails."""
    return str(value or "").strip().casefold()


def new_sheet_id() -> str:
    """A fresh public sheet identifier, e.g. ``CB-9F3A1B``."""
    return f"CB-{secrets.token_hex(3).upper()}"


def normalize_marked_cells(raw: Any) -> List[str]:
    """Return persisted tile marks as a clean list of board positions."""
    allowed = set(GRID_POSITIONS) | {MCQ_POSITION}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    marked = []
    for item in raw:
        position = str(item).strip().upper()
        if position in allowed and position not in marked:
            marked.append(position)
    return marked


class User(Base):
    """A person who can sign in with Google.

    Accounts are created on first successful sign-in. Sheets are linked to an
    account through ``BingoSheet.assigned_email`` rather than a hard foreign key,
    so an administrator can assign a sheet before the student has ever logged in.
    """

    __tablename__ = "app_user"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    role = Column(String, nullable=False, default=ROLE_USER)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:  # noqa: ANN001
        return normalize_email(value)

    @validates("role")
    def _valid_role(self, key: str, value: str) -> str:  # noqa: ANN001
        role = str(value or ROLE_USER).strip().lower()
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return role

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} email={self.email!r} role={self.role!r}>"


def normalize_question_ids(raw: Any) -> List[int]:
    """Return a sheet's stored clue ids as a list of integers.

    Sheets written by older versions of the app sometimes stored the id list as a
    JSON string inside the JSON column (double-encoded). Both a real list and
    such a string decode to the same ids here.
    """
    value = raw
    for _ in range(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return []
        else:
            break
    if not isinstance(value, (list, tuple)):
        return []

    question_ids: List[int] = []
    for item in value:
        try:
            question_ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return question_ids


class BingoQuestionList(Base):
    """Global pool of bingo clues."""

    __tablename__ = "bingo_question_list"

    id = Column(Integer, primary_key=True)
    text = Column(String, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BingoQuestionList id={self.id} text={self.text!r}>"


class MultipleChoiceQuestionList(Base):
    """Global pool of MCQs.

    ``options`` is a JSON array of strings; ``answer_key`` is the correct
    option letter (A-D), which maps to index 0-3.
    """

    __tablename__ = "multiple_choice_question_list"

    id = Column(Integer, primary_key=True)
    question = Column(String, nullable=False)
    options = Column(JSON, nullable=False)
    answer_key = Column(String, nullable=False)

    sheets = relationship("BingoSheet", back_populates="mc_question")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MultipleChoiceQuestionList id={self.id} answer_key={self.answer_key!r}>"


class BingoSheet(Base):
    """A single bingo board handed to one student."""

    __tablename__ = "bingo_sheet"

    id = Column(Integer, primary_key=True)
    # Public identifier used in URLs and in the admin search box.
    sheet_id = Column(String, unique=True, index=True, nullable=False, default=new_sheet_id)
    student_name = Column(String, nullable=False)
    parent_name = Column(String, nullable=False)
    parent_email = Column(String, nullable=False)
    # Stored as text so country codes and leading "+" / zeros survive.
    parent_whatsapp = Column(String, nullable=False)

    # Gmail account allowed to play this sheet; NULL means admin-only.
    assigned_email = Column(String, index=True, nullable=True)

    # Board positions the player has marked, e.g. ["A1", "C3"]; progress is saved.
    marked_cells = Column(JSON, nullable=False, default=list)

    # Exactly 24 integer ids into bingo_question_list, ordered A1..E5 skipping C3.
    question_list = Column(JSON, nullable=False)

    # Foreign key to the MCQ that owns the centre square.
    multiple_choice_question_id = Column(
        Integer,
        ForeignKey("multiple_choice_question_list.id"),
        nullable=False,
    )
    mc_question = relationship("MultipleChoiceQuestionList", back_populates="sheets")

    @validates("assigned_email")
    def _normalize_assignment(self, key: str, value: Optional[str]) -> Optional[str]:  # noqa: ANN001
        cleaned = normalize_email(value)
        return cleaned or None

    @property
    def marked(self) -> List[str]:
        """Saved board progress, tolerant of legacy/empty values."""
        return normalize_marked_cells(self.marked_cells)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BingoSheet id={self.id} sheet_id={self.sheet_id!r} student_name={self.student_name!r}>"
