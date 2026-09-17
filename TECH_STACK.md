# VeloTest Exam Platform — Technology Stack

Complete, current breakdown of every tool, library, platform, and integration in the platform —
including the enterprise-grade, host-configurable proctoring and browser-lockdown capabilities.

## 🌐 Core Application Framework & Languages

- **Python Flask**: The lightweight web framework handling routing, views, request contexts,
  lifecycle, OTP/email flows, PDF reports, and the anti-cheating endpoints (`app.py`, including
  `/exam/<id>/violation`). Everything lives in one module (~3,900 lines).
- **HTML5 / CSS3 / Vanilla JavaScript**: All frontend — including the entire proctoring engine in
  `static/exam.js`. No JS frameworks and **no external ML libraries** (the face detector is pure JS).
- **Jinja2 (Flask)**: 17 server-rendered templates (`welcome`, host auth/OTP, registration,
  exam, results, details, privacy, …).

## 🗄️ Database & Data Layer

- **SQLAlchemy & Flask-SQLAlchemy**: ORM managing hosts, exams, sessions, students, questions,
  answers, OTP tokens, daily-registration ledger, and proctoring incidents (11 tables).
- **`Exam.enable_proctoring` / `Exam.max_violations`**: the **host-configurable per-exam proctoring
  policy** — a Boolean toggle + strike threshold (3/5/7/10 dropdown on the Generate-Exam form,
  clamped 1–10 server-side), mirrored into `Exam.config` JSON and resolved by
  `_exam_proctoring_policy()`. Unproctored exams are a true no-lockdown mode (the server ignores
  violation reports).
- **`ExamViolation` model + proctoring columns on `Session`**: every security strike
  (`violation_type`, 1-based `count`, `detail`, base64 `snapshot`, `created_at`) is stored per
  attempt and foreign-keyed to the session, while `Session.violation_count`, `Session.flagged`, and
  `Session.auto_submitted` persist the server-authoritative tally, the checkmate flag, and whether
  the submission was forced at the host-configured strike threshold.
- **`DailyRegistration` ledger**: durable per-host rolling-24h registration cap, intentionally
  never touched by delete/reset actions.
- **SQLite**: The default file-based database (WAL mode) for reliable local development and
  zero-ops production.
- **Neon**: Serverless cloud PostgreSQL for production data storage; enabled simply by setting
  `DATABASE_URL`. The app uses **dual-dialect init** (conditional SQLite `check_same_thread` /
  PRAGMAs, `db.inspect(...)`-guarded `ALTER TABLE` migrations) so both engines work from one codebase.
- **psycopg2-binary**: The PostgreSQL adapter communicating with the Neon cloud database.

## 🔒 Security, Validation & State Management

- **Werkzeug**: Secure salted password hashing (`generate_password_hash` / `check_password_hash`)
  for host accounts and OTP codes.
- **Flask-WTF / CSRF Protection**: Global CSRF on **every** unsafe method (forms + JSON via the
  `X-CSRFToken` header), with a 6-hour token lifetime so long exams never fail submission.
- **Secure Cookies**: HttpOnly + SameSite configuration (`SESSION_COOKIE_SECURE=true` on HTTPS).
- **Flask-Limiter**: Per-IP rate limits (e.g. 120/min for `/time`, 10 000/min for submit/violation).
- **Marshmallow schemas**: Strict transport-layer validation for `/submit` and question payloads
  (`unknown=RAISE`, answer values must be int or str — blocks smuggled objects).
- **Server-authoritative violation counter**: strikes increment server-side in the same transaction
  that writes the `ExamViolation` row — page refreshes and tampered clients cannot reset the tally,
  and the `/violation` endpoint is a no-op when proctoring is disabled.
- **Monotonic strike debounce**: `performance.now()`-based 5-second cooldown collapses the
  `blur` + `visibilitychange` + `fullscreenchange` burst of one physical action into a single
  strike (no multi-event stacking).
- **Blocking overlays**: fixed full-viewport, high-z-index (`99999`) backdrops plus a capture-phase
  keydown lock; the warning modal hard-blocks clicking/typing/answering and pauses all monitoring
  until the student explicitly clicks "I Understand — Continue Exam".

## 🎥 Vanilla-JS Proctoring / Browser-Lockdown Engine (`static/exam.js`)

- **Camera gate**: `navigator.mediaDevices.getUserMedia` (640×480) verified via the Canvas
  `loadeddata` event (real frames), with a 10-second watchdog and per-error guidance; the server
  clock only starts via `/exam/<id>/start` after the gate passes.
- **Face-presence monitor**: every second a 160×120 canvas frame is analyzed with a BT.601 YCbCr
  skin-tone heuristic (≥ 8% central pixels) plus inter-frame motion (> 0.012 normalized delta);
  ~5 seconds of sustained absence reports a `face_not_detected` strike. Works fully offline.
- **DevTools detection**: a 2-second poll combining an outer/inner window-size-delta probe
  (≥ 100 px — catches docked panels including Network/Console) with a `debugger;` round-trip probe
  (> 120 ms — catches undocked/remote DevTools), re-armed every 5 seconds.
- **Lockdown**: fullscreen recapture on every click/keystroke (capture phase), tab-switch /
  focus-loss detection (`visibilitychange`/`blur`), `contextmenu` blocking, native
  `copy`/`cut`/`paste` blocking, and keydown blocking of `F12`, `Ctrl+Shift+I/J/C/E/K`, `Ctrl+U/S/P`,
  `F5`/`Ctrl+R`, `Ctrl+C/X/V`, `Ctrl+A` (Select All) and `F11`.
- **Immediate teardown**: the moment any submission is triggered, every media track is stopped (the
  webcam light turns off immediately), all countdown/monitor intervals are cleared, and all overlays
  are hidden; a failed submission restores monitors automatically.

## ✉️ Communication & Third-Party APIs

- **Brevo (formerly Sendinblue) (`sib-api-v3-sdk`)**: Transactional email for host and student OTP
  flows with **two isolated channels** — host keys/sender vs student keys (with key1→key2 failover
  and verified-sender handling).
- **ReportLab**: The pure-Python library generating downloadable result/session PDFs (with a
  print-friendly HTML fallback — no system binary like `wkhtmltopdf` required).

## ☁️ DevOps, Deployment & Uptime Monitoring

- **Render**: The cloud hosting provider running the web service (`velotest.onrender.com`) on
  Python 3.11; Persistent Disk via `RENDER_PERSISTENT_DISK_PATH` keeps the SQLite DB across
  redeploys (or `DATABASE_URL` for a managed database).
- **GitHub**: The version control repository hosting the project codebase (`Exam_Site_Live`), with
  a root `Procfile` (`gunicorn --chdir production_build wsgi:app`).
- **Visual Studio Code (VS Code)**: The primary code editor and local terminal environment.
- **UptimeRobot**: The automated external uptime monitor pinging the site every 5 minutes with HTTP
  requests to keep free-tier instances from spinning down.
- **gunicorn / waitress**: Production WSGI servers (Linux/macOS vs Windows) pinned in
  `requirements.txt`.

## 🧪 Built-in Verification Scripts

- **`_verify_proctoring.py`**: Guards the migration columns, asserts proctored pages render the
  camera gate + violation modal + `maxViolations`, that unproctored pages render none, that the
  violation endpoint is a no-op for unproctored exams, and that the host-configured threshold is
  honored (flags exactly at 5). Runs against a COPY of the DB.
- **`_verify_student.py` / `_verify_host_auth.py`**: Registration/submission and host OTP-auth flows.

## ⚖️ Legal & Compliance Pages

- **Privacy Policy (`privacy.html`)**: Comprehensive disclosures covering what data is collected
  (account info, exam logs, proctoring proof snapshots), how it's stored securely (relational DB,
  hashed passwords/OTPs, HttpOnly+SameSite cookies, CSRF, per-host isolation), retention, liability,
  and third-party sharing, with an auto-updated "Last updated" date and a footer link from every page.