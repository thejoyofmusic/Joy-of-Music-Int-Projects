"""Sign a test client in without going through Google.

The app reads the signed-in email from Starlette's signed session cookie, so the
tests mint that cookie directly. The signature scheme matches
``SessionMiddleware`` exactly: a ``TimestampSigner`` over the base64 JSON payload
(Starlette 0.4x uses no salt).
"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, urlparse

from itsdangerous import TimestampSigner

from app.auth import SESSION_COOKIE_NAME, upsert_user
from app.database import SessionLocal

def session_cookie_value(email: str) -> str:
    """A valid ``bingo_session`` cookie value for ``email``."""
    secret = os.environ["SESSION_SECRET"]
    payload = base64.b64encode(json.dumps({"email": email}).encode("utf-8"))
    return TimestampSigner(secret).sign(payload).decode("utf-8")


def login_as(client, email: str) -> None:
    """Create the account row (mirroring first sign-in) and set the cookie."""
    with SessionLocal() as db:
        upsert_user(db, email)
    # Clear first so a cookie left by an earlier identity (or by a response
    # Set-Cookie) is replaced instead of being sent alongside it.
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, session_cookie_value(email))


def start_google_login(client) -> str:
    """Hit /auth/login and return the ``state`` Google would echo back."""
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/")
    return parse_qs(urlparse(location).query)["state"][0]
