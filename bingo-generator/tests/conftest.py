"""Pytest fixtures — isolated state/output; never touch production paths in tests.

Environment for the web app is configured here, before any test module imports
``app.database`` (which builds the engine at import time).
"""

import os
import tempfile
from pathlib import Path

import pytest

BINGO_ROOT = Path(__file__).resolve().parent.parent

_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="bingo-test-db-"))
os.environ.setdefault("BINGO_DATABASE_URL", f"sqlite:///{_TEST_DB_DIR / 'test.db'}")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://testserver/auth/callback")
os.environ.setdefault("ADMIN_EMAILS", "admin@example.com")
# Session cookies must work over the TestClient's http:// base URL.
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")

CONCERT_DOCX = BINGO_ROOT / "input_bingo_questions.docx"
MCQ_DOCX = BINGO_ROOT / "input_multiple_choice_Q&A.docx"


@pytest.fixture
def bingo_root():
    return BINGO_ROOT


@pytest.fixture
def tmp_state_file(tmp_path):
    return str(tmp_path / "bingo_state.json")


@pytest.fixture
def tmp_output_dir(tmp_path):
    out = tmp_path / "completed-bingo-sheets"
    out.mkdir()
    return str(out)


@pytest.fixture
def env_isolated(monkeypatch, tmp_state_file, tmp_output_dir):
    """Point generator at temp state and output for integration tests."""
    monkeypatch.setenv("BINGO_STATE_FILE", tmp_state_file)
    monkeypatch.setenv("BINGO_OUTPUT_DIR", tmp_output_dir)
    monkeypatch.setenv("BINGO_ROOT", str(BINGO_ROOT))


@pytest.fixture
def production_state_mtime(bingo_root):
    """Snapshot production state mtime so T020 can assert tests did not touch it."""
    path = bingo_root / "bingo_state.json"
    if path.exists():
        return path.stat().st_mtime
    return None
