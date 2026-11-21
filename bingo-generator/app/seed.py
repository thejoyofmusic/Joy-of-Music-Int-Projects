"""Seed the global question pools from the authoring .docx files.

The clue list and MCQ list live in Word documents (``input_bingo_questions.docx``
and ``input_multiple_choice_Q&A.docx``). ``carnatic_bingo_generator`` already
parses those files with python-docx, so the web backend reuses the same parser to
keep the CLI and the app in sync.

Clues are synchronized by text while retaining records used by older sheets.
MCQs are inserted only when their table is empty.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.database import PROJECT_ROOT
from app.models import BingoQuestionList, MultipleChoiceQuestionList
from carnatic_bingo_generator import (
    CONCERT_LIST_FILENAME,
    MCQ_LIST_FILENAME,
    parse_concert_clues,
    parse_mcqs,
)

logger = logging.getLogger(__name__)


def seed_bingo_questions(db: Session, bingo_root: Path | None = None) -> int:
    """Sync the current clue source without changing IDs used by existing sheets."""
    source = (bingo_root or PROJECT_ROOT) / CONCERT_LIST_FILENAME
    if not source.is_file():
        logger.warning("Clue source not found, skipping clue seed: %s", source)
        return 0

    clues = parse_concert_clues(str(source))
    existing = db.query(BingoQuestionList).order_by(BingoQuestionList.id).all()
    by_text = {}
    for question in existing:
        key = " ".join(question.text.casefold().split())
        by_text.setdefault(key, question)
        question.active = False
    added = 0
    for clue in clues:
        key = " ".join(clue.casefold().split())
        question = by_text.get(key)
        if question is None:
            question = BingoQuestionList(text=clue, active=True)
            db.add(question)
            by_text[key] = question
            added += 1
        question.active = True
    db.commit()
    return added


def seed_multiple_choice_questions(db: Session, bingo_root: Path | None = None) -> int:
    """Insert the MCQ list when the table is empty. Returns rows added."""
    if db.query(MultipleChoiceQuestionList).count() > 0:
        return 0

    source = (bingo_root or PROJECT_ROOT) / MCQ_LIST_FILENAME
    if not source.is_file():
        logger.warning("MCQ source not found, skipping MCQ seed: %s", source)
        return 0

    mcqs = parse_mcqs(str(source))
    db.add_all(
        MultipleChoiceQuestionList(
            question=mcq["question"],
            options=list(mcq["options"]),
            answer_key=mcq["answer"],
        )
        for mcq in mcqs
    )
    db.commit()
    return len(mcqs)


def seed_from_docx(db: Session, bingo_root: Path | None = None) -> dict[str, int]:
    """Seed both pools, returning how many rows each one gained."""
    return {
        "bingo_questions": seed_bingo_questions(db, bingo_root),
        "multiple_choice_questions": seed_multiple_choice_questions(db, bingo_root),
    }
