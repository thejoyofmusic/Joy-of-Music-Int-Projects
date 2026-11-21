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

import csv
import io
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
    BingoPreview,
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
    overrides = sheet.clue_overrides or {}
    return [
        overrides.get(GRID_POSITIONS[index], by_id.get(question_id, ""))
        for index, question_id in enumerate(question_ids)
    ]


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
    available_ids = [row[0] for row in db.query(BingoQuestionList.id).filter(BingoQuestionList.active.is_(True)).all()]
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
    commit: bool = True,
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
    if commit:
        db.commit()
        db.refresh(sheet)
    else:
        db.flush()
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
    created: int = 0,
    saved: int = 0,
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

    previews_by_student = {email: [] for email in known_accounts}
    other_previews = []
    for draft in db.query(BingoPreview).filter_by(
        owner_email=user.email, result_url=None,
    ).order_by(BingoPreview.revision.desc(), BingoPreview.token).all():
        email = normalize_email(draft.items[0].get("assigned_email"))
        draft_fields = (
            str(item.get(field) or "").casefold()
            for item in draft.items
            for field in ("sheet_id", "student_name", "parent_name", "parent_email",
                          "parent_whatsapp", "assigned_email")
        )
        searchable = " ".join(draft_fields)
        if search and email not in matched_accounts and not all(
            term.casefold() in searchable for term in search.split()
        ):
            continue
        if email in known_accounts:
            previews_by_student[email].append(draft)
        else:
            other_previews.append(draft)

    accounts = [
        account for account in all_accounts
        if account.email in matched_accounts or sheets_by_student[account.email] or previews_by_student[account.email]
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
            "created": created,
            "saved": bool(saved),
            "previews_by_student": previews_by_student,
            "other_previews": other_previews,
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

    try:
        count = int(form_data.get("count") or "1")
        if not 1 <= count <= 100:
            raise ValueError
    except ValueError:
        return error_page(request, "Choose between 1 and 100 sheets.", 422, user=user)
    if count > 1 and form_data.get("sheet_id"):
        return error_page(request, "Custom sheet IDs can only be used for one sheet.", 422, user=user)

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

    items = []
    reserved_ids = set()
    for index in range(count):
        if index:
            question_ids, mcq_id, _ = random_board(db)
            payload = payload.model_copy(update={
                "question_list": question_ids, "multiple_choice_question_id": mcq_id,
            })
            sheet_id, id_error = ensure_unique_sheet_id(db, None)
            if id_error:
                return error_page(request, id_error, 409, user=user)
        while sheet_id in reserved_ids:
            sheet_id = new_sheet_id()
        reserved_ids.add(sheet_id)
        items.append({
            **payload.model_dump(), "sheet_id": sheet_id, "clue_overrides": {},
            "student_id": account.id if student_email else None,
        })
    return start_bingo_preview(db, user, items)


def start_bingo_preview(db: Session, user: User, items: list[dict[str, Any]]) -> Response:
    preview = BingoPreview(
        token=secrets.token_urlsafe(24), owner_email=user.email, items=items, revision=0,
    )
    db.add(preview)
    db.commit()
    return RedirectResponse(f"/admin/previews/{preview.token}", status_code=303)


def find_bingo_preview(db: Session, token: str, user: User) -> Optional[BingoPreview]:
    return db.query(BingoPreview).filter_by(token=token, owner_email=user.email).first()


@app.get("/admin/previews/{token}")
def bingo_preview_page(token: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    preview = find_bingo_preview(db, token, user)
    if preview is None:
        return error_page(request, "Preview not found.", 404, user=user)
    if preview.result_url:
        return RedirectResponse(preview.result_url, status_code=303)
    boards = []
    for item in preview.items:
        sheet = BingoSheet(**{key: value for key, value in item.items() if key != "student_id"}, marked_cells=[], mcq_solved=False)
        boards.append(sheet_view(db, sheet))
    return templates.TemplateResponse(request, "bingo_preview.html", {
        "user": user, "preview": preview, "boards": boards,
        "mcqs": db.query(MultipleChoiceQuestionList).order_by(MultipleChoiceQuestionList.id).all(),
    })


@app.post("/admin/previews/{token}")
async def update_bingo_preview(token: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    preview = find_bingo_preview(db, token, user)
    if preview is None:
        return error_page(request, "Preview not found.", 404, user=user)
    if preview.result_url:
        return RedirectResponse(preview.result_url, status_code=303)
    form = await request.form(max_fields=4000)
    try:
        revision = int(str(form.get("revision")))
    except (ValueError, TypeError):
        return error_page(request, "Reload the preview and try again.", 409, user=user)
    if preview.revision != revision:
        return error_page(request, "This preview changed. Reload it before continuing.", 409, user=user)
    action = str(form.get("action") or "")
    if action not in {"save", "save_exit", "confirm", "cancel"} and not action.startswith("reshuffle:"):
        return error_page(request, "Choose a preview action.", 422, user=user)

    items = []
    for index, original in enumerate(preview.items):
        item = {**original}
        sheet = BingoSheet(**{key: value for key, value in item.items() if key != "student_id"}, marked_cells=[], mcq_solved=False)
        current_clues = sheet_view(db, sheet)["clues_by_position"]
        clues = {
            position: str(form.get(f"clue_{index}_{position}", text)).strip()
            for position, text in current_clues.items()
        }
        if action != "cancel" and any(not text or len(text) > 500 for text in clues.values()):
            return error_page(request, "Each clue needs between 1 and 500 characters.", 422, user=user)
        try:
            mcq_id = int(str(form.get(f"mcq_{index}", item["multiple_choice_question_id"])))
        except ValueError:
            return error_page(request, "Choose a C3 question.", 422, user=user)
        if db.get(MultipleChoiceQuestionList, mcq_id) is None:
            return error_page(request, "C3 question not found.", 422, user=user)
        item["clue_overrides"] = clues
        item["multiple_choice_question_id"] = mcq_id
        items.append(item)

    if action.startswith("reshuffle:"):
        try:
            index = int(action.split(":", 1)[1])
            if not 0 <= index < len(items):
                raise ValueError
        except ValueError:
            return error_page(request, "Sheet not found in this preview.", 422, user=user)
        question_ids, mcq_id, error = random_board(db)
        if error:
            return error_page(request, error, 400, user=user)
        items[index].update({
            "question_list": question_ids, "multiple_choice_question_id": mcq_id,
            "clue_overrides": {},
        })

    # Claim the revision before creating sheets so retries and double clicks cannot assign twice.
    updated = db.query(BingoPreview).filter_by(
        token=token, owner_email=user.email, revision=revision, result_url=None,
    ).update({BingoPreview.revision: revision + 1, BingoPreview.items: items})
    if not updated:
        db.rollback()
        return error_page(request, "This preview changed. Reload it before continuing.", 409, user=user)
    if action == "confirm":
        for item in items:
            email = item.get("assigned_email")
            if email:
                account = db.query(User).filter_by(email=email).first()
                if item.get("student_id") and (
                    account is None or account.id != item["student_id"] or account.is_admin
                ):
                    db.rollback()
                    return error_page(request, "The student is no longer available. Start a new preview.", 409, user=user)
                if item.get("student_id"):
                    item.update({
                        "student_name": account.student_name or email.split("@")[0][:120],
                        "parent_name": account.parent_name, "parent_email": account.parent_email,
                        "parent_whatsapp": account.parent_whatsapp,
                    })
            sheet_id, error = ensure_unique_sheet_id(db, item["sheet_id"])
            if error:
                db.rollback()
                return error_page(request, error, 409, user=user)
            try:
                payload = AdminSheetCreate.model_validate(item)
            except ValidationError as exc:
                db.rollback()
                return error_page(request, validation_error_message(exc), 422, user=user)
            sheet = create_sheet(
                db, payload, sheet_id=sheet_id, assigned_email=email, commit=False,
            )
            sheet.clue_overrides = item["clue_overrides"]
        preview.result_url = (
            f"/admin/sheet/{sheet.sheet_id}" if len(items) == 1 else f"/admin?created={len(items)}"
        )
    elif action == "cancel":
        preview.result_url = "/admin"
    db.commit()
    if action == "save_exit":
        return RedirectResponse("/admin?saved=1", status_code=303)
    return RedirectResponse(preview.result_url or f"/admin/previews/{token}", status_code=303)


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


@app.post("/admin/sheet/{identifier}/duplicate")
def duplicate_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    source = find_sheet(db, identifier)
    if source is None:
        return error_page(request, "Sheet not found.", 404, user=user)
    sheet_id, error = ensure_unique_sheet_id(db, None)
    if error:
        return error_page(request, error, 409, user=user)
    item = {
        "sheet_id": sheet_id, "student_name": source.student_name,
        "parent_name": source.parent_name, "parent_email": source.parent_email,
        "parent_whatsapp": source.parent_whatsapp, "assigned_email": source.assigned_email,
        "question_list": list(normalize_question_ids(source.question_list)),
        "multiple_choice_question_id": source.multiple_choice_question_id,
        "clue_overrides": dict(source.clue_overrides or {}),
    }
    return start_bingo_preview(db, user, [item])


@app.get("/admin/sheet/{identifier}/edit")
def edit_sheet_page(identifier: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    sheet = find_sheet(db, identifier)
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)
    context = sheet_view(db, sheet)
    context.update({
        "user": user,
        "mcqs": db.query(MultipleChoiceQuestionList).order_by(MultipleChoiceQuestionList.id).all(),
    })
    return templates.TemplateResponse(request, "edit_sheet.html", context)


@app.post("/admin/sheet/{identifier}/edit")
async def edit_sheet(identifier: str, request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    sheet = find_sheet(db, identifier)
    if sheet is None:
        return error_page(request, "Sheet not found.", 404, user=user)
    form = await request.form()
    clues = {position: str(form.get(position) or "").strip() for position in GRID_POSITIONS}
    if any(not text or len(text) > 500 for text in clues.values()):
        return error_page(request, "Each clue needs between 1 and 500 characters.", 422, user=user)
    try:
        mcq_id = int(str(form.get("mcq_id") or ""))
    except ValueError:
        return error_page(request, "Choose a multiple-choice question.", 422, user=user)
    if db.get(MultipleChoiceQuestionList, mcq_id) is None:
        return error_page(request, "Multiple-choice question not found.", 422, user=user)
    current = sheet_view(db, sheet)["clues_by_position"]
    changed = {position for position in GRID_POSITIONS if clues[position] != current[position]}
    if mcq_id != sheet.multiple_choice_question_id:
        sheet.mcq_solved = False
        sheet.mcq_attempts = 0
        sheet.mcq_incorrect_attempts = 0
        sheet.mcq_wrong_choices = 0
        changed.add(MCQ_POSITION)
    sheet.marked_cells = [position for position in sheet.marked if position not in changed]
    sheet.multiple_choice_question_id = mcq_id
    sheet.clue_overrides = clues
    db.commit()
    return RedirectResponse(f"/admin/sheet/{sheet.sheet_id}", status_code=303)


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
        sheet.mcq_wrong_choices = 0
        sheet.mcq_attempts = 0
        sheet.mcq_solved = False
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
    if account is not None and (account.registered_student or account.is_admin):
        return error_page(
            request, "This email already belongs to a student or administrator.", 409,
            detail="Find the student in the list and use Edit details.", user=user,
        )
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


@app.get("/admin/students/import-template")
def student_import_template(request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    return Response(
        "email,student_name,parent_name,parent_email,parent_whatsapp\r\n",
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="students-template.csv"'},
    )


@app.post("/admin/students/import")
async def import_students(request: Request, db: Session = Depends(get_db)):
    user, failure = admin_guard(request, db)
    if failure:
        return failure
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return error_page(request, "Choose a CSV file.", 422, user=user)
    content = await upload.read(2_000_001)
    if len(content) > 2_000_000:
        return error_page(request, "Choose a CSV smaller than 2 MB.", 422, user=user)
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        if not reader.fieldnames or "email" not in reader.fieldnames:
            raise ValueError("The CSV must have an email column.")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("The CSV has repeated column headers.")
        rows = []
        duplicates = []
        errors = []
        seen = set()
        for row_number, row in enumerate(reader, start=2):
            if row_number > 1001:
                raise ValueError("Import at most 1,000 students at a time.")
            email = normalize_email(row.get("email") or "")
            if not auth.EMAIL_PATTERN.fullmatch(email) or None in row:
                errors.append(f"Row {row_number}: enter a valid email and check the columns.")
                continue
            try:
                details = StudentDetails.model_validate({
                    key: value or "" for key, value in row.items()
                    if key in StudentDetails.model_fields
                })
            except ValidationError as exc:
                errors.append(f"Row {row_number}: {validation_error_message(exc)}")
                continue
            existing = db.query(User).filter(func.lower(User.email) == email).first()
            if email in seen or (existing and (existing.registered_student or existing.is_admin)):
                duplicates.append(f"Row {row_number}: {email}")
                continue
            seen.add(email)
            rows.append((email, details, existing))
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        return error_page(request, f"Could not import CSV: {exc}", 422, user=user)
    if not errors:
        for email, details, account in rows:
            if account is None:
                account = User(email=email, role=auth.role_for_email(email))
                db.add(account)
            account.registered_student = True
            update_student_details(db, account, details.model_dump())
        db.commit()
    return templates.TemplateResponse(request, "import_students.html", {
        "user": user, "imported": len(rows) if not errors else 0,
        "duplicates": duplicates, "errors": errors,
    }, status_code=422 if errors else 200)


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

    marked = [position for position in sheet.marked if position != MCQ_POSITION]
    if "cells" in payload:
        raw_cells = payload["cells"]
        if not isinstance(raw_cells, (list, tuple)):
            return JSONResponse(
                {"error": "cells must be a list of board positions"}, status_code=400
            )
        marked = [position for position in normalize_marked_cells(raw_cells)
                  if position != MCQ_POSITION]
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

    if sheet.mcq_solved:
        return JSONResponse({"correct": True, "marked": sheet.marked, "already_solved": True})

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
    unsolved_sheet = db.query(BingoSheet).filter_by(id=sheet.id, mcq_solved=False)
    if correct:
        counters[BingoSheet.mcq_solved] = True
    else:
        selected = selected_option_index(selected_raw, list(mc_question.options))
        if selected is None or selected >= len(OPTION_LETTERS):
            return JSONResponse({"correct": False, "error": "Invalid answer choice."}, status_code=400)
        choice_bit = 1 << selected
        unsolved_sheet = unsolved_sheet.filter(
            BingoSheet.mcq_wrong_choices.bitwise_and(choice_bit) == 0
        )
        counters[BingoSheet.mcq_wrong_choices] = BingoSheet.mcq_wrong_choices.bitwise_or(choice_bit)
        counters[BingoSheet.mcq_incorrect_attempts] = BingoSheet.mcq_incorrect_attempts + 1

    updated = unsolved_sheet.update(counters)
    db.commit()
    if not updated:
        if sheet.mcq_solved:
            return JSONResponse({"correct": True, "marked": sheet.marked, "already_solved": True})
        return JSONResponse({"correct": False, "marked": sheet.marked, "already_attempted": True})
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
        available_ids = [row[0] for row in db.query(BingoQuestionList.id).filter(BingoQuestionList.active.is_(True)).all()]
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
        "clue_overrides": dict(sheet.clue_overrides or {}),
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
    rows = db.query(BingoQuestionList).filter(BingoQuestionList.active.is_(True)).order_by(BingoQuestionList.id).all()
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
                {"id": row.id, "text": row.text, "active": row.active}
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
