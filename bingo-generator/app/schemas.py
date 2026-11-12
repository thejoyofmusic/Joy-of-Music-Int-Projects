"""Pydantic schemas for the bingo web app's request and response bodies."""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import GRID_CELL_COUNT, ROLE_USER, ROLES

OPTION_LETTERS = "ABCD"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Public sheet ids look like CB-9F3A1B or CB-000001; admins may choose their own.
SHEET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")


def normalize_sheet_id(value: str) -> str:
    """Canonical form for a sheet identifier (upper-case, trimmed)."""
    return str(value or "").strip().upper()


def validate_assigned_email(value: str) -> str:
    """Validate and canonicalise a sheet owner's email address."""
    cleaned = str(value or "").strip().casefold()
    if not EMAIL_RE.match(cleaned):
        raise ValueError("must be a valid email address")
    return cleaned
# Keep phone numbers permissive: digits, spaces, dashes, parentheses, leading "+".
WHATSAPP_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{5,19}$")


class BingoQuestionCreate(BaseModel):
    """Payload for adding a clue to the global pool."""

    text: str = Field(min_length=1, max_length=500)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("text must not be blank")
        return cleaned


class BingoQuestionListSchema(BaseModel):
    """A stored clue."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    text: str


class MultipleChoiceQuestionCreate(BaseModel):
    """Payload for adding an MCQ to the global pool."""

    question: str = Field(min_length=1, max_length=1000)
    options: List[str] = Field(min_length=2, max_length=len(OPTION_LETTERS))
    answer_key: str

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question must not be blank")
        return cleaned

    @field_validator("options")
    @classmethod
    def _options_not_blank(cls, value: List[str]) -> List[str]:
        cleaned = [option.strip() for option in value]
        if any(not option for option in cleaned):
            raise ValueError("options must not contain blank entries")
        if len({option.casefold() for option in cleaned}) != len(cleaned):
            raise ValueError("options must be unique")
        return cleaned

    @field_validator("answer_key")
    @classmethod
    def _answer_key_is_letter(cls, value: str) -> str:
        cleaned = str(value).strip().upper()
        if cleaned not in OPTION_LETTERS:
            raise ValueError(f"answer_key must be one of {', '.join(OPTION_LETTERS)}")
        return cleaned

    @model_validator(mode="after")
    def _answer_key_matches_an_option(self) -> "MultipleChoiceQuestionCreate":
        if OPTION_LETTERS.index(self.answer_key) >= len(self.options):
            raise ValueError("answer_key points past the last option")
        return self


class MultipleChoiceQuestionListSchema(BaseModel):
    """A stored MCQ."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    options: List[str]
    answer_key: str


class BingoSheetCreate(BaseModel):
    """Payload for creating a bingo sheet.

    ``question_list`` must hold exactly 24 unique clue ids, mapped in order to
    A1, A2, A3, A4, A5, B1, … E5 while skipping the free space C3.
    """

    student_name: str = Field(min_length=1, max_length=120)
    parent_name: str = Field(min_length=1, max_length=120)
    parent_email: str
    parent_whatsapp: str
    question_list: List[int] = Field(min_length=GRID_CELL_COUNT, max_length=GRID_CELL_COUNT)
    multiple_choice_question_id: int = Field(gt=0)

    @field_validator("student_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("parent_name")
    @classmethod
    def _valid_parent_name(cls, value: str) -> str:
        return cls._name_not_blank(value)

    @field_validator("parent_email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        cleaned = value.strip()
        if not EMAIL_RE.match(cleaned):
            raise ValueError("must be a valid email address")
        return cleaned

    @field_validator("parent_whatsapp")
    @classmethod
    def _valid_whatsapp(cls, value: str) -> str:
        cleaned = value.strip()
        if not WHATSAPP_RE.match(cleaned):
            raise ValueError("must be a valid phone number (digits, optional leading '+')")
        if len(re.sub(r"\D", "", cleaned)) < 7:
            raise ValueError("must contain at least 7 digits")
        return cleaned

    @field_validator("question_list")
    @classmethod
    def _valid_question_list(cls, value: List[int]) -> List[int]:
        if len(value) != GRID_CELL_COUNT:
            raise ValueError(f"question_list must contain exactly {GRID_CELL_COUNT} question ids")
        if any(question_id <= 0 for question_id in value):
            raise ValueError("question_list must only contain positive question ids")
        if len(set(value)) != len(value):
            raise ValueError("question_list must not repeat a question id")
        return value


class AdminSheetCreate(BingoSheetCreate):
    """Payload for an administrator-created sheet.

    Adds the public identifier (generated when omitted) and the Gmail address the
    sheet is assigned to (unassigned sheets stay admin-only).
    """

    parent_name: str = Field(default="", max_length=120)
    parent_email: str = ""
    parent_whatsapp: str = ""

    @field_validator("parent_name")
    @classmethod
    def _valid_parent_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("parent_email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return super()._valid_email(value) if value.strip() else ""

    @field_validator("parent_whatsapp")
    @classmethod
    def _valid_whatsapp(cls, value: str) -> str:
        return super()._valid_whatsapp(value) if value.strip() else ""

    sheet_id: Optional[str] = None
    assigned_email: Optional[str] = None

    @field_validator("sheet_id")
    @classmethod
    def _valid_sheet_id(cls, value: Optional[str]) -> Optional[str]:
        cleaned = normalize_sheet_id(value or "")
        if not cleaned:
            return None
        if not SHEET_ID_RE.match(cleaned):
            raise ValueError(
                "must be 2-32 characters of letters, digits, '-' or '_'"
            )
        return cleaned

    @field_validator("assigned_email")
    @classmethod
    def _valid_assigned_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return validate_assigned_email(value)


class StudentDetails(BaseModel):
    """Student and parent information saved when an admin registers a student."""

    model_config = ConfigDict(str_strip_whitespace=True)

    student_name: str = Field(default="", max_length=120)
    parent_name: str = Field(default="", max_length=120)
    parent_email: str = ""
    parent_whatsapp: str = ""

    @field_validator("parent_email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        return AdminSheetCreate._valid_email(value)

    @field_validator("parent_whatsapp")
    @classmethod
    def _valid_whatsapp(cls, value: str) -> str:
        return AdminSheetCreate._valid_whatsapp(value)


class SheetAssignment(BaseModel):
    """Payload for assigning an existing sheet to a user email."""

    assigned_email: Optional[str] = None

    @field_validator("assigned_email")
    @classmethod
    def _valid_assigned_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        return validate_assigned_email(value)


class UserSchema(BaseModel):
    """A signed-in account."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str = ROLE_USER

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return value


class BingoSheetSchema(BaseModel):
    """A stored bingo sheet."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sheet_id: str
    student_name: str
    parent_name: str
    parent_email: str
    parent_whatsapp: str
    assigned_email: Optional[str] = None
    marked_cells: List[str] = Field(default_factory=list)
    question_list: List[int]
    multiple_choice_question_id: int
