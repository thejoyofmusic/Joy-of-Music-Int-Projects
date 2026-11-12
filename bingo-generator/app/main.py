"""FastAPI app for generating, playing and downloading Carnatic bingo sheets.

Access model
------------
* Anonymous visitors get the sign-in page.
* ``admin`` accounts (``ADMIN_EMAILS`` or a stored admin role) manage every
  sheet: search, create, assign and export.
* ``user`` accounts see only the sheets assigned to their Google email, and can
  play them on ``/dashboard`` (tile marks and the C3 unlock are persisted).
"""

from __future__ import annotations

import json
import logging
import random
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.database import SessionLocal, engine, get_db
from app.docx_export import render_sheet_docx
from app.migrations import run_migrations
from app.models import (
    GRID_CELL_COUNT,
    GRID_POSITIONS,
    MCQ_POSITION,
    BingoQuestionList,
    BingoSheet,
    MultipleChoiceQuestionList,
    User,
    new_sheet_id,
    normalize_email,
    normalize_marked_cells,
    normalize_question_ids,
)
from app.schemas import (
    AdminSheetCreate,
    BingoQuestionListSchema,
    BingoSheetCreate,
    MultipleChoiceQuestionListSchema,
    SheetAssignment,
    StudentDetails,
    normalize_sheet_id,
)
from app.seed import seed_from_docx

logger = logging.getLogger(__name__)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
OPTION_LETTERS = "ABCD"
UNAUTHORIZED_MESSAGE = (
    "You are not a valid user in the system. "
    "Please contact an administrator for access."
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bring the database up to date and seed reference content on boot."""
    run_migrations(engine)
    with SessionLocal() as db:
        try:
            seed_from_docx(db)
        except (OSError, ValueError) as exc:
            logger.warning("Could not seed reference questions from .docx files: %s", exc)
    yield


app = FastAPI(title="Bingo Sheet Generator", lifespan=lifespan)

# Signed, HTTP-only session cookie. Only the signed-in email is stored; the role
# is re-read from the database on each request.
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie=auth.SESSION_COOKIE_NAME,
    max_age=auth.SESSION_MAX_AGE,
    same_site="lax",
    https_only=auth.secure_cookies(),
)
if auth.session_secret_is_ephemeral():
    logger.warning(
        "SESSION_SECRET is not set; using a per-process key. Sessions will be "
        "invalidated on restart. Set SESSION_SECRET for any real deployment."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def ordered_clue_texts(db: Session, sheet: BingoSheet) -> List[str]:
    """Clue texts in board order (A1..E5, skipping C3)."""
    question_ids = normalize_question_ids(sheet.question_list)
    if not question_ids:
        return []
    rows = db.query(BingoQuestionList).filter(BingoQuestionList.id.in_(question_ids)).all()
    by_id = {question.id: question.text for question in rows}
    return [by_id.get(question_id, "") for question_id in question_ids]


def sheet_view(db: Session, sheet: BingoSheet) -> dict[str, Any]:
    """Template context for a sheet: clue per cell, C3 MCQ and saved progress."""
    texts = ordered_clue_texts(db, sheet)
    clues_by_position = {
        position: texts[index] if index < len(texts) else ""
        for index, position in enumerate(GRID_POSITIONS)
    }
    return {
        "sheet": sheet,
        "clues_by_position": clues_by_position,
        "marked": sheet.marked,
        "mc_question": db.get(MultipleChoiceQuestionList, sheet.multiple_choice_question_id),
    }


def correct_option_index(mc_question: MultipleChoiceQuestionList) -> Optional[int]:
    """Resolve the stored answer_key to a 0-based option index.

    answer_key is normally a letter (A-D), but numeric keys and the full option
    text are also tolerated so legacy rows keep verifying correctly.
    """
    options = list(mc_question.options or [])
    key = str(mc_question.answer_key or "").strip()
    if not key:
        return None

    if len(key) == 1 and key.upper() in OPTION_LETTERS:
        index = OPTION_LETTERS.index(key.upper())
        return index if index < len(options) else None

    if key.isdigit():
        index = int(key)
        if 0 <= index < len(options):
            return index
        if 1 <= index <= len(options):
            return index - 1
        return None

    for index, option in enumerate(options):
        if str(option).strip().casefold() == key.casefold():
            return index
    return None


def selected_option_index(value: Any, options: List[str]) -> Optional[int]:
    """Resolve a submitted answer to a 0-based option index.

    Accepts an index (0-based), a letter (A-D), a numeric string, or the option text.
    """
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None

    text = str(value).strip()
    if not text:
        return None

    if len(text) == 1 and text.upper() in OPTION_LETTERS:
        return OPTION_LETTERS.index(text.upper())

    if text.lstrip("+-").isdigit():
        return int(text)

    for index, option in enumerate(options):
        if str(option).strip().casefold() == text.casefold():
            return index
    return None


def validation_error_message(exc: ValidationError) -> str:
    """Flatten a pydantic error into a single readable sentence."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg', 'invalid value')}" if location else error.get("msg", "invalid value"))
    return "; ".join(parts) or "Invalid data"


def error_page(
    request: Request,
    message: str,
    status_code: int = 403,
    *,
    detail: Optional[str] = None,
    user: Optional[User] = None,
) -> Response:
    """Render the shared error page (used for every access failure)."""
    # Only an otherwise-unusable account on a local install may claim admin.
    can_promote = message == UNAUTHORIZED_MESSAGE and auth.can_self_promote(request, user)
    return templates.TemplateResponse(
        request,
        "error.html",
        {"message": message, "detail": detail, "user": user, "can_promote": can_promote},
        status_code=status_code,
    )


def login_redirect(request: Request) -> RedirectResponse:
    """Send anonymous visitors to Google sign-in, remembering where they were."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/auth/login?next={quote(target, safe='/')}", status_code=303)


def safe_next(value: Optional[str]) -> Optional[str]:
    """Only allow same-site relative redirects after sign-in."""
    if not value:
        return None
    target = value.strip()
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def find_sheet(db: Session, identifier: str) -> Optional[BingoSheet]:
    """Look a sheet up by public ``sheet_id``, falling back to the legacy id."""
    cleaned = str(identifier or "").strip()
    if not cleaned:
        return None
    sheet = (
        db.query(BingoSheet)
        .filter(func.upper(BingoSheet.sheet_id) == normalize_sheet_id(cleaned))
        .first()
    )
    if sheet is None and cleaned.isdigit():
        sheet = db.get(BingoSheet, int(cleaned))
    return sheet


def ensure_unique_sheet_id(db: Session, requested: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Reserve a sheet id. Returns ``(sheet_id, error)``.

    An explicit id must be free; a generated one is retried on the (very
    unlikely) random collision.
    """
    if requested:
        canonical = normalize_sheet_id(requested)
        exists = (
            db.query(func.count(BingoSheet.id))
            .filter(func.upper(BingoSheet.sheet_id) == canonical)
            .scalar()
        )
        if exists:
            return None, f'Sheet ID "{canonical}" is already in use.'
        return canonical, None

    for _ in range(20):
        candidate = new_sheet_id()
        exists = (
            db.query(func.count(BingoSheet.id))
            .filter(func.upper(BingoSheet.sheet_id) == candidate)
            .scalar()
        )
        if not exists:
            return candidate, None
    return None, "Could not allocate a unique sheet ID. Please try again."


def random_board(db: Session) -> Tuple[Optional[List[int]], Optional[int], Optional[str]]:
    """Draw a fresh board from the pools. Returns ``(question_ids, mcq_id, error)``."""
    available_ids = [row[0] for row in db.query(BingoQuestionList.id).all()]
    if len(available_ids) < GRID_CELL_COUNT:
        return (
            None,
            None,
            f"Need at least {GRID_CELL_COUNT} bingo questions in the database, "
            f"but only found {len(available_ids)}",
        )

    mcq_ids = [row[0] for row in db.query(MultipleChoiceQuestionList.id).all()]
    if not mcq_ids:
        return None, None, "No multiple choice questions in database"

    return random.sample(available_ids, GRID_CELL_COUNT), random.choice(mcq_ids), None


async def request_payload(request: Request) -> Dict[str, Any]:
    """Read a JSON object or a form body as a dict."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    return dict(await request.form())


def verify_selection(
    mc_question: MultipleChoiceQuestionList, selected_raw: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Check a submitted answer. Returns ``(result, error)``."""
    options = list(mc_question.options or [])
    correct_index = correct_option_index(mc_question)
    if correct_index is None or correct_index >= len(options):
        return None, "This question has an invalid stored answer key."

    selected_index = selected_option_index(selected_raw, options)
    if selected_index is None:
        return None, "selected_answer is required (0-based option index)."
    if not 0 <= selected_index < len(options):
        return None, f"selected_answer must be between 0 and {len(options) - 1}."

    return (
        {
            "correct": selected_index == correct_index,
            "correctIndex": correct_index,
            "correctAnswer": (
                OPTION_LETTERS[correct_index]
                if correct_index < len(OPTION_LETTERS)
                else str(correct_index)
            ),
            "question": mc_question.question,
            "options": options,
        },
        None,
    )


def create_sheet(
    db: Session,
    payload: BingoSheetCreate,
    *,
    sheet_id: Optional[str] = None,
    assigned_email: Optional[str] = None,
) -> BingoSheet:
    """Persist a validated sheet and return it."""
    sheet = BingoSheet(
        sheet_id=sheet_id or new_sheet_id(),
        student_name=payload.student_name,
        parent_name=payload.parent_name,
        parent_email=payload.parent_email,
        parent_whatsapp=payload.parent_whatsapp,
        assigned_email=assigned_email,
        marked_cells=[],
        question_list=list(payload.question_list),
        multiple_choice_question_id=payload.multiple_choice_question_id,
    )
    db.add(sheet)
    db.commit()
    db.refresh(sheet)
    return sheet


def admin_guard(
    request: Request, db: Session
) -> Tuple[Optional[User], Optional[Response]]:
    """Authenticate + authorize an admin page. Returns ``(user, error_response)``."""
    user = auth.current_user(request, db)
    if user is None:
        return None, login_redirect(request)
    if not user.is_admin:
        return None, error_page(
            request,
            "Administrator access is required for this page.",
            403,
            detail=f"You are signed in as {user.email}.",
            user=user,
        )
    return user, None


def api_admin_guard(
    request: Request, db: Session
) -> Tuple[Optional[User], Optional[Response]]:
    """JSON variant of :func:`admin_guard` for the data endpoints."""
    user = auth.current_user(request, db)
    if user is None:
        return None, JSONResponse({"error": "Sign in required"}, status_code=401)
    if not user.is_admin:
        return None, JSONResponse(
            {"error": "Administrator access required"}, status_code=403
        )
    return user, None


# ---------------------------------------------------------------------------
# Sign-in landing
# ---------------------------------------------------------------------------
@app.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    """Role-aware landing page: sign in, admin console, or dashboard."""
    user = auth.current_user(request, db)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "configured": auth.oauth_configured(),
                "local_login": auth.local_login_enabled() and auth.request_is_local(request),
            },
        )
    destination = auth.landing_url(db, user)
    if destination:
        return RedirectResponse(url=destination, status_code=303)
    return error_page(request, UNAUTHORIZED_MESSAGE, 403, user=user)


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------
@app.get("/auth/login", name="auth_login")
def auth_login(request: Request, next: Optional[str] = None):
    """Start the Google authorization-code flow."""
    if not auth.oauth_configured():
        return error_page(
            request,
            "Google sign-in is not configured yet.",
            503,
            detail=(
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in "
                "bingo-generator/.env, then restart the server."
            ),
        )

    state = secrets.token_urlsafe(24)
    request.session[auth.SESSION_STATE_KEY] = state
    target = safe_next(next)
    if target:
        request.session[auth.SESSION_NEXT_KEY] = target

    return RedirectResponse(url=auth.authorization_url(state), status_code=303)


@app.get("/auth/callback", name="auth_callback")
def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Finish sign-in and route the account to its role's home page."""
    if error:
        return error_page(
            request,
            "Google sign-in was cancelled.",
            400,
            detail=f"Google returned: {error}",
        )
    if not auth.oauth_configured():
        return error_page(request, "Google sign-in is not configured yet.", 503)

    expected_state = request.session.pop(auth.SESSION_STATE_KEY, None)
    if (
        not expected_state
        or not state
        or not secrets.compare_digest(str(state), str(expected_state))
    ):
        return error_page(
            request,
            "That sign-in link is no longer valid.",
            400,
            detail="Please start the sign-in again.",
        )
    if not code:
        return error_page(request, "Google did not send an authorization code.", 400)

    try:
        email = auth.exchange_code_for_email(code)
        user = auth.upsert_user(db, email)
    except auth.OAuthError as exc:
        logger.warning("Google sign-in failed: %s", exc)
        return error_page(
            request,
            "We couldn't complete Google sign-in.",
            502,
            detail=str(exc),
        )
    except ValueError as exc:
        logger.warning("Could not create the account for the Google sign-in: %s", exc)
        return error_page(request, "We couldn't complete Google sign-in.", 502)

    auth.sign_in(request, user)
    destination = auth.landing_url(db, user)
    if destination is None:
        return error_page(request, UNAUTHORIZED_MESSAGE, 403, user=user)

    target = safe_next(request.session.pop(auth.SESSION_NEXT_KEY, None))
    return RedirectResponse(url=target or destination, status_code=303)


@app.get("/setup", name="setup_page")
def setup_page(request: Request, saved: int = 0):
    """Local page that collects Google OAuth credentials and saves them to .env."""
    if not auth.local_login_enabled():
        return error_page(
            request,
            "Setup is only available locally.",
            403,
            detail="Edit .env on the server host instead.",
        )
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "configured": auth.oauth_configured(),
            "callback": auth.callback_url(),
            "client_id": auth.google_client_id(),
            "admin_emails": ", ".join(sorted(auth.admin_emails())),
            "saved": bool(saved),
            "error": None,
        },
    )


@app.post("/setup", name="setup_save")
async def setup_save(request: Request):
    """Validate and persist Google OAuth credentials, then enable sign-in live."""
    if not auth.local_login_enabled():
        return error_page(request, "Setup is only available locally.", 403)

    form = await request.form()
    client_id = str(form.get("google_client_id", "")).strip()
    client_secret = str(form.get("google_client_secret", "")).strip()
    admin_raw = str(form.get("admin_emails", "")).strip()
    emails = [
        part.strip()
        for part in admin_raw.replace(";", ",").replace("\n", ",").split(",")
        if part.strip()
    ]

    error: Optional[str] = None
    if not auth.looks_like_google_client_id(client_id):
        error = (
            "That doesn't look like a Google OAuth Client ID — it ends with "
            ".apps.googleusercontent.com. Copy it from the Credentials page."
        )
    elif not client_secret:
        error = "Paste the Client secret as well (it is shown next to the Client ID)."
    elif any(not auth.looks_like_email(email) for email in emails):
        error = "Administrator emails must be valid addresses, e.g. you@gmail.com."

    if error:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "configured": auth.oauth_configured(),
                "callback": auth.callback_url(),
                "client_id": client_id,
                "admin_emails": admin_raw,
                "saved": False,
                "error": error,
            },
            status_code=400,
        )

    values = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
    }
    if emails:
        values["ADMIN_EMAILS"] = ",".join(emails)
    auth.save_settings(values)
    return RedirectResponse(url="/setup?saved=1", status_code=303)


@app.post("/auth/local-login", name="auth_local_login")
async def auth_local_login(request: Request, db: Session = Depends(get_db)):
    """Sign in from this machine without Google (development only).

    Guarded twice: the feature must be enabled for a loopback ``APP_BASE_URL``,
    and the request itself must arrive from this computer. Roles and access are
    the same as Google sign-in — an unassigned account still gets the error page.
    """
    if not auth.local_login_enabled() or not auth.request_is_local(request):
        return error_page(
            request,
            "Local sign-in is not available.",
            403,
            detail=(
                "It only works while APP_BASE_URL is http://127.0.0.1 or "
                "http://localhost. Use Google sign-in instead."
            ),
        )

    form = await request.form()
    email = normalize_email(form.get("email"))
    if not auth.looks_like_email(email):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "configured": auth.oauth_configured(),
                "local_login": True,
                "error": "Enter a valid email address, e.g. you@example.com.",
            },
            status_code=400,
        )

    try:
        user = auth.upsert_local_user(db, email)
    except ValueError as exc:
        logger.warning("Local sign-in failed for %s: %s", email, exc)
        return error_page(request, "We couldn't sign you in.", 400)

    auth.sign_in(request, user)
    destination = auth.landing_url(db, user)
    if destination is None:
        return error_page(request, UNAUTHORIZED_MESSAGE, 403, user=user)
    return RedirectResponse(url=destination, status_code=303)


@app.post("/auth/promote", name="auth_promote")
def auth_promote(request: Request, db: Session = Depends(get_db)):
    """Local-only escape hatch: make the signed-in account an administrator.

    For someone running the app on their own machine who signed in with the right
    address but has no sheets assigned and no ``ADMIN_EMAILS`` entry yet. Works
    only from this computer, and never on an ``https`` deployment.
    """
    user = auth.current_user(request, db)
    if user is None:
        return login_redirect(request)
    if not auth.can_self_promote(request, user):
        return error_page(
            request,
            "Administrator promotion is not available here.",
            403,
            detail="Ask an existing administrator to assign you a sheet.",
            user=user,
        )

    auth.promote_to_admin(db, user)
    logger.warning("Local sign-in promoted %s to administrator", user.email)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/auth/logout")
@app.post("/auth/logout")
def auth_logout(request: Request):
    """Clear the session cookie and return to the sign-in page."""
    auth.sign_out(request)
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.get("/admin")
def admin_home(
    request: Request,
    q: str = "",
    added: str = "",
    updated: str = "",
    removed: str = "",
    deleted: int = 0,
    deleted_sheet: str = "",
    db: Session = Depends(get_db),
):
    """Search sheets by ID, student or parent details, or assigned email."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    search = q.strip()
    sheet_query = db.query(BingoSheet).order_by(BingoSheet.id.desc())
    account_query = db.query(User).filter(User.role != "admin").order_by(User.email)
    sheet_fields = (
        BingoSheet.sheet_id,
        BingoSheet.student_name,
        BingoSheet.parent_name,
        BingoSheet.parent_email,
        BingoSheet.parent_whatsapp,
        BingoSheet.assigned_email,
    )
    account_fields = (
        User.student_name, User.email, User.parent_name, User.parent_email, User.parent_whatsapp
    )
    matching_sheets = sheet_query
    matching_accounts = account_query
    for term in search.split():
        matching_sheets = matching_sheets.filter(
            or_(*(field.icontains(term, autoescape=True) for field in sheet_fields))
        )
        matching_accounts = matching_accounts.filter(
            or_(*(field.icontains(term, autoescape=True) for field in account_fields))
        )
    all_accounts = account_query.all()
    matched_accounts = {account.email for account in matching_accounts.all()} if search else {
        account.email for account in all_accounts
    }
    known_accounts = {account.email for account in all_accounts}
    matching_ids = {sheet.id for sheet in matching_sheets.all()} if search else set()
    sheets_by_student = {email: [] for email in known_accounts}
    other_sheets = []
    board_counts = {}
    for sheet in sheet_query.all():
        email = normalize_email(sheet.assigned_email)
        board_counts[email] = board_counts.get(email, 0) + 1
        if search and sheet.id not in matching_ids and email not in matched_accounts:
            continue
        if email in known_accounts:
            sheets_by_student[email].append(sheet)
        else:
            other_sheets.append(sheet)

    accounts = [
        account for account in all_accounts
        if account.email in matched_accounts or sheets_by_student[account.email]
    ]
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "query": search,
            "matched": len(other_sheets) + sum(map(len, sheets_by_student.values())),
            "accounts": accounts,
            "assignment_accounts": all_accounts,
            "sheets_by_student": sheets_by_student,
            "other_sheets": other_sheets,
            "board_counts": board_counts,
            "added": added,
            "updated": updated,
            "removed": removed,
            "deleted": deleted,
            "deleted_sheet": deleted_sheet,
        },
    )


@app.post("/admin/sheets")
async def admin_create_sheet(request: Request, db: Session = Depends(get_db)):
    """Create a sheet and optionally assign it to a Gmail address."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    try:
        form_data = {
            key: (str(value).strip() if value is not None else "")
            for key, value in (await request.form()).items()
        }
    except Exception:
        return error_page(request, "Could not read the form.", 400, user=user)

    student_email = normalize_email(form_data.get("student_email", ""))
    if student_email:
        account = db.query(User).filter_by(email=student_email).first()
        if account is None or account.is_admin:
            return error_page(request, "Student account not found.", 404, user=user)
        form_data["assigned_email"] = account.email
        form_data["student_name"] = account.student_name or account.email.split("@")[0][:120]
        form_data["parent_name"] = account.parent_name
        form_data["parent_email"] = account.parent_email
        form_data["parent_whatsapp"] = account.parent_whatsapp

    question_ids, mcq_id, pool_error = random_board(db)
    if pool_error:
        return error_page(request, pool_error, 400, user=user)

    try:
        payload = AdminSheetCreate(
            student_name=form_data.get("student_name", ""),
            parent_name=form_data.get("parent_name", ""),
            parent_email=form_data.get("parent_email", ""),
            parent_whatsapp=form_data.get("parent_whatsapp", ""),
            question_list=question_ids,
            multiple_choice_question_id=mcq_id,
            sheet_id=form_data.get("sheet_id") or None,
            assigned_email=form_data.get("assigned_email") or None,
        )
    except ValidationError as exc:
        return error_page(request, validation_error_message(exc), 422, user=user)

    sheet_id, id_error = ensure_unique_sheet_id(db, payload.sheet_id)
    if id_error:
        return error_page(request, id_error, 409, user=user)

    sheet = create_sheet(
        db,
        payload,
        sheet_id=sheet_id,
        assigned_email=payload.assigned_email,
    )
    logger.info(
        "Admin %s created sheet %s for %s",
        user.email,
        sheet.sheet_id,
        sheet.assigned_email or "unassigned",
    )
    return RedirectResponse(url=f"/admin/sheet/{sheet.sheet_id}", status_code=303)


@app.get("/admin/sheet/{identifier}")
def admin_sheet_detail(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """One sheet, with print/export and assignment controls."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    sheet = find_sheet(db, identifier)
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)

    context = sheet_view(db, sheet)
    context.update({"user": user, "is_admin": True, "assign_error": None})
    return templates.TemplateResponse(request, "sheet_detail.html", context)


@app.post("/admin/sheet/{identifier}/delete")
def admin_delete_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    sheet = find_sheet(db, identifier)
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)

    sheet_id = sheet.sheet_id
    db.delete(sheet)
    db.commit()
    logger.info("Admin %s deleted sheet %s", user.email, sheet_id)
    return RedirectResponse(url=f"/admin?deleted_sheet={quote(sheet_id, safe='')}", status_code=303)


def assign_sheet(sheet: BingoSheet, email: Optional[str]) -> None:
    if normalize_email(sheet.assigned_email) != normalize_email(email):
        sheet.marked_cells = [position for position in sheet.marked if position != MCQ_POSITION]
        sheet.mcq_incorrect_attempts = 0
        sheet.mcq_attempts = 0
        sheet.last_activity_at = None
    sheet.assigned_email = email


@app.post("/admin/sheet/{identifier}/assign")
async def admin_assign_sheet(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """Attach (or detach) a sheet to a Gmail address."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    sheet = find_sheet(db, identifier)
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)

    form = await request.form()
    try:
        assignment = SheetAssignment(assigned_email=form.get("assigned_email"))
    except ValidationError as exc:
        context = sheet_view(db, sheet)
        context.update(
            {
                "user": user,
                "is_admin": True,
                "assign_error": validation_error_message(exc),
            }
        )
        return templates.TemplateResponse(
            request, "sheet_detail.html", context, status_code=422
        )

    assign_sheet(sheet, assignment.assigned_email)
    db.commit()
    logger.info(
        "Admin %s assigned sheet %s to %s",
        user.email,
        sheet.sheet_id,
        assignment.assigned_email or "nobody",
    )
    return RedirectResponse(url=f"/admin/sheet/{sheet.sheet_id}", status_code=303)


@app.post("/admin/sheets/assign")
async def admin_assign_existing(request: Request, db: Session = Depends(get_db)):
    """Assign an existing sheet to an email straight from the admin table."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    form = await request.form()
    sheet = find_sheet(db, str(form.get("sheet_id") or ""))
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)

    try:
        assignment = SheetAssignment(assigned_email=form.get("assigned_email"))
    except ValidationError as exc:
        return error_page(request, validation_error_message(exc), 422, user=user)

    assign_sheet(sheet, assignment.assigned_email)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


def update_student_details(db: Session, account: User, details: dict[str, str]) -> None:
    for field, value in details.items():
        setattr(account, field, value)
    if details:
        sheet_details = dict(details)
        if sheet_details.get("student_name") == "":
            sheet_details["student_name"] = account.email.split("@")[0][:120]
        db.query(BingoSheet).filter(
            func.lower(BingoSheet.assigned_email) == account.email
        ).update(sheet_details, synchronize_session=False)


@app.post("/admin/students")
async def admin_add_student(request: Request, db: Session = Depends(get_db)):
    """Register a student so an admin can assign a sheet later."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    try:
        form = await request.form()
    except Exception:
        return error_page(request, "Could not read the form.", 400, user=user)

    values = {
        key: (str(value).strip() if value is not None else "")
        for key, value in form.items()
    }
    email = normalize_email(values.get("email") or "")
    if not email or not auth.EMAIL_PATTERN.match(email):
        return error_page(
            request,
            "Enter the student's Google email address.",
            422,
            detail="For example student@gmail.com.",
            user=user,
        )

    try:
        details = StudentDetails.model_validate(values)
    except ValidationError as exc:
        return error_page(request, validation_error_message(exc), 422, user=user)

    account = db.query(User).filter_by(email=email).first()
    if account is None:
        account = User(email=email, role=auth.role_for_email(email))
        db.add(account)
    if not account.is_admin:
        account.registered_student = True
    update_student_details(
        db, account, {field: value for field, value in details.model_dump().items() if value}
    )
    db.commit()
    logger.info("Admin %s saved student details for %s", user.email, email)
    return RedirectResponse(url=f"/admin?added={quote(email, safe='')}", status_code=303)


@app.post("/admin/students/{student_id}/edit")
async def admin_edit_student(student_id: int, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    account = db.get(User, student_id)
    if account is None or account.is_admin:
        return error_page(request, "Student account not found.", 404, user=user)

    try:
        form = await request.form()
    except Exception:
        return error_page(request, "Could not read the form.", 400, user=user)
    try:
        details = StudentDetails.model_validate(dict(form))
    except ValidationError as exc:
        return error_page(request, validation_error_message(exc), 422, user=user)

    account.registered_student = True
    update_student_details(db, account, details.model_dump(exclude_unset=True))
    db.commit()
    return RedirectResponse(url=f"/admin?updated={quote(account.email, safe='')}", status_code=303)


@app.post("/admin/students/remove")
async def admin_remove_student(request: Request, db: Session = Depends(get_db)):
    """Remove a student account and detach its boards.

    Deleting the ``app_user`` row alone would not revoke anything: Google sign-in
    recreates the account on the next attempt. Detaching the boards is what
    actually takes the dashboard away.
    """
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    try:
        form = await request.form()
    except Exception:
        return error_page(request, "Could not read the form.", 400, user=user)

    email = normalize_email(str(form.get("email") or ""))
    account = db.query(User).filter(User.email == email).first() if email else None
    if account is None:
        return error_page(request, "That account is not in the list.", 404, user=user)
    if account.is_admin:
        return error_page(
            request,
            "Administrator accounts cannot be removed here.",
            403,
            detail=(
                "Remove the address from ADMIN_EMAILS in bingo-generator/.env instead, "
                "so the admin console stays reachable."
            ),
            user=user,
        )

    detached = (
        db.query(BingoSheet)
        .filter(func.lower(BingoSheet.assigned_email) == email)
        .all()
    )
    delete_boards = str(form.get("delete_sheets") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if delete_boards:
        for sheet in detached:
            db.delete(sheet)
    else:
        for sheet in detached:
            assign_sheet(sheet, None)

    db.delete(account)
    db.commit()
    removed_boards = len(detached) if delete_boards else 0
    logger.info(
        "Admin %s removed student %s (%d board(s) %s)",
        user.email,
        email,
        len(detached),
        "deleted" if delete_boards else "detached",
    )
    return RedirectResponse(
        url=f"/admin?removed={email}&deleted={removed_boards}", status_code=303
    )


# ---------------------------------------------------------------------------
# User dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard")
@app.get("/dashboard/assignments")
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Show recent work or list every assignment for the signed-in student."""
    user = auth.current_user(request, db)
    if user is None:
        return login_redirect(request)
    if user.is_admin:
        return RedirectResponse(url="/admin", status_code=303)

    sheets = (
        db.query(BingoSheet)
        .filter(func.lower(BingoSheet.assigned_email) == user.email)
        .order_by(BingoSheet.id.desc())
        .all()
    )
    if not sheets and not user.registered_student:
        return error_page(request, UNAUTHORIZED_MESSAGE, 403, user=user)

    assignments = request.url.path == "/dashboard/assignments"
    recent = sorted(
        (sheet for sheet in sheets if sheet.last_activity_at or sheet.marked or sheet.mcq_attempts),
        key=lambda sheet: (sheet.last_activity_at or datetime.min, sheet.id),
        reverse=True,
    )[:3]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "student_name": user.student_name or (sheets[0].student_name if sheets else ""),
            "active_tab": "assignments" if assignments else "dashboard",
            "sheets": sheets if assignments else recent or sheets[:1],
            "has_recent_work": bool(recent),
        },
    )


def dashboard_sheet(
    request: Request, db: Session, identifier: str
) -> Tuple[Optional[User], Optional[BingoSheet], Optional[Response]]:
    """Resolve a board the current user is allowed to play."""
    user = auth.current_user(request, db)
    if user is None:
        return None, None, JSONResponse({"error": "Sign in required"}, status_code=401)

    sheet = find_sheet(db, identifier)
    if sheet is None:
        return user, None, JSONResponse({"error": "Sheet not found"}, status_code=404)
    if not auth.can_access_sheet(user, sheet):
        return user, None, JSONResponse({"error": "Access denied"}, status_code=403)
    return user, sheet, None


@app.get("/dashboard/sheet/{identifier}")
def play_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    user, sheet, failure = dashboard_sheet(request, db, identifier)
    if user is None:
        return login_redirect(request)
    if failure is not None:
        message = "Sheet not found." if failure.status_code == 404 else "This sheet is not assigned to you."
        return error_page(request, message, failure.status_code, user=user)
    if user.is_admin:
        return RedirectResponse(url=f"/admin/sheet/{sheet.sheet_id}", status_code=303)

    context = sheet_view(db, sheet)
    context.update({"user": user, "active_tab": "assignments"})
    return templates.TemplateResponse(request, "play_sheet.html", context)


@app.post("/dashboard/sheet/{identifier}/state")
async def dashboard_sheet_state(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """Toggle or replace the saved tile marks for one board."""
    user, sheet, failure = dashboard_sheet(request, db, identifier)
    if failure:
        return failure

    try:
        payload = await request_payload(request)
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    marked = list(sheet.marked)
    if "cells" in payload:
        raw_cells = payload["cells"]
        if not isinstance(raw_cells, (list, tuple)):
            return JSONResponse(
                {"error": "cells must be a list of board positions"}, status_code=400
            )
        # C3 is earned through the MCQ, so a bulk replace cannot add it.
        already_unlocked = MCQ_POSITION in sheet.marked
        marked = [
            position
            for position in normalize_marked_cells(raw_cells)
            if position != MCQ_POSITION or already_unlocked
        ]
    else:
        position = str(payload.get("cell") or "").strip().upper()
        if position == MCQ_POSITION:
            return JSONResponse(
                {"error": "The free space unlocks by answering the MCQ."}, status_code=400
            )
        if position not in GRID_POSITIONS:
            return JSONResponse(
                {"error": f"Unknown board position: {position or '(empty)'}"},
                status_code=400,
            )
        if position in marked:
            marked.remove(position)
        else:
            marked.append(position)

    sheet.marked_cells = normalize_marked_cells(marked)
    sheet.last_activity_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return JSONResponse(
        {
            "sheet_id": sheet.sheet_id,
            "marked": sheet.marked,
            "count": len(sheet.marked),
        }
    )


@app.post("/dashboard/sheet/{identifier}/verify")
async def dashboard_verify(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """Check this board's MCQ and persist the C3 unlock when correct."""
    user, sheet, failure = dashboard_sheet(request, db, identifier)
    if failure:
        return failure

    mc_question = db.get(
        MultipleChoiceQuestionList, sheet.multiple_choice_question_id
    )
    if mc_question is None:
        return JSONResponse(
            {"correct": False, "error": "MCQ not found"}, status_code=404
        )

    try:
        payload = await request_payload(request)
    except Exception:
        return JSONResponse(
            {"correct": False, "error": "Invalid request body"}, status_code=400
        )

    selected_raw = payload.get(
        "selected_answer",
        payload.get("selectedAnswer", payload.get("option", payload.get("optionIndex"))),
    )
    result, error = verify_selection(mc_question, selected_raw)
    if error:
        return JSONResponse({"correct": False, "error": error}, status_code=400)

    correct = result["correct"]
    counters = {
        BingoSheet.mcq_attempts: BingoSheet.mcq_attempts + 1,
        BingoSheet.last_activity_at: datetime.now(timezone.utc).replace(tzinfo=None),
    }
    if correct:
        sheet.marked_cells = normalize_marked_cells(list(sheet.marked) + [MCQ_POSITION])
    else:
        counters[BingoSheet.mcq_incorrect_attempts] = BingoSheet.mcq_incorrect_attempts + 1
    db.query(BingoSheet).filter_by(id=sheet.id).update(counters)
    db.commit()
    return JSONResponse({"correct": correct, "marked": sheet.marked})



# ---------------------------------------------------------------------------
# Sheet pages
# ---------------------------------------------------------------------------
def viewable_sheet(
    request: Request, db: Session, identifier: str
) -> Tuple[Optional[User], Optional[BingoSheet], Optional[Response]]:
    """Guard for the human-facing sheet pages (owner or admin only)."""
    user = auth.current_user(request, db)
    if user is None:
        return None, None, login_redirect(request)

    sheet = find_sheet(db, identifier)
    if sheet is None:
        return user, None, error_page(request, "Sheet not found.", 404, user=user)
    if not auth.can_access_sheet(user, sheet):
        return (
            user,
            None,
            error_page(
                request,
                "You don't have access to this sheet.",
                403,
                detail="Ask an administrator to assign it to your Google account.",
                user=user,
            ),
        )
    return user, sheet, None


@app.get("/sheet/{identifier}")
def get_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    """Show a stored bingo sheet (read-only; playing happens on the dashboard)."""
    user, sheet, failure = viewable_sheet(request, db, identifier)
    if failure:
        return failure

    context = sheet_view(db, sheet)
    context.update({"user": user, "is_admin": user.is_admin, "assign_error": None})
    return templates.TemplateResponse(request, "sheet_detail.html", context)


@app.post("/sheet/{identifier}/details")
async def edit_sheet_details(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """Let the sheet's owner or an admin correct the student/parent details."""
    user, sheet, failure = viewable_sheet(request, db, identifier)
    if failure:
        return failure

    try:
        form = await request.form()
    except Exception:
        return error_page(request, "Could not read the form.", 400, user=user)

    values = {
        key: (str(value).strip() if value is not None else "")
        for key, value in form.items()
    }
    parent_email = values.get("parent_email") or ""
    if parent_email and not auth.EMAIL_PATTERN.match(parent_email):
        return error_page(
            request, "That parent email does not look valid.", 422, user=user
        )

    sheet.student_name = values.get("student_name") or sheet.student_name
    sheet.parent_name = values.get("parent_name") or ""
    sheet.parent_email = parent_email
    sheet.parent_whatsapp = values.get("parent_whatsapp") or ""
    db.commit()
    logger.info("%s updated details for sheet %s", user.email, sheet.sheet_id)
    return RedirectResponse(url=f"/sheet/{sheet.sheet_id}", status_code=303)


@app.get("/sheet/{identifier}/download")
def download_sheet(
    identifier: str, request: Request, db: Session = Depends(get_db)
):
    """Render the printable .docx for a stored sheet."""
    user, sheet, failure = viewable_sheet(request, db, identifier)
    if failure:
        return failure

    clue_texts = ordered_clue_texts(db, sheet)
    if len(clue_texts) != GRID_CELL_COUNT or any(not text for text in clue_texts):
        return JSONResponse(
            {"error": "This sheet is missing clue references and cannot be exported."},
            status_code=400,
        )

    mc_question = db.get(MultipleChoiceQuestionList, sheet.multiple_choice_question_id)
    if mc_question is None:
        return JSONResponse(
            {"error": "This sheet's multiple choice question is missing."}, status_code=400
        )

    try:
        content = render_sheet_docx(
            sheet,
            clue_texts,
            {
                "question": mc_question.question,
                "options": list(mc_question.options or []),
                "answer": mc_question.answer_key,
            },
        )
    except FileNotFoundError as exc:
        return JSONResponse({"error": f"Bingo template not found: {exc}"}, status_code=500)
    except ValueError as exc:
        return JSONResponse(
            {"error": f"Could not render the printable sheet: {exc}"}, status_code=500
        )

    filename = f"Carnatic_Bingo_Sheet_{sheet.sheet_id}.docx"
    return Response(
        content=content,
        media_type=DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Generator (admin)
# ---------------------------------------------------------------------------
@app.get("/generate-sheet")
def generate_sheet_get():
    """A direct visit / refresh of this POST-only URL goes to the admin console."""
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/generate-sheet")
async def generate_sheet(request: Request, db: Session = Depends(get_db)):
    """Create an unassigned sheet from the admin form."""
    user, failure = admin_guard(request, db)
    if failure:
        return failure

    form = await request.form()
    form_data = {
        key: (str(value).strip() if value is not None else "")
        for key, value in form.items()
    }

    # 24 clue ids, or a random draw from the pool when the form omits them.
    if form_data.get("question_list"):
        try:
            question_ids = [
                int(part) for part in form_data["question_list"].split(",") if part.strip()
            ]
        except ValueError:
            return JSONResponse(
                {"error": "question_list must be a comma-separated list of integers"},
                status_code=400,
            )
    else:
        available_ids = [row[0] for row in db.query(BingoQuestionList.id).all()]
        if len(available_ids) < GRID_CELL_COUNT:
            return JSONResponse(
                {
                    "error": (
                        f"Need at least {GRID_CELL_COUNT} bingo questions in the "
                        f"database, but only found {len(available_ids)}"
                    )
                },
                status_code=400,
            )
        question_ids = random.sample(available_ids, GRID_CELL_COUNT)

    if len(question_ids) == GRID_CELL_COUNT:
        found_ids = {
            row[0]
            for row in db.query(BingoQuestionList.id)
            .filter(BingoQuestionList.id.in_(question_ids))
            .all()
        }
        missing = sorted(set(question_ids) - found_ids)
        if missing:
            return JSONResponse(
                {"error": f"Unknown bingo question ids: {', '.join(map(str, missing))}"},
                status_code=400,
            )

    # Centre-square MCQ, or a random pick when the form omits it.
    if form_data.get("multiple_choice_question_id"):
        try:
            mcq_id = int(form_data["multiple_choice_question_id"])
        except ValueError:
            return JSONResponse(
                {"error": "multiple_choice_question_id must be an integer"},
                status_code=400,
            )
    else:
        mcq_ids = [row[0] for row in db.query(MultipleChoiceQuestionList.id).all()]
        if not mcq_ids:
            return JSONResponse(
                {"error": "No multiple choice questions in database"}, status_code=400
            )
        mcq_id = random.choice(mcq_ids)

    if db.get(MultipleChoiceQuestionList, mcq_id) is None:
        return JSONResponse(
            {"error": f"Unknown multiple choice question id: {mcq_id}"}, status_code=404
        )

    try:
        payload = AdminSheetCreate(
            student_name=form_data.get("student_name", ""),
            parent_name=form_data.get("parent_name", ""),
            parent_email=form_data.get("parent_email", ""),
            parent_whatsapp=form_data.get("parent_whatsapp", ""),
            question_list=question_ids,
            multiple_choice_question_id=mcq_id,
            sheet_id=form_data.get("sheet_id") or None,
            assigned_email=form_data.get("assigned_email") or None,
        )
    except ValidationError as exc:
        return JSONResponse({"error": validation_error_message(exc)}, status_code=422)

    sheet_id, id_error = ensure_unique_sheet_id(db, payload.sheet_id)
    if id_error:
        return JSONResponse({"error": id_error}, status_code=409)

    sheet = create_sheet(
        db,
        payload,
        sheet_id=sheet_id,
        assigned_email=payload.assigned_email,
    )

    return RedirectResponse(url=f"/admin/sheet/{sheet.sheet_id}", status_code=303)


# ---------------------------------------------------------------------------
# Read-only JSON API
# ---------------------------------------------------------------------------
def sheet_payload(db: Session, sheet: BingoSheet) -> dict[str, Any]:
    """Plain-JSON view of one sheet, with clue ids and texts resolved."""
    mc_question = db.get(MultipleChoiceQuestionList, sheet.multiple_choice_question_id)
    return {
        "id": sheet.id,
        "sheet_id": sheet.sheet_id,
        "student_name": sheet.student_name,
        "parent_name": sheet.parent_name,
        "parent_email": sheet.parent_email,
        "parent_whatsapp": sheet.parent_whatsapp,
        "assigned_email": sheet.assigned_email,
        "marked_cells": sheet.marked,
        "question_list": normalize_question_ids(sheet.question_list),
        "multiple_choice_question_id": sheet.multiple_choice_question_id,
        "clues": ordered_clue_texts(db, sheet),
        "free_space_mcq": (
            None
            if mc_question is None
            else {
                "id": mc_question.id,
                "question": mc_question.question,
                "options": list(mc_question.options or []),
                "answer_key": mc_question.answer_key,
            }
        ),
    }


@app.get("/api/bingo-questions")
def api_bingo_questions(db: Session = Depends(get_db)):
    """Every stored clue, as JSON."""
    rows = db.query(BingoQuestionList).order_by(BingoQuestionList.id).all()
    return [BingoQuestionListSchema.model_validate(row) for row in rows]


@app.get("/api/mc-questions")
def api_mc_questions(request: Request, db: Session = Depends(get_db)):
    """Return the question bank and answer keys to an administrator."""
    _, failure = api_admin_guard(request, db)
    if failure:
        return failure
    rows = db.query(MultipleChoiceQuestionList).order_by(MultipleChoiceQuestionList.id).all()
    return [MultipleChoiceQuestionListSchema.model_validate(row) for row in rows]


@app.get("/api/sheets")
def api_sheets(request: Request, db: Session = Depends(get_db)):
    """Every stored bingo sheet, as JSON (admin only)."""
    user, failure = api_admin_guard(request, db)
    if failure:
        return failure
    rows = db.query(BingoSheet).order_by(BingoSheet.id).all()
    return [sheet_payload(db, sheet) for sheet in rows]


@app.get("/api/sheets/{identifier}")
def api_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    """One stored bingo sheet, as JSON (admin only)."""
    user, failure = api_admin_guard(request, db)
    if failure:
        return failure
    sheet = find_sheet(db, identifier)
    if sheet is None:
        return JSONResponse({"error": "Sheet not found"}, status_code=404)
    return sheet_payload(db, sheet)


@app.get("/api/export")
def api_export(request: Request, db: Session = Depends(get_db)):
    """All three tables in one .json file, sent as a download (admin only)."""
    user, failure = api_admin_guard(request, db)
    if failure:
        return failure

    content = json.dumps(
        {
            "bingo_question_list": [
                {"id": row.id, "text": row.text}
                for row in db.query(BingoQuestionList).order_by(BingoQuestionList.id).all()
            ],
            "multiple_choice_question_list": [
                {
                    "id": row.id,
                    "question": row.question,
                    "options": list(row.options or []),
                    "answer_key": row.answer_key,
                }
                for row in db.query(MultipleChoiceQuestionList)
                .order_by(MultipleChoiceQuestionList.id)
                .all()
            ],
            "bingo_sheet": [
                sheet_payload(db, sheet)
                for sheet in db.query(BingoSheet).order_by(BingoSheet.id).all()
            ],
        },
        indent=2,
        ensure_ascii=False,
    )
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="carnatic_bingo_data.json"'
        },
    )


# ---------------------------------------------------------------------------
# Answer verification (global content, no account data)
# ---------------------------------------------------------------------------
@app.post("/verify-answer")
async def verify_answer(request: Request, db: Session = Depends(get_db)):
    """Verify a selected MCQ answer for an administrator.

    Accepts either a JSON body ({"mcqId": 1, "selectedAnswer": 2}) or a form post
    (mcq_id / selected_answer). selectedAnswer may be a 0-based option index, a
    letter (A-D), or the option text. The dashboard uses the sheet-scoped
    ``/dashboard/sheet/{id}/verify`` route, which also saves progress.
    """
    _, failure = api_admin_guard(request, db)
    if failure:
        return failure

    try:
        payload = await request_payload(request)
    except Exception:
        return JSONResponse({"correct": False, "error": "Invalid request body"}, status_code=400)

    mcq_raw = payload.get("mcq_id", payload.get("mcqId", payload.get("multiple_choice_question_id")))
    selected_raw = payload.get(
        "selected_answer",
        payload.get("selectedAnswer", payload.get("option", payload.get("optionIndex"))),
    )

    try:
        mcq_id = int(str(mcq_raw).strip())
    except (TypeError, ValueError):
        return JSONResponse({"correct": False, "error": "mcq_id is required"}, status_code=400)

    mc_question = db.get(MultipleChoiceQuestionList, mcq_id)
    if mc_question is None:
        return JSONResponse({"correct": False, "error": "MCQ not found"}, status_code=404)

    result, error = verify_selection(mc_question, selected_raw)
    if error:
        status_code = 500 if "answer key" in error else 400
        return JSONResponse({"correct": False, "error": error}, status_code=status_code)

    return JSONResponse(result)
