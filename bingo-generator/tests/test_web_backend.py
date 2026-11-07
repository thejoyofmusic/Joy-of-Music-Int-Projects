"""Tests for the FastAPI backend.

The test database, Google credentials and session secret are configured in
``tests/conftest.py`` before ``app.database`` is imported. Requests in this
module are made as an administrator, since sheet management is admin-only.
"""

from __future__ import annotations

import json
from io import BytesIO

import pytest

from docx import Document
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import inspect

from app.database import SessionLocal, engine
from app.main import app
from app.migrations import run_migrations
from app.models import (
    GRID_CELL_COUNT,
    BingoQuestionList,
    BingoSheet,
    MultipleChoiceQuestionList,
)
from app.schemas import BingoSheetCreate
from app.seed import seed_from_docx
from tests.auth_helpers import login_as

ADMIN_EMAIL = "admin@example.com"

SHEET_FORM = {
    "student_name": "Test Student",
    "parent_name": "Test Parent",
    "parent_email": "parent@example.com",
    "parent_whatsapp": "+1 (555) 010-9999",
}


@pytest.fixture(scope="module")
def client():
    # Entering the context runs startup: migrations + seeding.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def admin_session(client):
    """Run every test in this module as the configured administrator."""
    client.cookies.clear()
    login_as(client, ADMIN_EMAIL)
    yield
    client.cookies.clear()


@pytest.fixture(scope="module")
def created_sheet(client):
    login_as(client, ADMIN_EMAIL)
    response = client.post("/generate-sheet", data=SHEET_FORM, follow_redirects=False)
    assert response.status_code == 303, response.text
    with SessionLocal() as db:
        return db.query(BingoSheet).order_by(BingoSheet.id.desc()).first()


def test_migrations_create_expected_schema(client):
    tables = set(inspect(engine).get_table_names())
    assert {
        "bingo_question_list",
        "multiple_choice_question_list",
        "bingo_sheet",
        "app_user",
        "schema_migrations",
    } <= tables
    # Already applied during startup, so another run is a no-op.
    assert run_migrations(engine) == []


def test_seed_is_idempotent(client):
    with SessionLocal() as db:
        assert db.query(BingoQuestionList).count() == 24
        assert db.query(MultipleChoiceQuestionList).count() == 20
        assert seed_from_docx(db) == {
            "bingo_questions": 0,
            "multiple_choice_questions": 0,
        }


def test_bingo_sheet_schema_requires_24_unique_question_ids():
    payload = {
        **SHEET_FORM,
        "multiple_choice_question_id": 1,
    }
    valid = BingoSheetCreate(**payload, question_list=list(range(1, GRID_CELL_COUNT + 1)))
    assert len(valid.question_list) == GRID_CELL_COUNT

    with pytest.raises(ValidationError):
        BingoSheetCreate(**payload, question_list=list(range(1, 23)))

    with pytest.raises(ValidationError):
        BingoSheetCreate(
            **payload,
            question_list=[1] * GRID_CELL_COUNT,
        )

    with pytest.raises(ValidationError):
        BingoSheetCreate(**payload, question_list=list(range(1, 24)) + [1])


def test_bingo_sheet_schema_validates_contact_fields():
    with pytest.raises(ValidationError):
        BingoSheetCreate(
            **{**SHEET_FORM, "parent_email": "not-an-email"},
            question_list=list(range(1, 25)),
            multiple_choice_question_id=1,
        )
    with pytest.raises(ValidationError):
        BingoSheetCreate(
            **{**SHEET_FORM, "parent_whatsapp": "abc"},
            question_list=list(range(1, 25)),
            multiple_choice_question_id=1,
        )


def test_generate_sheet_stores_24_unique_foreign_keys(created_sheet):
    assert created_sheet is not None
    question_ids = list(created_sheet.question_list)
    assert len(question_ids) == GRID_CELL_COUNT
    assert len(set(question_ids)) == GRID_CELL_COUNT

    with SessionLocal() as db:
        found = {
            row[0]
            for row in db.query(BingoQuestionList.id)
            .filter(BingoQuestionList.id.in_(question_ids))
            .all()
        }
        assert set(question_ids) == found
        assert db.get(MultipleChoiceQuestionList, created_sheet.multiple_choice_question_id)


def test_sheet_page_renders_every_cell(client, created_sheet):
    response = client.get(f"/sheet/{created_sheet.id}")
    assert response.status_code == 200
    page = response.text
    assert page.count('class="cell clue"') == 24
    assert page.count('class="coord-badge"') == 24
    assert 'class="cell free-space"' in page


def test_generate_sheet_rejects_unknown_question_ids(client):
    response = client.post(
        "/generate-sheet",
        data={**SHEET_FORM, "question_list": ",".join(["9999"] * GRID_CELL_COUNT)},
    )
    assert response.status_code == 400
    assert "Unknown bingo question ids" in response.json()["error"]


def test_generate_sheet_rejects_wrong_question_count(client):
    response = client.post(
        "/generate-sheet",
        data={**SHEET_FORM, "question_list": ",".join(str(i) for i in range(1, 24))},
    )
    assert response.status_code == 422
    assert "question_list" in response.json()["error"]


def test_generate_sheet_get_redirects_to_admin(client):
    response = client.get("/generate-sheet", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


def test_download_returns_printable_docx(client, created_sheet):
    response = client.get(f"/sheet/{created_sheet.id}/download")
    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.headers["content-disposition"] == (
        f'attachment; filename="Carnatic_Bingo_Sheet_{created_sheet.sheet_id}.docx"'
    )

    document = Document(BytesIO(response.content))

    # Sheet number is stamped into the title.
    assert any(
        f"Bingo Sheet #{created_sheet.id}" in paragraph.text
        for paragraph in document.paragraphs
    )

    with SessionLocal() as db:
        clue_texts = [
            db.get(BingoQuestionList, question_id).text
            for question_id in created_sheet.question_list
        ]
        mcq = db.get(MultipleChoiceQuestionList, created_sheet.multiple_choice_question_id)

    # The "I Heard" table carries every clue in board order.
    heard_table = next(
        table for table in document.tables if len(table.rows) == 12 and len(table.columns) == 2
    )
    heard_text = "\n".join(cell.text for row in heard_table.rows for cell in row.cells)
    for clue_text in clue_texts:
        assert clue_text in heard_text

    # The MCQ square holds the question and its options.
    mcq_cell = next(
        table for table in document.tables if len(table.rows) == 1 and len(table.columns) == 1
    ).rows[0].cells[0]
    assert mcq.question in mcq_cell.text
    for option in mcq.options:
        assert option in mcq_cell.text


def test_legacy_double_encoded_question_list_renders(client):
    """Older rows stored the id list as a JSON string inside the JSON column."""
    with SessionLocal() as db:
        db.add(
            BingoSheet(
                student_name="Legacy Student",
                parent_name="Legacy Parent",
                parent_email="legacy@example.com",
                parent_whatsapp="+15550109999",
                question_list=json.dumps(list(range(1, GRID_CELL_COUNT + 1))),
                multiple_choice_question_id=1,
            )
        )
        db.commit()
        legacy_sheet_id = db.query(BingoSheet).order_by(BingoSheet.id.desc()).first().id

    page = client.get(f"/sheet/{legacy_sheet_id}")
    assert page.status_code == 200
    assert page.text.count('class="cell clue"') == 24
    assert client.get(f"/sheet/{legacy_sheet_id}/download").status_code == 200


def test_download_missing_sheet_returns_404(client):
    response = client.get("/sheet/9999/download")
    assert response.status_code == 404


def test_api_questions_return_json_arrays(client):
    clues = client.get("/api/bingo-questions")
    assert clues.status_code == 200
    assert clues.headers["content-type"].startswith("application/json")
    assert len(clues.json()) == 24
    assert set(clues.json()[0]) == {"id", "text"}

    mcqs = client.get("/api/mc-questions")
    assert mcqs.status_code == 200
    assert len(mcqs.json()) == 20
    assert set(mcqs.json()[0]) == {"id", "question", "options", "answer_key"}
    assert isinstance(mcqs.json()[0]["options"], list)


def test_api_sheets_expose_resolved_sheet(client, created_sheet):
    response = client.get(f"/api/sheets/{created_sheet.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created_sheet.id
    assert body["question_list"] == list(created_sheet.question_list)
    assert len(body["question_list"]) == GRID_CELL_COUNT
    assert len(body["clues"]) == GRID_CELL_COUNT
    assert body["free_space_mcq"]["id"] == created_sheet.multiple_choice_question_id

    listing = client.get("/api/sheets")
    assert listing.status_code == 200
    assert any(sheet["id"] == created_sheet.id for sheet in listing.json())

    assert client.get("/api/sheets/9999").status_code == 404


def test_api_export_downloads_everything(client, created_sheet):
    response = client.get("/api/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "carnatic_bingo_data.json" in response.headers["content-disposition"]

    body = response.json()
    assert len(body["bingo_question_list"]) == 24
    assert len(body["multiple_choice_question_list"]) == 20
    assert any(sheet["id"] == created_sheet.id for sheet in body["bingo_sheet"])


def test_verify_answer_accepts_json_and_letters(client):
    with SessionLocal() as db:
        mcq = db.get(MultipleChoiceQuestionList, 1)
        correct = "ABCD".index(mcq.answer_key)
        options = list(mcq.options)

    good = client.post("/verify-answer", json={"mcqId": 1, "selectedAnswer": correct})
    assert good.status_code == 200
    assert good.json()["correct"] is True
    assert good.json()["correctIndex"] == correct

    by_letter = client.post(
        "/verify-answer", json={"mcqId": 1, "selectedAnswer": mcq.answer_key}
    )
    assert by_letter.json()["correct"] is True

    by_text = client.post(
        "/verify-answer", json={"mcqId": 1, "selectedAnswer": options[correct]}
    )
    assert by_text.json()["correct"] is True

    wrong_index = (correct + 1) % len(options)
    wrong = client.post("/verify-answer", json={"mcqId": 1, "selectedAnswer": wrong_index})
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False
    assert wrong.json()["correctIndex"] == correct

    form = client.post("/verify-answer", data={"mcq_id": 1, "selected_answer": str(correct)})
    assert form.status_code == 200
    assert form.json()["correct"] is True


def test_verify_answer_rejects_bad_input(client):
    assert client.post("/verify-answer", json={"selectedAnswer": 0}).status_code == 400
    assert (
        client.post("/verify-answer", json={"mcqId": 9999, "selectedAnswer": 0}).status_code
        == 404
    )
    assert (
        client.post("/verify-answer", json={"mcqId": 1, "selectedAnswer": 9}).status_code
        == 400
    )
    assert client.post("/verify-answer", json={"mcqId": 1}).status_code == 400
