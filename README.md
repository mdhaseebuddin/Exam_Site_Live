# Exam Platform

Flask + SQLAlchemy (SQLite) online examination platform with email-based OTP
password reset, student session OTPs, math CAPTCHA, server-side PDF reports,
and a production-grade **Anti-Cheating & Browser-Lockdown System** featuring
webcam proctoring, a server-authoritative violation counter, and enforced
fullscreen exam lockdown.

## Repository layout

```
.
├── Procfile                # Cloud start command (points into production_build)
├── .gitignore              # Excludes secrets, DBs, logs & local runtime data
├── README.md               # This file
└── production_build/       # Self-contained, deployable application package
    ├── app.py              # Flask app (routes, business logic, config)
    ├── models.py           # SQLAlchemy models
    ├── schemas.py          # Marshmallow input-validation schemas
    ├── wsgi.py             # WSGI entrypoint (calls init_db() on boot)
    ├── requirements.txt    # Pinned Python dependencies
    ├── .env.example        # Environment-variable template
    ├── .gitignore          # Folder-level ignore rules
    ├── templates/          # Jinja2 templates
    ├── static/             # CSS / JS / images
    └── instance/           # SQLite DB (created at runtime; git-ignored)
```

`production_build/` is a **self-contained package** — every file the app needs
to run lives inside it, and deployment is driven from the repo root via the
`Procfile`.

## Anti-Cheating & Browser-Lockdown System

Every exam attempt runs inside a locked-down proctoring environment:

- **Fullscreen enforcement with click recapture & `Esc` mitigation** — the exam
  page requests browser fullscreen on load and re-enters it on **every** click
  or keystroke (capture phase), so `Esc` / `F11` cannot keep the student out.
  Exiting fullscreen is counted as a violation and a blocking "Enter
  Fullscreen" overlay appears until the student clicks back in.
- **Tab-switch, focus-loss, copy/paste & devtools restrictions** —
  `visibilitychange` and `blur` detect tab switches, window minimizes, and
  clicks into other applications; right-click (`contextmenu`), `Ctrl+C/X/V`,
  `F12`, `Ctrl+Shift+I/J/C`, view-source (`Ctrl+U`), save/print (`Ctrl+S/P`),
  and refresh (`F5` / `Ctrl+R`) are all blocked while the exam is active.
- **Smart 3-strike face-absence webcam proctoring** — on every strike a
  `getUserMedia` webcam frame is captured, compressed to a small base64 JPEG
  via the HTML5 Canvas API, uploaded with the incident, and persisted
  server-side on the `ExamViolation` record. The first two strikes show a
  strict warning modal; the **3rd strike automatically submits the exam** and
  redirects the student to the results page.
- **Server-authoritative violation counter & deadline-tamper protection** —
  every incident increments `session.violation_count` server-side in the same
  transaction that writes the `exam_violations` row, so page refreshes and
  tampered clients **cannot reset the tally**; the countdown and submit
  deadline are anchored to absolute UTC server timestamps and enforced on the
  server (any submission past the deadline is rejected).
- **Conditional host session review** — the host's Session Details page renders
  a dedicated **Proctoring Audit** card (strike count, chronological
  timestamps, and thumbnail previews of the proof snapshots) **only** when the
  attempt reached the 3-strike threshold and was auto-submitted. Normal
  completions with 0, 1, or 2 warnings stay clean and uncluttered.

The policy is tunable with environment variables: `MAX_VIOLATIONS` (default
`3`) and `SNAPSHOT_MAX_BYTES` (default `480000`).

## Configuration

All configuration comes from environment variables, loaded at startup with
`python-dotenv` (`load_dotenv()` in `app.py`). See
`production_build/.env.example` for the full list. The variables required in
production are:

| Variable      | Purpose                                                |
|---------------|--------------------------------------------------------|
| `SECRET_KEY`  | Flask session signing (fail-fast if missing)           |
| `FLASK_ENV`   | `production`                                           |
| `SMTP_HOST`   | Gmail SMTP server (`smtp.gmail.com`)                   |
| `SMTP_PORT`   | `587`                                                  |
| `SMTP_USER`   | Gmail address used for OTP emails                      |
| `SMTP_PASS`   | Gmail **App Password** (not your normal password)      |
| `SMTP_FROM`   | From-address for OTP emails                            |
| `MAIL_DEFAULT_SENDER` | Host OTP sender (verified on the host Brevo key)    |
| `MAIL_STUDENT_SENDER` | Student OTP sender (verified on the student Brevo keys) |
| `MAX_SUBMISSIONS` | Lifetime cap on completed submissions (default 500)           |
| `DAILY_REGISTRATION_LIMIT` | Strict per-host cap on student registrations per 24h (default 70) |
| `DAILY_REGISTRATION_WINDOW_HOURS` | Rolling window (hours) defining a host's "day" (default 24) |
| `MAX_VIOLATIONS` | Security-strike threshold that force-submits a flagged attempt (default 3) |
| `SNAPSHOT_MAX_BYTES` | Max base64 length accepted for a proctoring proof snapshot (default 480000) |

## Deploying to a cloud host (Render / Koyeb)

1. **Push** this repository to GitHub/GitLab and connect it to your host.
2. **Build command:** `pip install -r production_build/requirements.txt`
3. **Start command:** the root `Procfile` runs
   `gunicorn --chdir production_build wsgi:app` (leave the host's "Start
   Command" empty so the `Procfile` `web:` line is used).
4. **Environment:** set the variables from the table above in the host's
   dashboard (no `.env` file is needed on the host).

> **Why `--chdir`, not `production_build.wsgi:app`?**
> The app's modules use plain absolute imports (`from app import app`,
> `from models import ...`, `from schemas import ...`), so gunicorn must run
> *inside* `production_build`. The dotted module form resolves to the folder
> but its internal `from app import ...` then fails with
> `ModuleNotFoundError: No module named 'app'`. `--chdir production_build`
> makes the folder the working directory, so every import resolves exactly as
> the code expects. The app computes its own paths (`DB_PATH`, `LOGS_DIR`)
> relative to `__file__`, not the working directory, so `--chdir` is safe.

## Local development

```bash
cd production_build
python -m venv venv
# Windows:      venv\Scripts\activate
# Linux/macOS:  source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in real values

# Windows:
waitress-serve --listen=0.0.0.0:8000 wsgi:app
# Linux / macOS:
gunicorn --workers 4 --threads 2 --bind 0.0.0.0:8000 wsgi:app
```

> **SQLite on cloud hosts (IMPORTANT):** on free-tier instances the filesystem
> is **ephemeral and is wiped on every redeploy**, so a plain `instance/exam.db`
> resets on each deploy — registered host accounts and student submissions
> disappear and hosts must re-register. To keep data across restarts/redeploys:
> attach a **Render Persistent Disk** and set
> `RENDER_PERSISTENT_DISK_PATH=<mount path>` (the app then stores its SQLite DB
> at `<disk>/exam.db`), or point `DB_PATH` at a persistent absolute location, or
> set `DATABASE_URL` to a managed database. `DATABASE_URL` is fully supported,
> but the app ships SQLite/WAL-specific SQL — SQLite is the default and
> recommended engine.

> **Webcam proctoring requires HTTPS:** `navigator.mediaDevices.getUserMedia`
> only works in a *secure context* (HTTPS, or `http://localhost` during local
> development). Deploy on HTTPS (Render/Koyeb default to it) or snapshot
> capture is skipped gracefully — strikes and timestamps are still recorded,
> and the host's Proctoring Audit shows the "no snapshot captured" placeholder.
