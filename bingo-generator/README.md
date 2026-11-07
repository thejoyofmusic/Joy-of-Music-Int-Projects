# Carnatic Bingo Sheet Generator

Automates printable Carnatic concert bingo sheets for The Joy of Music.

## Invoke in Cursor / Claude

Project skill: **`.cursor/skills/bingo-generator/`** (mirrored in `.claude/skills/bingo-generator/`).

```text
/bingo-generator
```

Or say: “make me a new bingo sheet”.

## Developer Quick start

Requires **Python 3.10+**.

If you want to run this manually you can use the following commands:

```bash
cd bingo-generator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python carnatic_bingo_generator.py
```

Layout and static content come from `input_reference_bingo_sheet.docx` (edit in Word for formatting). Clues come from `input_bingo_questions.docx`; MCQs from `input_multiple_choice_Q&A.docx`.

### Web app (FastAPI)

```bash
cd bingo-generator
python3 -m pip install -r requirements.txt
cp .env.example .env         # then add your Google OAuth credentials + admin email
python3 -m app.setup_db      # apply migrations + seed clues/MCQs into bingo_sheets.db
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

- Data lives in SQLite (`bingo_sheets.db`); override with `BINGO_DATABASE_URL`.
- Models: `app/models.py`, schemas: `app/schemas.py`, migrations: `app/migrations.py`.
- `GET /sheet/{sheet_id}` shows a board and `GET /sheet/{sheet_id}/download` returns the printable `.docx` filled from the reference template.

### Sign-in, roles and dashboards

Google OAuth 2.0 (authorization-code flow) decides what a visitor can see. Create
a **Web application** OAuth client in the Google Cloud console, add
`http://127.0.0.1:8000/auth/callback` as an authorised redirect URI, and put the
credentials in `.env` (see `.env.example`).

The quickest route is the built-in setup page: open
`http://127.0.0.1:8000/setup`, follow the two steps it lists (it shows the exact
redirect URI to paste into the console), then paste the Client ID and secret and
save. It writes them to `.env` with owner-only permissions and enables sign-in
immediately — no restart. Like the local sign-in form, `/setup` exists only for a
loopback `APP_BASE_URL`; on a deployed `https` site you edit `.env` on the host.

| Variable | Purpose |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth client credentials |
| `GOOGLE_REDIRECT_URI` | Must match the console entry; defaults to `{APP_BASE_URL}/auth/callback` |
| `APP_BASE_URL` | Public origin; an `https://` value also enables the cookie `Secure` flag |
| `ADMIN_EMAILS` | Comma-separated accounts promoted to `admin` on sign-in |
| `SESSION_SECRET` | Signs the session cookie (required in production) |
| `SESSION_COOKIE_SECURE` | Optional override of the cookie `Secure` flag |

Routes: `GET /auth/login`, `GET /auth/callback`, `GET|POST /auth/logout`. The
session is a signed, HTTP-only, SameSite=Lax cookie holding only the email; the
role is re-read from the `app_user` table on each request.

**Before Google is configured** the sign-in page offers a local form
(`POST /auth/local-login`). It is offered only when `APP_BASE_URL` is a plain-http
loopback address and the request comes from this machine; the first account it
creates becomes the administrator, so a fresh checkout is usable immediately.
Set `LOCAL_LOGIN_ENABLED=false` to require Google even locally. An `https://`
`APP_BASE_URL` (any real deployment) turns it off automatically.

If the address you signed in with has no sheets assigned and no admin role, the
error page offers **“Make this account an administrator”** (`POST /auth/promote`).
That is how the owner claims the admin console on a local install when the first
account went to a mistyped address; it is local-only for the same reasons as the
local sign-in form.

After sign-in:

- **Admin** (`ADMIN_EMAILS` or a stored admin role) → `/admin`: search sheets by
  sheet ID, create a sheet with an optional custom ID and assigned Gmail address,
  assign or move existing sheets, and open a sheet to print or download `.docx`.
- **Valid user** (at least one sheet assigned to their email) → `/dashboard`: the
  assigned 5x5 boards, tappable tiles with saved progress, and the C3 MCQ that
  unlocks the free space when answered correctly.
- **Anyone else** → an error page: *“You are not a valid user in the system.
  Please contact an administrator for access.”*

Sheet pages (`/sheet/...`), the `.docx` download and the sheet JSON endpoints
require a session and allow only the assigned account or an admin.

#### Troubleshooting sign-in

- **Google: “Access blocked … Error 401: invalid_client”** — the app sent a
  client ID Google does not recognise (a placeholder or a stale value). Check
  `GOOGLE_CLIENT_ID` in `.env` against the OAuth client in the console, then
  restart the server: `.env` is only read at startup.
- **“redirect_uri_mismatch”** — the console's authorised redirect URI must be
  exactly the `GOOGLE_REDIRECT_URI` value, e.g.
  `http://127.0.0.1:8000/auth/callback` (no trailing slash).
- **Sign-in page says “not configured”** — `GOOGLE_CLIENT_ID` or
  `GOOGLE_CLIENT_SECRET` is empty in `.env`.
- **Signed out after every restart** — set a stable `SESSION_SECRET`.

JSON views of global content: `GET /api/bingo-questions`, `GET /api/mc-questions`.
Admin-only: `GET /api/sheets`, `GET /api/sheets/{sheet_id}` and `GET /api/export`
(one `carnatic_bingo_data.json` download with all three tables).

### Reference template (`input_reference_bingo_sheet.docx`)

The generator **copies** this file for every new sheet, then only replaces:

- Sheet number in the title (`Bingo Sheet #N`)
- Clue text in the "I Heard" table (labels stay; text is shuffled)
- MCQ question and four options on page 2

Everything else in the template carries through: fonts, margins, logos, colors, headers, the 6×6 grid layout, and static labels like `★ Answer MCQ ★` in the free space.

**Safe to edit in Word (no code change):** formatting, wording of static text, images, page layout that keeps the same table structure.

**Requires code change:** different table sizes (e.g. not 6×6 grid, not 12×2 "I Heard", not 1×1 MCQ cell), different cell labels, or a different number of clues/MCQs.

Output: `completed-bingo-sheets/Carnatic_Bingo_Sheet_001.docx`, `002.docx`, …

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests use temporary state/output paths and do not modify production `bingo_state.json`.

## State

`bingo_state.json` tracks sheet number, clue permutations, MCQ rotation, and every generated file.

## Specs

See `docs/specs/001-carnatic-bingo-generator/`.
