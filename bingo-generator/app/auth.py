"""Google OAuth 2.0 sign-in, roles and session helpers.

Configuration comes from the environment (``.env`` in the project root is loaded
on import, without overriding variables that are already set):

``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``
    OAuth client credentials from https://console.cloud.google.com/apis/credentials
``GOOGLE_REDIRECT_URI``
    Must match the authorised redirect URI exactly. Defaults to
    ``{APP_BASE_URL}/auth/callback``.
``APP_BASE_URL``
    Public base URL of the app; also decides whether session cookies get the
    ``Secure`` flag (any ``https://`` base URL does).
``ADMIN_EMAILS``
    Comma/semicolon separated addresses that are promoted to ``admin`` on sign-in.
``SESSION_SECRET``
    Signing key for the session cookie. Generated per-process when unset, which
    logs everyone out on restart, so set it in any real deployment.
``SESSION_COOKIE_SECURE``
    Optional explicit override of the cookie ``Secure`` flag (true/false).

The session itself is a signed, HTTP-only, SameSite=Lax cookie handled by
Starlette's ``SessionMiddleware``; only the signed-in email is stored in it, and
the role is re-read from the database on every request.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import PROJECT_ROOT
from app.models import (
    ROLE_ADMIN,
    ROLE_USER,
    BingoSheet,
    User,
    normalize_email,
)

logger = logging.getLogger(__name__)


def env_file_path() -> Path:
    """The .env file the app reads and writes (override with ``BINGO_ENV_FILE``)."""
    override = os.environ.get("BINGO_ENV_FILE", "").strip()
    return Path(override) if override else PROJECT_ROOT / ".env"


# Real environment variables win over .env values.
load_dotenv(env_file_path(), override=False)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

SESSION_COOKIE_NAME = "bingo_session"
SESSION_EMAIL_KEY = "email"
SESSION_STATE_KEY = "oauth_state"
SESSION_NEXT_KEY = "next"

SESSION_MAX_AGE = 60 * 60 * 24 * 14  # two weeks
TRUTHY = {"1", "true", "yes", "on"}

# Hosts that mean "this computer". ``testclient`` is Starlette's in-process client.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "testclient"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_FALLBACK_SECRET = secrets.token_urlsafe(32)


class OAuthError(RuntimeError):
    """Raised when Google sign-in cannot be completed."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def google_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def oauth_configured() -> bool:
    """True when both Google credentials are present."""
    return bool(google_client_id() and google_client_secret())


def app_base_url() -> str:
    return os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")


def callback_url() -> str:
    """Registered redirect URI; must match the Google console entry exactly."""
    explicit = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    return explicit or f"{app_base_url()}/auth/callback"


def session_secret() -> str:
    configured = os.environ.get("SESSION_SECRET", "").strip()
    return configured or _FALLBACK_SECRET


def session_secret_is_ephemeral() -> bool:
    return not os.environ.get("SESSION_SECRET", "").strip()


def secure_cookies() -> bool:
    """Send the session cookie over HTTPS only.

    Defaults to True for an https deployment and False for local http
    development, so sign-in works out of the box on localhost. Override with
    ``SESSION_COOKIE_SECURE``.
    """
    override = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower()
    if override:
        return override in TRUTHY
    return app_base_url().startswith("https://")


def admin_emails() -> set[str]:
    """Addresses from ``ADMIN_EMAILS`` that are always treated as admins."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    parts = raw.replace(";", ",").replace("\n", ",").split(",")
    return {normalize_email(part) for part in parts if part.strip()}


def role_for_email(email: str) -> str:
    return ROLE_ADMIN if normalize_email(email) in admin_emails() else ROLE_USER


# ---------------------------------------------------------------------------
# Configuration written from the browser setup page
# ---------------------------------------------------------------------------
def looks_like_google_client_id(value: str) -> bool:
    """Google web client IDs look like ``1234-abc.apps.googleusercontent.com``."""
    text = str(value or "").strip()
    return (
        text.endswith(".apps.googleusercontent.com")
        and " " not in text
        and "@" not in text
        and len(text) > len(".apps.googleusercontent.com")
    )


def save_settings(values: Dict[str, str]) -> Path:
    """Write settings into the .env file, then apply them without a restart.

    Existing keys are replaced in place so comments and ordering survive; new keys
    are appended. The file is created with owner-only permissions because it holds
    the OAuth client secret.
    """
    path = env_file_path()
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)

    lines = []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - permission tweaks are best-effort
        pass

    # The running process reads configuration from os.environ on every request.
    os.environ.update(values)
    logger.info("Saved %s to %s", ", ".join(values), path)
    return path


# ---------------------------------------------------------------------------
# Local (no-Google) sign-in
# ---------------------------------------------------------------------------
def looks_like_email(value: Any) -> bool:
    return bool(EMAIL_PATTERN.match(normalize_email(value)))


def local_login_enabled() -> bool:
    """Whether signing in without Google is allowed.

    This is a development convenience for the machine running the app: it is
    available only while ``APP_BASE_URL`` is a plain-http loopback address, and a
    real deployment (``https://...``) disables it automatically. Set
    ``LOCAL_LOGIN_ENABLED=false`` to turn it off even locally.
    """
    parsed = urlparse(app_base_url())
    if parsed.scheme != "http" or (parsed.hostname or "").lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        return False
    override = os.environ.get("LOCAL_LOGIN_ENABLED", "").strip().lower()
    return override in TRUTHY if override else True


def request_is_local(request) -> bool:
    """True when the request actually came from this machine."""
    host = (getattr(getattr(request, "client", None), "host", "") or "").strip().lower()
    return host in LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# Users and authorization
# ---------------------------------------------------------------------------
def upsert_user(db: Session, email: str) -> User:
    """Create or refresh the account for a Google email.

    ``ADMIN_EMAILS`` promotes an account to admin; an admin is never demoted just
    because the variable changed, so access is only lost deliberately.
    """
    canonical = normalize_email(email)
    if not canonical:
        raise OAuthError("Google did not return an email address")

    user = db.query(User).filter(User.email == canonical).first()
    wanted_role = role_for_email(canonical)

    if user is None:
        user = User(email=canonical, role=wanted_role)
        db.add(user)
    elif wanted_role == ROLE_ADMIN and user.role != ROLE_ADMIN:
        user.role = ROLE_ADMIN

    db.commit()
    db.refresh(user)
    return user


def current_user(request, db: Session) -> Optional[User]:
    """The signed-in account, or None when the session is empty/stale."""
    email = normalize_email(request.session.get(SESSION_EMAIL_KEY))
    if not email:
        return None
    return db.query(User).filter(User.email == email).first()


def admin_exists(db: Session) -> bool:
    return bool(db.query(func.count(User.id)).filter(User.role == ROLE_ADMIN).scalar())


def upsert_local_user(db: Session, email: str) -> User:
    """Create/refresh an account without Google.

    If the database has no administrator yet, the first local account claims the
    role so a fresh install is usable without waiting for ``ADMIN_EMAILS``.
    """
    user = upsert_user(db, email)
    if user.role != ROLE_ADMIN and not admin_exists(db):
        logger.warning("No administrator exists; promoting local account %s", user.email)
        user.role = ROLE_ADMIN
        db.commit()
        db.refresh(user)
    return user


def promote_to_admin(db: Session, user: User) -> User:
    """Give an already signed-in account the admin role (local development only)."""
    if user.role != ROLE_ADMIN:
        user.role = ROLE_ADMIN
        db.commit()
        db.refresh(user)
    return user


def can_self_promote(request, user: Optional[User]) -> bool:
    """Whether to offer the local "make me an administrator" action.

    Only ever true for a signed-in account on a loopback development install, so
    a real deployment (https) never exposes it.
    """
    return bool(user) and local_login_enabled() and request_is_local(request)


def assigned_sheet_count(db: Session, email: str) -> int:
    canonical = normalize_email(email)
    if not canonical:
        return 0
    return (
        db.query(func.count(BingoSheet.id))
        .filter(func.lower(BingoSheet.assigned_email) == canonical)
        .scalar()
        or 0
    )


def can_access_sheet(user: User, sheet: BingoSheet) -> bool:
    """Admins see every sheet; everyone else only their own assignment."""
    if user.is_admin:
        return True
    return normalize_email(sheet.assigned_email) == normalize_email(user.email)


def landing_url(db: Session, user: User) -> Optional[str]:
    """Where a freshly signed-in account belongs, or None when unauthorized."""
    if user.is_admin:
        return "/admin"
    if user.registered_student or assigned_sheet_count(db, user.email) > 0:
        return "/dashboard"
    return None


def sign_in(request, user: User) -> None:
    request.session[SESSION_EMAIL_KEY] = normalize_email(user.email)


def sign_out(request) -> None:
    request.session.clear()


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------
def authorization_url(state: str) -> str:
    """The Google consent-screen URL for the authorization-code flow."""
    params = {
        "client_id": google_client_id(),
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def _decode_id_token(id_token: str) -> Dict[str, Any]:
    """Read the claims of a token that Google just handed us over TLS.

    The token comes straight from Google's token endpoint, so it is trusted
    without a JWKS signature check; issuer, audience and expiry are still
    verified, and the userinfo endpoint is preferred when available.
    """
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (IndexError, ValueError, TypeError) as exc:
        raise OAuthError("Google returned an unreadable ID token") from exc

    if not isinstance(claims, dict):
        raise OAuthError("Google returned an unreadable ID token")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise OAuthError("Google ID token has an unexpected issuer")
    audience = claims.get("aud")
    client_id = google_client_id()
    if client_id and audience not in (client_id, [client_id]):
        raise OAuthError("Google ID token was issued for another application")
    expires = claims.get("exp")
    if isinstance(expires, (int, float)) and expires < time.time():
        raise OAuthError("Google ID token has expired")
    return claims


def _verified_email(info: Dict[str, Any]) -> Optional[str]:
    email = normalize_email(info.get("email"))
    if not email:
        return None
    verified = info.get("email_verified")
    if verified is False or str(verified).strip().lower() == "false":
        return None
    return email


def exchange_code_for_email(code: str) -> str:
    """Trade an authorization code for the signed-in Google email address."""
    client_id = google_client_id()
    client_secret = google_client_secret()
    if not (client_id and client_secret):
        raise OAuthError("Google OAuth credentials are not configured")

    try:
        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback_url(),
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=15.0,
        )
        response.raise_for_status()
        token = response.json()
    except httpx.HTTPError as exc:
        raise OAuthError(f"Google token exchange failed: {exc}") from exc
    except ValueError as exc:
        raise OAuthError("Google returned an unreadable token response") from exc

    if not isinstance(token, dict):
        raise OAuthError("Google returned an unreadable token response")

    access_token = token.get("access_token")
    if access_token:
        try:
            profile = httpx.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15.0,
            )
            profile.raise_for_status()
            info = profile.json()
        except (httpx.HTTPError, ValueError):
            info = {}
        if isinstance(info, dict):
            email = _verified_email(info)
            if email:
                return email

    id_token = token.get("id_token")
    if isinstance(id_token, str) and id_token:
        email = _verified_email(_decode_id_token(id_token))
        if email:
            return email

    raise OAuthError("Google did not return a verified email address")
