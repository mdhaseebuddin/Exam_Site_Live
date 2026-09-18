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

Every exam attempt runs inside a locked-down, **host-configurable** proctoring
environment. When the host enables proctoring for an exam (the default), the
student cannot start or answer until their webcam is verified, and the browser
is locked down until the exam is submitted:

- **Per-exam proctoring policy (host-configurable)** — each generated exam
  carries its own `enable_proctoring` toggle and a `max_violations` strike
  threshold (3 / 5 / 7 / 10 on the Generate-Exam form, clamped server-side to
  1–10). Unproctored exams skip the camera gate, the browser lockdown, and
  strike counting entirely — the server treats any violation report as a
  no-op, and students take the exam like a normal test.
- **Blocking webcam verification gate** — the exam stays fully locked until
  `navigator.mediaDevices.getUserMedia` delivers real video frames (verified
  via the Canvas `loadeddata` event). Denied / missing / busy cameras show a
  blocking error with retry guidance; the server clock only starts once the
  student clicks "Start Exam", so camera-setup time is never charged.
- **Fullscreen enforcement with click recapture & `Esc` mitigation** — the exam
  page requests browser fullscreen on load and re-enters it on **every** click
  or keystroke (capture phase), so `Esc` / `F11` cannot keep the student out.
  Exiting fullscreen is counted as a violation and a blocking "Enter
  Fullscreen" overlay appears until the student clicks back in.
- **Tab-switch, focus-loss, clipboard & shortcut lockdown** — `visibilitychange`
  and `blur` detect tab switches, window minimizes, and clicks into other
  applications; right-click (`contextmenu`), native `copy`/`cut`/`paste`,
  `Ctrl+C/X/V`, `Ctrl+A` (Select All), `F12`, `Ctrl+Shift+I/J/C/E/K`,
  view-source (`Ctrl+U`), save/print (`Ctrl+S/P`), refresh (`F5` / `Ctrl+R`),
  and the `F11` fullscreen toggle are all blocked while the exam is active.
- **DevTools detection (window-size + debugger probe)** — a 2-second poll
  catches docked DevTools via the outer/inner window-size delta and undocked /
  remote DevTools via a `debugger;` round-trip probe, reporting
  `devtools_detected` strikes with a 5-second re-arm so keeping DevTools open
  keeps accruing strikes.
- **Vanilla-JS face-presence monitor** — every second a small Canvas frame is
  analyzed with a broadened YCbCr skin-tone heuristic plus inter-frame motion;
  ~7 seconds of sustained absence reports a `face_not_detected` strike. No
  external ML library is used and it works fully offline.
- **Proof snapshots on every strike** — each incident captures one webcam
  frame, JPEG-encoded to a base64 data-URL via the HTML5 Canvas API, uploaded
  with the incident, and persisted server-side on the `ExamViolation` record
  (size-capped by `SNAPSHOT_MAX_BYTES`).
- **Monotonic 5-second strike debounce** — one physical action (tab switch,
  focus loss, fullscreen exit) fires `blur` + `visibilitychange` +
  `fullscreenchange` near-simultaneously; a `performance.now()`-based 5-second
  cooldown collapses them into **one** strike, so students are never double-
  charged for a single action.
- **Server-authoritative violation counter & deadline-tamper protection** —
  every incident increments `session.violation_count` server-side in the same
  transaction that writes the `exam_violations` row, so page refreshes and
  tampered clients **cannot reset the tally**; the countdown and submit
  deadline are anchored to absolute UTC server timestamps and enforced on the
  server (any submission past the deadline is rejected).
- **Blocking warning modal that pauses monitoring** — non-final strikes show a
  strict modal with the exact count ("Strike 1 of 3") and the specific reason
  ("Reason: Face not detected in camera view"). While it is open, a
  high-z-index backdrop plus a capture-phase keydown lock block all clicking,
  typing and answering, and **every background check (face monitor, DevTools
  poll, countdown and server-sync timers) is paused** so strikes can never
  stack behind an un-acknowledged warning — nothing resumes until the student
  clicks "I Understand — Continue Exam".
- **Immediate teardown on submit & auto-submit at the threshold** — the moment
  a submission is triggered (manual, time-out, or the final strike), every
  media track is stopped (the webcam light turns off immediately), all
  intervals are cleared, and overlays are hidden. At `max_violations` the exam
  is force-submitted and the student is redirected to the results page.
- **Conditional host session review** — the host's Session Details page renders
  a dedicated **Proctoring Audit** card (strike count, chronological
  timestamps, and thumbnail previews of the proof snapshots) **only** when the
  attempt reached the host-configured `max_violations` threshold and was
  auto-submitted. Normal completions with fewer strikes stay clean.

The global fallback policy is tunable with environment variables:
`MAX_VIOLATIONS` (default `3`, used only for legacy attempts without a parent
Exam) and `SNAPSHOT_MAX_BYTES` (default `480000`) — every exam generated through
the dashboard carries its own per-exam proctoring policy, which takes
precedence.

## Configuration

All configuration comes from environment variables, loaded at startup with
`python-dotenv` (`load_dotenv()` in `app.py`). See
`production_build/.env.example` for the full list. The variables required in
production are:

| Variable      | Purpose                                                |
|---------------|--------------------------------------------------------|
| `SECRET_KEY`  | Flask session signing (fail-fast if missing)           |
| `FLASK_ENV`   | `production`                                           |
| `BREVO_HOST_API_KEY` | Host-channel Brevo API key (`xkeysib-…`) for password-reset/login OTP emails |
| `BREVO_STUDENT_API_KEY_1` | Student-channel Brevo API key (tried first) for registration OTP emails |
| `BREVO_STUDENT_API_KEY_2` | Optional student failover key (used only if key 1 fails) |
| `MAIL_DEFAULT_SENDER` | Host OTP sender (must be verified on the host Brevo key's account) |
| `MAIL_STUDENT_SENDER` | Student OTP sender (must be verified on the student Brevo keys' account) |
| `MAX_SUBMISSIONS` | Lifetime cap on completed submissions (default 500)           |
| `DAILY_REGISTRATION_LIMIT` | Strict per-host cap on student registrations per 24h (default 70) |
| `DAILY_REGISTRATION_WINDOW_HOURS` | Rolling window (hours) defining a host's "day" (default 24) |
| `MAX_VIOLATIONS` | Global **fallback** strike threshold that force-submits a flagged attempt (default 3; per-exam `max_violations` set on the Generate-Exam form takes precedence) |
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
