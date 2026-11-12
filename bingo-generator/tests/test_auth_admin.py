"""Tests for Google OAuth routing, admin management and user-scoped dashboards.

Google's endpoints are never called: ``app.auth.exchange_code_for_email`` is
patched, so the tests exercise the real callback route, state validation, account
creation, role assignment and redirect rules.
"""

from __future__ import annotations

import os
import uuid
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app import auth
from app.database import SessionLocal
from app.main import app
from app.models import GRID_CELL_COUNT, BingoSheet, MultipleChoiceQuestionList, User
from tests.auth_helpers import login_as, start_google_login

ADMIN_EMAIL = "admin@example.com"


def unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def clean_session(client):
    client.cookies.clear()
    yield
    client.cookies.clear()


def make_sheet(
    client,
    *,
    assigned_email: str | None = None,
    student: str = "Test Student",
    sheet_id: str | None = None,
) -> BingoSheet:
    """Create a sheet through the admin route and return it."""
    login_as(client, ADMIN_EMAIL)
    response = client.post(
        "/admin/sheets",
        data={
            "student_name": student,
            "parent_name": "Parent Name",
            "parent_email": "parent@example.com",
            "parent_whatsapp": "+1 (555) 010-9999",
            "assigned_email": assigned_email or "",
            "sheet_id": sheet_id or "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    created_id = response.headers["location"].rsplit("/", 1)[-1]
    with SessionLocal() as db:
        return db.query(BingoSheet).filter(BingoSheet.sheet_id == created_id).one()


def sign_in_via_callback(client, monkeypatch, email: str):
    """Run /auth/login then /auth/callback as if Google returned ``email``."""
    monkeypatch.setattr(auth, "exchange_code_for_email", lambda code: email)
    state = start_google_login(client)
    return client.get(
        f"/auth/callback?code=test-code&state={state}", follow_redirects=False
    )


# ---------------------------------------------------------------------------
# Sign-in entry points
# ---------------------------------------------------------------------------
def test_landing_page_offers_google_sign_in(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "/auth/login" in page.text
    assert "Continue with Google" in page.text


def test_login_redirects_to_google_with_state_and_httponly_cookie(client):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303

    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=test-client-id" in location
    assert "response_type=code" in location
    assert "state=" in location
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fcallback" in location

    cookie = response.headers["set-cookie"].lower()
    assert "bingo_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_callback_rejects_unknown_state(client):
    start_google_login(client)
    response = client.get(
        "/auth/callback?code=test-code&state=not-the-state", follow_redirects=False
    )
    assert response.status_code == 400
    assert "no longer valid" in response.text


# ---------------------------------------------------------------------------
# Local sign-in (development, no Google round trip)
# ---------------------------------------------------------------------------
def test_login_page_offers_local_sign_in(client):
    page = client.get("/")
    assert "/auth/local-login" in page.text
    assert "Continue without Google" in page.text


def test_first_local_sign_in_becomes_admin(client):
    """A fresh install with no administrator must still be usable."""
    with SessionLocal() as db:
        existing_admins = [u.email for u in db.query(User).filter(User.role == "admin")]
        db.query(User).filter(User.role == "admin").update({"role": "user"})
        db.commit()

    try:
        client.cookies.clear()
        response = client.post(
            "/auth/local-login",
            data={"email": unique_email("owner")},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/admin"
        assert "httponly" in response.headers["set-cookie"].lower()
    finally:
        with SessionLocal() as db:
            db.query(User).filter(User.role == "admin").update({"role": "user"})
            for email in existing_admins:
                db.query(User).filter(User.email == email).update({"role": "admin"})
            db.commit()
        client.cookies.clear()


def test_local_sign_in_denies_unassigned_non_admin(client):
    login_as(client, ADMIN_EMAIL)  # guarantee an administrator exists
    client.cookies.clear()

    response = client.post(
        "/auth/local-login",
        data={"email": unique_email("stranger")},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "not a valid user in the system" in response.text


def test_local_sign_in_rejects_invalid_email(client):
    response = client.post(
        "/auth/local-login", data={"email": "not-an-email"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "valid email address" in response.text


def test_local_sign_in_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("LOCAL_LOGIN_ENABLED", "false")
    response = client.post(
        "/auth/local-login",
        data={"email": unique_email("owner")},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "Local sign-in is not available" in response.text


def test_local_account_can_claim_admin_from_the_error_page(client):
    """Signed in, no sheets assigned: the error page offers a local promotion."""
    email = unique_email("operator")
    denied = client.post(
        "/auth/local-login", data={"email": email}, follow_redirects=False
    )
    assert denied.status_code == 403
    assert "Make this account an administrator" in denied.text

    promoted = client.post("/auth/promote", follow_redirects=False)
    assert promoted.status_code == 303
    assert promoted.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200

    with SessionLocal() as db:
        assert db.query(User).filter(User.email == email).one().is_admin


def test_promotion_is_unavailable_on_a_public_base_url(client, monkeypatch):
    email = unique_email("operator")
    client.post("/auth/local-login", data={"email": email}, follow_redirects=False)

    monkeypatch.setenv("APP_BASE_URL", "https://bingo.example.com")
    response = client.post("/auth/promote", follow_redirects=False)
    assert response.status_code == 403
    assert "not available here" in response.text

    with SessionLocal() as db:
        assert not db.query(User).filter(User.email == email).one().is_admin


def test_local_sign_in_is_off_for_public_base_url(client, monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://bingo.example.com")
    assert auth.local_login_enabled() is False

    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_LOGIN_ENABLED", raising=False)
    assert auth.local_login_enabled() is True


# ---------------------------------------------------------------------------
# In-app OAuth setup
# ---------------------------------------------------------------------------
def test_setup_page_shows_redirect_uri(client):
    page = client.get("/setup")
    assert page.status_code == 200
    assert "http://testserver/auth/callback" in page.text
    assert "apps.googleusercontent.com" in page.text


def test_setup_rejects_a_non_google_client_id(client, monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("GOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n", encoding="utf-8")
    monkeypatch.setenv("BINGO_ENV_FILE", str(env_path))

    response = client.post(
        "/setup",
        data={"google_client_id": "not-a-client-id", "google_client_secret": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "look like a Google OAuth Client ID" in response.text
    assert env_path.read_text(encoding="utf-8") == (
        "GOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n"
    )


def test_setup_saves_credentials_and_enables_sign_in(client, monkeypatch, tmp_path):
    # Register the current values so teardown restores them even though
    # save_settings writes straight into os.environ.
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_EMAILS"):
        monkeypatch.setenv(key, os.environ.get(key, ""))

    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep me\nGOOGLE_CLIENT_ID=\nGOOGLE_CLIENT_SECRET=\n"
        "GOOGLE_REDIRECT_URI=http://testserver/auth/callback\n"
        "APP_BASE_URL=http://127.0.0.1:8000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BINGO_ENV_FILE", str(env_path))

    response = client.post(
        "/setup",
        data={
            "google_client_id": "1234-abcd.apps.googleusercontent.com",
            "google_client_secret": "GOCSPX-test-secret",
            "admin_emails": "owner@example.com",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/setup?saved=1"

    saved = env_path.read_text(encoding="utf-8")
    assert "# keep me" in saved
    assert "GOOGLE_CLIENT_ID=1234-abcd.apps.googleusercontent.com" in saved
    assert "GOOGLE_CLIENT_SECRET=GOCSPX-test-secret" in saved
    assert "ADMIN_EMAILS=owner@example.com" in saved
    assert "GOOGLE_REDIRECT_URI=http://testserver/auth/callback" in saved

    # Applied to the running process, so no restart is needed.
    assert auth.google_client_id() == "1234-abcd.apps.googleusercontent.com"
    assert auth.oauth_configured() is True
    assert auth.role_for_email("owner@example.com") == "admin"


def test_setup_is_unavailable_on_a_public_base_url(client, monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://bingo.example.com")
    assert client.get("/setup").status_code == 403
    assert client.post("/setup", data={}).status_code == 403


# ---------------------------------------------------------------------------
# Post-login routing
# ---------------------------------------------------------------------------
def test_admin_sign_in_lands_on_admin_console(client, monkeypatch):
    response = sign_in_via_callback(client, monkeypatch, ADMIN_EMAIL)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"

    console = client.get("/admin")
    assert console.status_code == 200
    assert "Admin console" in console.text

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == ADMIN_EMAIL).one()
        assert user.role == "admin"


def test_valid_user_sign_in_lands_on_dashboard(client, monkeypatch):
    student = unique_email("student")
    sheet = make_sheet(client, assigned_email=student, student="Dashboard Student")
    client.cookies.clear()

    response = sign_in_via_callback(client, monkeypatch, student.upper())
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert sheet.sheet_id in dashboard.text
    assert "Dashboard Student" in dashboard.text
    assert dashboard.text.count('class="cell clue') == GRID_CELL_COUNT

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == student).one()
        assert user.role == "user"


def test_unauthorized_email_renders_access_error(client, monkeypatch):
    stranger = unique_email("stranger")
    response = sign_in_via_callback(client, monkeypatch, stranger)

    assert response.status_code == 403
    assert "You are not a valid user in the system." in response.text
    assert "Please contact an administrator for access." in response.text

    # The same message is shown if they revisit the landing page.
    assert client.get("/").status_code == 403


def test_oauth_failure_shows_error_page(client, monkeypatch):
    def boom(code):
        raise auth.OAuthError("token endpoint unavailable")

    monkeypatch.setattr(auth, "exchange_code_for_email", boom)
    state = start_google_login(client)
    response = client.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
    assert response.status_code == 502
    assert "complete Google sign-in" in response.text
    assert "token endpoint unavailable" in response.text


def test_logout_clears_the_session(client, monkeypatch):
    sign_in_via_callback(client, monkeypatch, ADMIN_EMAIL)
    assert client.get("/admin").status_code == 200

    client.get("/auth/logout", follow_redirects=False)
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")


# ---------------------------------------------------------------------------
# Admin interface
# ---------------------------------------------------------------------------
def test_admin_creates_sheet_with_unique_id_and_assignment(client):
    student = unique_email("assigned")
    sheet = make_sheet(
        client,
        assigned_email=student,
        student="Assignment Student",
        sheet_id="CB-TEST01",
    )

    assert sheet.sheet_id == "CB-TEST01"
    assert sheet.assigned_email == student
    assert len(sheet.question_list) == GRID_CELL_COUNT
    assert len(set(sheet.question_list)) == GRID_CELL_COUNT
    assert sheet.marked == []

    duplicate = client.post(
        "/admin/sheets",
        data={
            "student_name": "Second Student",
            "parent_name": "Parent Name",
            "parent_email": "parent@example.com",
            "parent_whatsapp": "+1 (555) 010-9999",
            "sheet_id": "CB-TEST01",
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 409
    assert "already in use" in duplicate.text


def test_admin_creation_validates_input(client):
    login_as(client, ADMIN_EMAIL)
    missing_name = client.post(
        "/admin/sheets",
        data={
            "student_name": "",
            "parent_name": "Parent",
            "parent_email": "parent@example.com",
            "parent_whatsapp": "+1 (555) 010-9999",
        },
        follow_redirects=False,
    )
    assert missing_name.status_code == 422

    bad_email = client.post(
        "/admin/sheets",
        data={
            "student_name": "Valid Student",
            "parent_name": "Parent",
            "parent_email": "parent@example.com",
            "parent_whatsapp": "+1 (555) 010-9999",
            "assigned_email": "not-an-email",
        },
        follow_redirects=False,
    )
    assert bad_email.status_code == 422


def test_admin_search_filters_by_sheet_id(client):
    alpha = make_sheet(client, student="Alpha Student", sheet_id="CB-ALPHA1")
    beta = make_sheet(client, student="Beta Student", sheet_id="CB-BETA22")

    page = client.get("/admin?q=ALPHA1")
    assert page.status_code == 200
    assert alpha.sheet_id in page.text.split('id="sheet-results"', 1)[1]
    assert beta.sheet_id not in page.text.split('id="sheet-results"', 1)[1]

    page = client.get("/admin?q=beta")
    assert beta.sheet_id in page.text.split('id="sheet-results"', 1)[1]
    assert alpha.sheet_id not in page.text.split('id="sheet-results"', 1)[1]

    everything = client.get("/admin")
    assert alpha.sheet_id in everything.text
    assert beta.sheet_id in everything.text


def test_admin_search_matches_terms_across_sheet_fields(client):
    target = make_sheet(client, student="Ananya Rao", assigned_email="learner@example.com")
    other = make_sheet(client, student="Ananya Shah")
    with SessionLocal() as db:
        stored = db.get(BingoSheet, target.id)
        stored.parent_name = "Meera Patel"
        stored.parent_email = "family@example.com"
        stored.parent_whatsapp = "+1 (408) 555-0199"
        db.commit()

    for query in ("RAO anan", "meera", "family@", "555-0199", "learner@", "ANANYA patel"):
        response = client.get("/admin", params={"q": query})
        assert response.status_code == 200
        assert target.sheet_id in response.text.split('id="sheet-results"', 1)[1]
        assert other.sheet_id not in response.text.split('id="sheet-results"', 1)[1]

    for query in ("Ananya Missing", "%", "_"):
        response = client.get("/admin", params={"q": query})
        assert target.sheet_id not in response.text.split('id="sheet-results"', 1)[1]
        assert other.sheet_id not in response.text.split('id="sheet-results"', 1)[1]


def test_admin_search_filters_student_accounts_without_sheets(client):
    login_as(client, ADMIN_EMAIL)
    email = unique_email("student-search")
    client.post("/admin/students", data={
        "email": email, "student_name": "Tara Kumar", "parent_name": "Priya Sharma",
    })

    for query, matches in (("tar", True), ("kumar sharm", True), ("tarz", False)):
        response = client.get("/admin", params={"q": query})
        rows = response.text.split('id="student-results"', 1)[1].split("</tbody>", 1)[0]
        assert (email in rows) == matches


def test_adding_student_does_not_assign_a_sheet(client):
    email = unique_email("new-student")
    existing = make_sheet(client)
    with SessionLocal() as db:
        sheet_count = db.query(BingoSheet).count()

    details = {
        "student_name": "Asha Rao",
        "parent_name": "Meera Rao",
        "parent_email": "meera@example.com",
        "parent_whatsapp": "+1 (408) 555-0199",
    }
    response = client.post("/admin/students", data={"email": email, **details})
    assert response.status_code == 200
    assert "Student account ready for" in response.text
    assert "Create bingo sheet" in response.text

    with SessionLocal() as db:
        assert db.query(User).filter_by(email=email).one()
        assert db.query(BingoSheet).count() == sheet_count

    response = client.post(
        "/admin/sheets/assign",
        data={"sheet_id": existing.sheet_id, "assigned_email": email},
        follow_redirects=False,
    )
    assert response.status_code == 303

    response = client.post(
        "/admin/sheets", data={"student_email": email}, follow_redirects=False
    )
    assert response.status_code == 303
    generated_id = response.headers["location"].rsplit("/", 1)[-1]
    page = client.get("/admin", params={"q": generated_id})
    student_card = page.text.split('id="student-results"', 1)[1].split("</article>", 1)[0]
    assert "Asha Rao" in student_card
    assert generated_id in student_card
    assert existing.sheet_id not in student_card

    with SessionLocal() as db:
        sheets = db.query(BingoSheet).filter_by(assigned_email=email).all()
        assert {sheet.sheet_id for sheet in sheets} == {existing.sheet_id, generated_id}
        generated = next(sheet for sheet in sheets if sheet.sheet_id == generated_id)
        assert {field: getattr(generated, field) for field in details} == details


def test_edit_student_updates_profile_and_assigned_sheets(client):
    email = unique_email("edit-details")
    sheet = make_sheet(client, assigned_email=email)
    client.post("/admin/students", data={"email": email})
    with SessionLocal() as db:
        student_id = db.query(User).filter_by(email=email).one().id
    url = f"/admin/students/{student_id}/edit"
    details = {
        "student_name": "Nila Rao",
        "parent_name": "Asha Rao",
        "parent_email": "asha@example.com",
        "parent_whatsapp": "",
    }

    login_as(client, email)
    assert client.post(url, data=details).status_code == 403
    login_as(client, ADMIN_EMAIL)
    assert client.post(url, data={**details, "parent_email": "invalid"}).status_code == 422
    response = client.post(url, data=details)
    assert response.status_code == 200
    assert "Saved student details" in response.text
    assert 'value="asha@example.com"' in response.text

    with SessionLocal() as db:
        account = db.get(User, student_id)
        stored = db.get(BingoSheet, sheet.id)
        assert {key: getattr(account, key) for key in details} == details
        assert {key: getattr(stored, key) for key in details} == details
        assert account.email == stored.assigned_email == email


def test_admin_assigns_existing_sheet_and_user_gains_access(client, monkeypatch):
    sheet = make_sheet(client, student="Unassigned Student", sheet_id="CB-ASSIGN1")
    assert sheet.assigned_email is None

    login_as(client, ADMIN_EMAIL)
    response = client.post(
        "/admin/sheets/assign",
        data={"sheet_id": sheet.sheet_id, "assigned_email": " Late.Student@Example.com "},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with SessionLocal() as db:
        refreshed = db.query(BingoSheet).filter(BingoSheet.sheet_id == sheet.sheet_id).one()
        assert refreshed.assigned_email == "late.student@example.com"

    client.cookies.clear()
    sign_in = sign_in_via_callback(client, monkeypatch, "late.student@example.com")
    assert sign_in.headers["location"] == "/dashboard"
    assert sheet.sheet_id in client.get("/dashboard").text


def test_only_admin_can_delete_one_sheet_without_removing_student(client):
    email = unique_email("delete-sheet")
    target = make_sheet(client, assigned_email=email)
    other = make_sheet(client, assigned_email=email)
    url = f"/admin/sheet/{target.sheet_id}/delete"

    login_as(client, email)
    assert client.post(url).status_code == 403
    with SessionLocal() as db:
        assert db.get(BingoSheet, target.id) is not None

    login_as(client, ADMIN_EMAIL)
    response = client.post(url)
    assert response.status_code == 200
    assert f"Deleted bingo sheet <strong>{target.sheet_id}</strong>" in response.text

    with SessionLocal() as db:
        assert db.get(BingoSheet, target.id) is None
        assert db.get(BingoSheet, other.id).assigned_email == email
        assert db.query(User).filter_by(email=email).one()


def test_admin_sheet_detail_and_docx_export(client):
    sheet = make_sheet(client, student="Detail Student", sheet_id="CB-DETAIL")
    login_as(client, ADMIN_EMAIL)

    page = client.get(f"/admin/sheet/{sheet.sheet_id}")
    assert page.status_code == 200
    assert "Detail Student" in page.text
    assert "Download printable .docx" in page.text
    assert "Print this page" in page.text
    assert page.text.count('class="cell clue"') == GRID_CELL_COUNT

    download = client.get(f"/sheet/{sheet.sheet_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert f"Carnatic_Bingo_Sheet_{sheet.sheet_id}.docx" in download.headers[
        "content-disposition"
    ]
    document = Document(BytesIO(download.content))
    assert any(
        f"Bingo Sheet #{sheet.id}" in paragraph.text
        for paragraph in document.paragraphs
    )


def test_non_admin_cannot_reach_admin_pages_or_data(client):
    student = unique_email("plain")
    make_sheet(client, assigned_email=student, student="Plain Student")
    login_as(client, student)

    page = client.get("/admin")
    assert page.status_code == 403
    assert "Administrator access is required" in page.text

    assert client.get("/api/sheets").status_code == 403
    assert client.get("/api/export").status_code == 403


# ---------------------------------------------------------------------------
# User dashboard
# ---------------------------------------------------------------------------
def test_dashboard_is_scoped_to_the_signed_in_user(client):
    owner = unique_email("owner")
    other = unique_email("other")
    mine = make_sheet(client, assigned_email=owner, student="Owner Student", sheet_id="CB-MINE1")
    theirs = make_sheet(client, assigned_email=other, student="Other Student", sheet_id="CB-THEIRS")

    login_as(client, owner)

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert mine.sheet_id in dashboard.text
    assert theirs.sheet_id not in dashboard.text

    assert client.get(f"/sheet/{mine.sheet_id}").status_code == 200
    assert client.get(f"/sheet/{theirs.sheet_id}").status_code == 403

    blocked = client.post(
        f"/dashboard/sheet/{theirs.sheet_id}/state", json={"cell": "A1"}
    )
    assert blocked.status_code == 403


def test_dashboard_tile_state_persists(client):
    student = unique_email("player")
    sheet = make_sheet(client, assigned_email=student, student="Player Student")
    login_as(client, student)

    def post(body):
        return client.post(f"/dashboard/sheet/{sheet.sheet_id}/state", json=body)

    first = post({"cell": "A1"})
    assert first.status_code == 200
    assert first.json()["marked"] == ["A1"]

    toggled_off = post({"cell": "A1"})
    assert toggled_off.json()["marked"] == []

    replaced = post({"cells": ["A1", "E5", "A1"]})
    assert replaced.json()["marked"] == ["A1", "E5"]

    assert post({"cell": "C3"}).status_code == 400
    assert post({"cell": "Z9"}).status_code == 400
    # A bulk replace cannot grant the free space without the MCQ.
    assert "C3" not in post({"cells": ["A1", "C3"]}).json()["marked"]
    assert post({"cells": ["A1", "E5"]}).json()["marked"] == ["A1", "E5"]

    with SessionLocal() as db:
        stored = db.query(BingoSheet).filter(BingoSheet.sheet_id == sheet.sheet_id).one()
        assert stored.marked == ["A1", "E5"]

    page = client.get("/dashboard")
    assert page.text.count("cell clue marked") == 2


def test_dashboard_mcq_unlocks_the_free_space(client):
    student = unique_email("mcq")
    sheet = make_sheet(client, assigned_email=student, student="MCQ Student")
    login_as(client, student)

    with SessionLocal() as db:
        mcq = db.get(MultipleChoiceQuestionList, sheet.multiple_choice_question_id)
        correct = "ABCD".index(mcq.answer_key)
        wrong = (correct + 1) % len(mcq.options)

    rejected = client.post(
        f"/dashboard/sheet/{sheet.sheet_id}/verify", json={"selectedAnswer": wrong}
    )
    assert rejected.status_code == 200
    assert rejected.json()["correct"] is False
    assert "C3" not in rejected.json().get("marked", [])

    accepted = client.post(
        f"/dashboard/sheet/{sheet.sheet_id}/verify", json={"selectedAnswer": correct}
    )
    assert accepted.status_code == 200
    assert accepted.json()["correct"] is True
    assert "C3" in accepted.json()["marked"]
    assert set(accepted.json()) == {"correct", "marked"}
    assert set(rejected.json()) == {"correct", "marked"}
    assert client.get("/api/mc-questions").status_code == 403
    assert client.post("/verify-answer", json={"mcqId": mcq.id, "selectedAnswer": correct}).status_code == 403

    with SessionLocal() as db:
        stored = db.query(BingoSheet).filter(BingoSheet.sheet_id == sheet.sheet_id).one()
        assert "C3" in stored.marked

    assert "FREE ✓" in client.get("/dashboard").text

    login_as(client, ADMIN_EMAIL)
    page = client.get(f"/admin?q={sheet.sheet_id}")
    assert "<th>C3 attempts</th>" in page.text
    assert "<td>2</td>" in page.text
    assert "1 wrong attempt" in page.text
    assert "Correct" in page.text


# ---------------------------------------------------------------------------
# Access guards on the existing pages
# ---------------------------------------------------------------------------
def test_anonymous_sheet_pages_require_sign_in(client):
    sheet = make_sheet(client, student="Guarded Student")
    client.cookies.clear()

    page = client.get(f"/sheet/{sheet.sheet_id}", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/auth/login?next=")

    download = client.get(f"/sheet/{sheet.sheet_id}/download", follow_redirects=False)
    assert download.status_code == 303

    assert client.get("/api/sheets").status_code == 401
    assert client.get("/api/export").status_code == 401


def test_numeric_sheet_url_still_resolves_for_admin(client):
    sheet = make_sheet(client, student="Legacy Url Student")
    login_as(client, ADMIN_EMAIL)

    page = client.get(f"/sheet/{sheet.id}")
    assert page.status_code == 200
    assert page.text.count('class="cell clue"') == GRID_CELL_COUNT


def test_sign_in_is_idempotent_for_existing_accounts(client):
    email = unique_email("repeat")
    with SessionLocal() as db:
        first_id = auth.upsert_user(db, email).id
    with SessionLocal() as db:
        second = auth.upsert_user(db, email.upper())
        assert second.id == first_id
        assert db.query(User).filter(User.email == email).count() == 1
        assert not second.is_admin
