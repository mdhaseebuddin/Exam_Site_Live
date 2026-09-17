# 🎓 Mega Blueprint Prompt — Fully Rebuildable Exam Platform

> **What this is:** A single, self-contained, copy-paste build prompt. Give it to any coding agent
> and it will reconstruct the **complete VeloTest-style online examination platform** end-to-end
> under a **new name**, wired to **new Brevo email keys**, a **new Neon PostgreSQL database**, and
> fully working **SQLite ↔ PostgreSQL dual-dialect handling**, **email OTP verification**,
> **server-side PDF reports** (pure-Python ReportLab), and **all 17 frontend templates**.
>
> Everything in this document was reverse-engineered from the working reference implementation.
> Follow it **exactly** unless a section says "choose/rename". Do not invent new dependencies.

---

## 0. MEGA-PROMPT (copy from here down in one block)

You are a senior full-stack engineer. Rebuild the entire production-grade online examination
platform described below **from scratch**, in a **`production_build/`** folder of a fresh repository.

### 0.1 Branding knobs (use these EXACT placeholders the owner fills in)

- `APP_NAME` — the new product name (e.g. `AcmeExam`). Use it in: `<title>` tags, navbar brand,
  email subjects, PDF titles, and the welcome page.
- `HOST_SENDER_EMAIL` — sender address **verified** on the host Brevo account.
- `STUDENT_SENDER_EMAIL` — sender address **verified** on the student Brevo account (must differ
  from the host sender; Brevo drops emails for unverified/freemail senders).
- `BREVO_HOST_API_KEY` / `BREVO_STUDENT_API_KEY_1` / `BREVO_STUDENT_API_KEY_2` — new API keys.
- `NEON_DATABASE_URL` — e.g. `postgresql://user:pass@host:5432/neondb`.
- `ADMIN_EMAIL` / `ADMIN_PHONE` — contact shown on the host dashboard capacity notice.

### 1. REPOSITORY LAYOUT (create exactly this)

```
<repo root>/
├── Procfile                        # web: gunicorn --chdir production_build wsgi:app
├── .gitignore                      # ignores .env, *.db*, instance/, logs/, user_logins/,
│                                   #   tests_conducted/, data/, __pycache__/, .venv/, *.exe
└── production_build/
    ├── app.py                      # the entire Flask application (routes, config, logic)
    ├── models.py                   # SQLAlchemy ORM models + dict serializers
    ├── schemas.py                  # Marshmallow input-validation schemas
    ├── wsgi.py                     # WSGI entrypoint (calls init_db() on boot)
    ├── requirements.txt            # pinned dependencies (section 3)
    ├── .env.example                # env template (section 4)
    ├── .gitignore                  # folder-level ignore rules (same as root)
    ├── instance/                   # runtime SQLite DB location (create at runtime)
    ├── logs/                       # rotating log files (create at runtime)
    ├── user_logins/                # per-host login audit files (create at runtime)
    ├── tests_conducted/            # per-host "tests conducted" reports (create at runtime)
    ├── static/
    │   ├── logo.png                # site favicon + logo
    │   ├── style.css               # shared stylesheet
    │   └── exam.js                 # exam-page logic + anti-cheating engine (camera gate,
    │   │                           #   fullscreen lockdown, shortcut/DevTools/face monitors,
    │   │                           #   blocking violation modal, teardown on submit)
    └── templates/                  # 17 Jinja2 templates (section 14)
        ├── welcome.html            ├── host.html            ├── host_login.html
        ├── host_login_verify.html  ├── host_register.html   ├── host_register_verify.html
        ├── host_forgot.html        ├── host_reset.html      ├── register.html
        ├── register_verify.html    ├── exam.html            ├── result.html
        ├── details.html            ├── exam_attempts.html   ├── details_pdf.html
        ├── result_pdf.html         └── privacy.html
```

### 2. NON-NEGOTIABLE ENGINEERING CONSTRAINTS

1. **One file `app.py`** — like the original, keep all routes + config + helpers in one module.
2. **Dual-dialect database (SQLite default, PostgreSQL via `DATABASE_URL`)** — this is a MUST.
   - `check_same_thread` must be passed **only** for SQLite; for PostgreSQL pass **empty
     `connect_args={}`**. Failing this is fatal: psycopg2 raises
     `ProgrammingError: invalid connection option "check_same_thread"`.
   - The SQLite `PRAGMA journal_mode=WAL / busy_timeout / synchronous / foreign_keys` event
     listener must be **guarded** with `isinstance(dbapi_connection, sqlite3.Connection)` so it
     never runs on PostgreSQL connections.
   - All `ALTER TABLE` migration statements in `init_db()` must be **column-existence-guarded**
     using `db.inspect(db.engine).get_columns(...)` (must work on both engines).
3. **Time is never trusted from the browser.** The exam countdown and deadline enforcement are
   server-anchored (absolute UTC Unix timestamps from the DB, re-synced by a `/time` endpoint;
   submit rejects after `deadline + 10s` grace).
4. **The correct answers NEVER leave the server.** The exam page receives only question text +
   options; grading is 100% server-side at submit time.
5. **Registration is email-OTP verified** for both hosts and students (Brevo transactional email).
6. **Every state-changing form POST and every JSON POST carries a CSRF token** (Flask-WTF global).
7. **Concurrency is handled with DB primitives.** Duplicate registration is stopped by a UNIQUE
   constraint on `students.session_id`; duplicate submission is stopped by one atomic
   `UPDATE sessions SET status='completed' WHERE id=:sid AND status!='completed' AND (SELECT
   COUNT(*) FROM sessions WHERE status='completed' AND host_email=:owner) < :max_cap`; catching
   `IntegrityError` is always done and never blocks startup.
8. **PDFs are generated with ReportLab (pure Python, no system binary)** and always fall back to a
   print-friendly HTML response (with a `.pdf` filename) if ReportLab is missing or throws.
9. **The violation tally is server-authoritative.** The client POSTs incidents to
   `/exam/<session_id>/violation`; the server appends an `ExamViolation` row, increments
   `session.violation_count` and flags the session at the host-configured `max_violations` in the
   SAME transaction. A tampered/page-refreshed client can never reset the count, and the endpoint
   is a NO-OP (`proctoring:false`, never flags) for exams created with proctoring disabled.
10. **The exam clock only starts after the camera gate.** `/exam/<session_id>/start` (POST) is
    called ONLY after the client verifies a live webcam stream and the student clicks "Start Exam";
    the deadline is then persisted server-side so camera-setup time is never charged and refreshes
    can't extend it.
11. **Webcam proctoring requires a secure context (HTTPS or `http://localhost`).** Without one,
    snapshot capture degrades gracefully — strikes and timestamps are still recorded, and the
    audit shows the "no snapshot captured" placeholder.

### 3. TECH STACK & PINNED DEPENDENCIES (`requirements.txt`)

```text
Flask==3.1.3
Flask-SQLAlchemy==3.1.1
Flask-WTF==1.3.0
Flask-Limiter==4.1.1
marshmallow==4.3.1
python-dotenv==1.2.2
sib-api-v3-sdk==7.6.0       # Brevo transactional email
reportlab==5.0.0            # pure-Python PDF generation
gunicorn                    # production WSGI server (Linux/macOS / cloud)
waitress==3.0.2             # Windows / local-dev fallback
psycopg2-binary==2.9.9      # PostgreSQL adapter (Neon)
```

**Guidance:** Use Werkzeug (bundled with Flask) for password hashing
(`generate_password_hash`/`check_password_hash`); Marshmallow for strict transport-layer
validation; Flask-Limiter for per-IP rate limits; `sqlalchemy.engine.make_url` to detect the
backend dialect at startup.

### 4. ENVIRONMENT VARIABLES (write exactly this as `.env.example`)

```ini
FLASK_ENV="production"
SECRET_KEY="CHANGE_ME_generate_a_random_64_char_hex_key"
SESSION_COOKIE_SECURE="true"          # must be "true" over HTTPS

# --- Sender addresses (Brevo) ---
MAIL_DEFAULT_SENDER="<HOST_SENDER_EMAIL>"
MAIL_STUDENT_SENDER="<STUDENT_SENDER_EMAIL>"

# --- Database ---
# Resolution order used by the app:
#   1. DATABASE_URL                -> used verbatim if set (e.g. Neon PostgreSQL)
#   2. DB_PATH                     -> explicit absolute SQLite file path
#   3. RENDER_PERSISTENT_DISK_PATH -> Render Persistent Disk mount -> <disk>/exam.db
#   4. production_build/instance/exam.db  (created automatically at startup)
DATABASE_URL="<NEON_DATABASE_URL>"
# DB_PATH=
# RENDER_PERSISTENT_DISK_PATH=

# --- Optional tuning (safe defaults shown) ---
# DB_PATH                             # default: instance/exam.db
# RENDER_PERSISTENT_DISK_PATH         # set by Render when a Persistent Disk is attached
# LOG_FILE                            # default: logs/exam.log
# MAX_SUBMISSIONS=500                 # lifetime completed-submissions cap (per host)
# DAILY_REGISTRATION_LIMIT=70         # strict per-host student registrations per rolling 24h
# DAILY_REGISTRATION_WINDOW_HOURS=24  # rolling window (hours) defining a host's "day"
# MAX_CONTENT_LENGTH=1048576            # 1 MiB default request cap
# WTF_CSRF_TIME_LIMIT=21600             # 6 h CSRF-token lifetime
# MAX_VIOLATIONS=3                      # global FALLBACK strike threshold for legacy attempts with
#                                       #   no parent Exam (per-exam max_violations overrides it)
# SNAPSHOT_MAX_BYTES=480000             # max base64 length accepted for a proctoring proof snapshot
```

`load_dotenv()` must run **before** reading `FLASK_ENV` / `SECRET_KEY`. Set `FLASK_ENV` and
`IS_PRODUCTION = FLASK_ENV == "production"`; fail-fast with `RuntimeError` if production and
`SECRET_KEY` is missing.
### 5. DATABASE SCHEMA (`models.py`) — 11 tables exactly like this

Use `db = SQLAlchemy()` (no init args; call `db.init_app(app)` in `app.py`). All timestamps are
ISO-8601 **strings** in UTC so templates render them directly. JSON columns store config blobs.

| Table | Columns & notes |
|---|---|
| `exams` | `id` String(32) PK (shareable token), `host_email` String(255) indexed+nullable, `config` JSON, `enable_proctoring` Boolean NOT NULL default True, `max_violations` Integer NOT NULL default 3 (the per-exam proctoring policy is MIRRORED into `config` too, so legacy serializers and session copies keep working), `created_at` String(64). Relationships `sessions` and `exam_questions` (both cascade all/delete-orphan; exam_questions ordered by position). |
| `exam_questions` | `id` int PK, `exam_id` FK→exams.id, `position` int (0-based), `question_id` String(16) nullable, `type` String(16) default `mcq`, `text` Text, `options` JSON, `correct_index` int. **Immutable snapshot of the bank at exam-generation time.** |
| `questions` | Master bank: `id` String(16) PK (`q_<hex>`), `host_email` String indexed+nullable, `type` String(16) (`mcq`/`essay`/`coding`), `text` Text, `options` JSON, `correct_index` int, `created_at` String(64). |
| `sessions` | `id` String(16) PK (session hex), `exam_id` FK→exams.id nullable indexed, `host_email` String nullable indexed, `expiry` DateTime nullable, `config` JSON, `status` String(16) default `pending` (`pending→registered→started→completed`), `started_at`/`deadline`/`completed_at` String(64) nullable, `score` int nullable, `total_selected` int nullable, `violation_count` Integer NOT NULL default 0, `flagged` Boolean NOT NULL default False, `auto_submitted` Boolean NOT NULL default False, `created_at` String(64). Cascades: `student` (uselist=False), `questions`, `answers`, `violations`. |
| `students` | `id` int PK, `session_id` String(16) **UNIQUE** FK→sessions.id (anti-duplicate registration), `name` String(200), `email` String(255) indexed, `phone` String(20), `custom_fields` JSON, `registered_at` String(64), `agreed_to_policy` Boolean default False, `agreed_at` String(64). |
| `host_users` | `id` int PK, `email` String(255) **UNIQUE** indexed, `name` String(200), `password_hash` String(255), `created_at` String(64). |
| `otp_tokens` | `id` int PK, `email` String(255) indexed, `code_hash` String(255), `purpose` String(32) default `reset`, `expires_at` String(64), `used` Boolean default False, `created_at` String(64). |
| `session_questions` | Per-attempt dealt-question snapshot. `id` int PK, `session_id` FK→sessions.id, `position` int, `question_id` String(16), `type`, `text`, `options` JSON, `correct_index` int. |
| `answers` | `id` int PK, `session_id` FK→sessions.id, `position` int, `question_id` String(16), `type` String(16), `response` JSON (int for MCQ / str for essay+coding), `correct` Boolean nullable, `correct_index` int nullable. |
| `daily_registrations` | Durable per-host ledger. `id` int PK, `host_email` String(255) indexed, `registered_at` String(64). **Never touched by delete/reset actions**, so a host's rolling-24h count survives exam deletion. |
| `exam_violations` | Proctoring incident ledger per attempt. `id` int PK, `session_id` FK→sessions.id, `violation_type` String(32), `count` int (1-based strike number), `detail` String(255) nullable, `snapshot` Text nullable (base64 JPEG proof data-URL), `created_at` String(64). Append-only: written in the SAME transaction that increments `sessions.violation_count`. |

**Serializers** in `models.py` must rebuild exactly the legacy dict shapes templates consume:
`bank_question_to_dict`, `session_question_to_dict`, `answer_to_graded_dict`,
`student_to_dict` (merges `custom_fields` with name/phone/registered_at), `session_to_dict`
(exam_title, time_limit_minutes, ratio, custom_registration_fields, required_fields default
`["name","phone"]`, **enable_proctoring, max_violations** (from the session config, default
True/3), questions, student, status, started_at, deadline, completed_at, answers
`{position: response}`, graded, score, total_selected, **violation_count, flagged,
auto_submitted, violations** (array of `{type, count, detail, snapshot, created_at}`),
created_at), and `exam_to_dict`
(exam_id, exam_title, time_limit_minutes, ratio, max_capacity, custom_registration_fields,
required_fields, **enable_proctoring / max_violations — read from the real columns FIRST with a
config fallback** (the columns are the single source of truth after the model change),
questions, created_at, attempt_count, completed_count).
### 6. DUAL-DIALECT DB INITIALIZATION (`app.py`) — THE CRITICAL FIX

Resolve the URI once, detect SQLite, and build engine options **conditionally**:

```python
import sqlite3
from sqlalchemy import event, text
from sqlalchemy.engine import Engine, make_url

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "instance", "exam.db"))

def _resolve_database_uri() -> str:
    """Priority: DATABASE_URL -> DB_PATH -> RENDER_PERSISTENT_DISK_PATH -> instance/exam.db."""
    explicit_uri = os.environ.get("DATABASE_URL", "").strip()
    if explicit_uri:
        return explicit_uri
    render_disk = os.environ.get("RENDER_PERSISTENT_DISK_PATH", "").strip()
    if render_disk and not os.environ.get("DB_PATH"):
        sqlite_file = os.path.join(render_disk, "exam.db")
    else:
        sqlite_file = DB_PATH
    os.makedirs(os.path.dirname(sqlite_file), exist_ok=True)
    return f"sqlite:///{sqlite_file}"

_DATABASE_URI = _resolve_database_uri()
_is_sqlite = make_url(_DATABASE_URI).get_backend_name() == "sqlite"

app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
_engine_options = {"pool_pre_ping": True}
if _is_sqlite:
    _engine_options["connect_args"] = {"check_same_thread": False}
else:
    _engine_options["connect_args"] = {}   # NEVER send SQLite-only args to PostgreSQL
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = _engine_options
db.init_app(app)

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return                                       # PG connections must be untouched
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
```

**`init_db()`** (called automatically from `wsgi.py`) must, inside `app.app_context()`:
`db.create_all()`; create the private dirs (`user_logins/`, `tests_conducted/`); run
**column-existence-guarded** `ALTER TABLE` migrations for `host_users.name`,
`questions.host_email`, `sessions.host_email`, `sessions.exam_id`, `students.agreed_to_policy`,
`students.agreed_at`, `students.email` (via `db.inspect(db.engine).get_columns(...)`; works on
both engines); commit; then idempotently back-fill the `daily_registrations` ledger from existing
`Student` rows joined to `Session` (uncommitted/pending rows roll back, wrapped so startup can
**never** fail).

### 7. APP SKELETON & GLOBAL HARDENING (`app.py`)

- Flask instance: `app = Flask(__name__)`.
- **Session cookie hardening:** `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE="Strict"`,
  `SESSION_COOKIE_SECURE` = env `SESSION_COOKIE_SECURE=="true"`.
- `MAX_CONTENT_LENGTH` 1 MiB default; `WTF_CSRF_TIME_LIMIT` 21600s default (6h, so long exams
  submit without the token aging out).
- **Rate limiting:** `limiter = Limiter(get_remote_address, app=app, default_limits=["10000 per minute"])`.
  Route-level limits: `host_register` POST 20/hour; `host_login` POST 20/min; `host_forgot_password`
  POST 10/hour; `host_reset_password` POST 10/hour; `/exam/<session_id>/time` 120/min;
  question/add/generate/register/submit POST 10000/min.
- **Security headers on every response (`after_request`):** `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `X-XSS-Protection: 1; mode=block`, and a Content-Security-Policy of `default-src 'self';
  script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; style-src 'self'
  https://cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data:; font-src 'self'
  https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'self'`.
- **Error handlers return JSON** for 400/404/500 and for `CSRFError` (a friendly "session or
  security token expired" message) — the exam client does `res.json()` on every path.
- **Logging:** rotating file handler on `logs/exam.log` (1 MB × 5 backups) + console handler;
  `_audit_logger = logging.getLogger("exam_audit")`; a small `audit(event, **details)` helper that
  JSON-serializes details into one line. Log HTTP statuses and all security events through it.
- **Context processor** `inject_capacity_context` exposes to every template: `completed_submissions`
  (host-scoped), `max_capacity` (500), `daily_student_registrations` (host-scoped),
  `daily_registration_limit` (70), `daily_registration_window_hours` (24).
### 8. AUTHENTICATION, ACCOUNTS & OTP SYSTEM (`app.py`)

**Constants:** `SESSION_HOST_EMAIL = "host_email"`, `MIN_PASSWORD_LEN = 8`,
`EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"`, `OTP_LIFETIME_MINUTES = 10` (DB-backed reset OTPs),
`OTP_SESSION_LIFETIME_MINUTES = 2` (session-backed login/register OTPs).

**Helpers:**
- `hash_password` → `generate_password_hash`; `verify_password` → `check_password_hash`
  (wrapped in try/except returning False).
- `find_host(email)` → latest `HostUser` by `email` lowercased, ordered by id desc (legacy-duplicate
  safety). `current_host()` reads `session[SESSION_HOST_EMAIL]`; `login_required` decorator
  redirects anonymous users to `host_login?next=request.path`.
- **Math CAPTCHA** (no external service): `captcha_payload()` generates `a op b` with `+ - *`
  (subtraction kept non-negative; a,b in 2..9), stores `session["captcha_answer"]` (string) and
  `session["captcha_text"]`. `verify_captcha` pops the stored answer (single-use) and compares
  `int`-normalized values. Always render `captcha=captcha_payload()` fresh after each POST attempt.
- **DB-backed reset OTP:** `_generate_otp()` = `f"{secrets.randbelow(1000000):06d}"`;
  `issue_otp(email, purpose="reset")` stores an `OtpToken` (hashed code, 10-min `expires_at`,
  `used=False`) and emails the raw code; `verify_otp` succeeds only for an unused, unexpired,
  matching-hash token and **marks it used** (single-use).
- **Session-backed OTP (login/register, 2-min):** `_set_session_otp` stores `{"code_hash":
  hash_password(code), "generated_at": datetime.utcnow()}` in the session; `_issue_session_otp`
  picks host vs student email channel; `verify_session_otp` validates expiry (normalize naive/aware
  UTC before comparing!) — on expiry, clears the key and returns `"OTP has expired. Please request
  a new one."`; on mismatch `"Invalid verification code. Please try again."`.

**Host registration (`/host/register`, GET+POST, 20/hour):** 2-step flow tracked in session as
`reg_step` (`input`→`verify`). Step 1 validates email regex, name required, password ≥ 8 chars and
match, duplicate check (`find_host` → flash "Account already exists. Please log in." + redirect to
login), CAPTCHA, and the "I Agree to the Terms & Privacy Policy" checkbox (`agree == "1"`). Then it
stores `session["reg_pending"] = {email, name, password_hash}` and emails a session OTP
(`channel="host"`). Step 2 supports `action=resend` and verify: on success re-checks duplicates
(pre + `IntegrityError` catch on commit), creates `HostUser`, writes `log_user_login(host,
"REGISTER")`, `session.clear()`, sets the host session, redirects to dashboard. Templates:
`host_register.html`, `host_register_verify.html`.

**Host login — `/host/login` (GET+POST, 20/min):** password+email+CAPTCHA+agree → stores
`session["login_pending"]` and issues a 2-min session OTP (`login_otp`). Step 2 verifies it (also
`action=resend`), then sets `session[SESSION_HOST_EMAIL]`, `log_user_login(host, "LOGIN")`, and
redirects to `next` (only if it starts with a single `/`) or the dashboard. Also **legacy-duplicate
compat:** if `find_host` matches nothing but a row whose hash verifies exists, use that. Templates:
`host_login.html`, `host_login_verify.html`.

**Host logout — `/host/logout` (POST only):** pops the session email, honors a safe `next`, redirects
to login.

**Forgot/reset password — `/host/forgot-password` (GET+POST, 10/hour) + `/host/reset-password`
(GET+POST, 10/hour):** forgot: CAPTCHA-checked; if a matching host exists, `issue_otp(email)`;
**always** render the same "sent" page to prevent account enumeration. reset: verify CAPTCHA, valid
email+OTP, password policy + match; on success `verify_otp`, update the hash, audit, render success.
Templates: `host_forgot.html`, `host_reset.html`.

### 9. EMAIL INTEGRATION — BREVO TRANSACTIONAL API (TWO ISOLATED CHANNELS)

Use `sib_api_v3_sdk` exactly like the reference:

- **Host channel** `_send_otp_email(email, code)`: key = `BREVO_HOST_API_KEY` (fallback
  `BREVO_API_KEY`); sender = `MAIL_DEFAULT_SENDER`; subject "Your {APP_NAME} password reset code";
  body includes the code + `OTP_LIFETIME_MINUTES` expiry. If no key configured, **always log the
  code to the console/audit** (`otp_generated`) and return False (flow still works in dev).
- **Student channel** `_send_student_otp(email, code)`: keys = `BREVO_STUDENT_API_KEY_1`,
  `BREVO_STUDENT_API_KEY_2`; **failover:** try key 1; on any failure (exception, or a 2xx response
  with a missing `message_id`) fall through to key 2. sender = `MAIL_STUDENT_SENDER` **— never** the
  host sender (student keys are verified against a different sender; mixing them makes Brevo drop
  the email). Success requires a real `message_id` on the response — treat absence as failure.
  Extract `status`/`reason`/`body` from `ApiException` for the audit trail. On total failure, log the
  code to console/audit.
- Always `print(f"\n[OTP DEBUG] Target: {email} | Code: {code}\n")` for local/ngrok debugging.
### 10. HOST DASHBOARD & EXAM-GENERATION FEATURES

**Registration configuration constants:** `REG_FIELD_LABELS = {name: "Full Name", email: "Email
Address", phone: "Phone Number", address, department}`; `PERMANENT_REQUIRED_FIELDS =
["email", "phone"]` (always collected+verified even if the host configures fewer);
`DEFAULT_REQUIRED_FIELDS = ["name", "email", "phone"]`; server-side validation: name must match
`^[A-Za-z][A-Za-z\s.'-]*$`, phone exactly `^\d{10}$`, email via `EMAIL_RE`. `slugify(text)` turns
any display label into an HTML-safe input name (`"My Field!"` → `my_field`).

**Question bank (`/host`, `/host/question`, `/host/question/<qid>/delete`, `/clear_questions`):**
- Dashboard `host()` renders `host.html` with only **this host's** questions and exams
  (`filter_by(host_email=...)`) — strict per-host isolation.
- `add_question()` (POST): validate via `validate_question_payload`; type ∈ {`mcq`,`essay`,`coding`};
  MCQ requires ≥ 2 non-empty options and a 0-indexed `correct_index` in range; store
  `id=f"q_{uuid.uuid4().hex[:12]}"`, `host_email`, `created_at` (UTC ISO).
- `delete_question` / `clear_questions`: ownership-guarded; clear removes only this host's bank rows.

**Exam generation (`/host/generate`, POST):** require ≥ 1 bank question (else `error=empty_bank`)
and the terms checkbox (`agree=="1"` else `error=must_agree`); parse a positive `time_limit`
(minutes) and `ratio` (clamped to `[1, len(bank)]`); `max_capacity = MAX_SUBMISSIONS` (500,
hardcoded — not host-configurable). Parse dynamic **custom registration fields**
(`cf_name`/`cf_required`/`cf_slug` triplets) into `[{name, required, slug}]` (dedupe slugs with a
`_<i>` suffix). Build the `Exam` with `id = uuid4().hex[:16]` and config JSON {exam_title,
time_limit_minutes, ratio, max_capacity, custom_registration_fields, required_fields,
enable_proctoring, max_violations}, and set the real columns `enable_proctoring` /
`max_violations`. **Host proctoring policy:** `enable_proctoring` is a standard HTML checkbox
(`value="1"` when checked; the dashboard JS greys out and drops the `max_violations` select from
the submitted form when the switch is off) and `max_violations` is a 3/5/7/10 dropdown, clamped
server-side to `[1, 10]`. The same policy is copied into each Session's `config` at registration
(`_exam_proctoring_policy(ex)` resolves `(enabled, max_violations)`; legacy attempts with no parent
Exam default to enabled + the env `MAX_VIOLATIONS`), so the exam page and server read ONE
consistent policy.
**Snapshot the entire bank** into `ExamQuestion` rows (immutable isolation); commit; audit
`exam_generated`; refresh `/host` (redirect).
**Randomizer** `randomize_questions(bank, ratio)`: clamp the ratio, `random.sample` without
repetition, `random.shuffle` the subset, `copy.deepcopy` each question, then for MCQ shuffle
options and **recompute `correct_index`** against the shuffled order (locate the correct option
text). Never mutate the master bank.

**Attempts/details/PDF/delete:** `host_exam_attempts` (`/host/exam/<exam_id>/attempts`, all
sessions under an owned Exam), `session_details` (`/host/session/<session_id>`, owned only),
`session_details_pdf` (`/host/session/<session_id>/details.pdf` →
`pdf_response("details_pdf.html", f"session_{session_id}.pdf", ...)`). `delete_exam` cascades to
all sessions/students/questions/answers/exam_questions (ownership-guarded). `clear_submissions`
deletes all of this host's sessions + related rows but **keeps the question bank intact**.

**Bulk export `/host/download-all.zip`:** stream a ZIP in memory, one folder per submission
(`<safe_folder_name>`; dedupe collisions with `_2`, `_3`...), each containing `details.pdf` (or
`details.html` fallback) + `submission.json` (raw data, indent=2), plus a top-level `index.json`
(exported_at, host_email, submission_count, submissions summary) and `README.txt`. Filename
`exam_submissions_<username>_<UTC%Y%m%d_%H%M%S>.zip`; audit; `send_file(..., as_attachment=True)`.

### 11. STUDENT FLOWS — UNIVERSAL PORTAL, REGISTRATION, EXAM, SUBMIT, RESULTS

**Universal portal `/exam/register/<exam_id>` (main entry) and legacy
`/exam/<session_id>/register` + `/register/<session_id>`** implement the same gates and a 2-step
(input → OTP-verify) flow; pending data lives in `session["stu_reg_pending"]` (with exam_id or
session_id stored so a stale browser can be detected and reset):

1. **Lifetime capacity gate:** `completed_submissions_count(ex.host_email) >= MAX_SUBMISSIONS` →
   clear error (JSON `{error}` 400 on AJAX, else rendered template).
2. **Daily cap gate:** `daily_registration_blocked(ex.host_email)` → rolling-window error message.
3. **Step 1 (input):** validate name/phone/email (server-side regexes), reject duplicate
   email for this exam/session, verify CAPTCHA and the privacy checkbox (`agree=="1"`); build
   `custom_vals` from config slugs; send a **student-channel** OTP; set `stu_reg_step="verify"`.
4. **Step 2 (verify):** `action=resend` re-emails; otherwise verify the OTP then finalize: create
   `Student` (`agreed_to_policy=True`, `agreed_at`/`registered_at` = UTC now), flip
   `pending→registered`, insert the `DailyRegistration` ledger row **in the same transaction**,
   **re-check the daily cap before commit** (rollback + block if hit), and catch `IntegrityError`
   on the UNIQUE `students.session_id` (already registered → send them onward).
- **Exam page `/exam/<session_id>`:** unknown id → redirect to dashboard; `status=="completed"`
  → render `result.html`; session not authorized (`session[f"auth_{session_id}"]` missing) →
  redirect to its registration portal; un-registered → force registration (universal portal if the
  session has an `exam_id`, else the legacy register route). First visit: persist `started_at`,
  compute `deadline = started_at + time_limit_minutes`, set `status="started"` (survives
  refreshes). Render `exam.html` with `public_questions` (text + options **only**, never answers),
  `deadline_unix`, `server_now_unix`, `remaining_seconds`, `csrf_token()`, plus the student's
  captured details and required/custom field labels, AND the proctoring config
  `enable_proctoring` / `max_violations` / `violation_threshold` / `violation_count` / `flagged`
  (resolved via `_exam_proctoring_policy`). A `flagged` attempt (or a force-submit pending from a
  previous session) renders a `deadline_unix=0` sentinel and force-submits on load — the whole
  suite is skipped client-side when proctoring is disabled.
- **`/exam/<session_id>/time` (JSON, 120/min):** returns `{server_now_unix, deadline_unix,
  flagged, violation_count, violation_threshold}`; rejects completed/invalid/not-started sessions.
  This is the countdown/drift anchor AND the re-anchor for the server-authoritative violation
  tally — the client force-submits on the next poll when the server reports `flagged`.
- **`/exam/<session_id>/start` (POST JSON, 10000/min):** starts the official server-anchored
  countdown — called ONLY after the blocking camera gate confirms a live webcam stream, so
  camera-setup time is never charged. Idempotent (a running clock just returns its deadline).
- **`/exam/<session_id>/submit` (POST JSON, 10000/min):** wrapped in try/except so **every** path
  returns JSON. Inside `_submit_exam`:
  1. Marshmallow `validate_submit_payload` (400 on malformed/unknown fields).
  2. Load `Session` (400 if missing).
  3. **Deadline enforcement:** `now > deadline + 10s` → 400 "The exam time has expired." Malformed
     deadline is logged but does not block.
  4. **Atomic gate** (single SQL UPDATE with `WHERE status != 'completed'` AND completed-count <
     max_cap) — rowcount 0 → "invalid or already submitted" or, if cap hit, the capacity message.
  5. Reload the (now-owned) session. **Proctoring:** if `s.flagged` is set (the attempt reached
     the host-configured `max_violations`), persist `auto_submitted = True` and audit
     `exam_submitted_flag` so the host's Proctoring Audit card renders. Merge submitted `student`
     info into the stored record (custom slugs included).
  5b. **`/exam/<session_id>/violation` (POST JSON, 10000/min, server-authoritative):**
     unproctored sessions → NO-OP `{ok, proctoring:false, count, threshold, auto_submit:false,
     flagged:false}` (never counts, never flags). Otherwise: clamp `type` ≤ 32 chars / `detail` ≤
     255, accept the optional `snapshot` ONLY as a `data:image/` URL ≤ `SNAPSHOT_MAX_BYTES`, then
     in ONE transaction increment `session.violation_count`, set `flagged` at `max_violations`,
     append the `ExamViolation` row and audit. Respond `{ok, count, threshold, auto_submit,
     flagged}` (+ a message when auto_submit).
  6. **Grade** each dealt question: MCQ → compare `int(raw)` to `correct_index` (score++);
     essay/coding → store the text verbatim (no auto score).
  7. Commit answers + `completed_at`. All-auto-graded → persist `score`/`total_selected` and return
     JSON `{ok, score, total, percent, manual_review: False, session_id}`; otherwise
     `manual_review=True`, `score=None`. Refresh `tests_conducted/<host>.log`.
  8. Any exception: `rollback()`, traceback print, audit, JSON 500.
- **`/exam/<session_id>/result.pdf`:** completed sessions only →
  `pdf_response("result_pdf.html", f"result_{session_id}.pdf", session=...)`; else bounce back to
  the exam/result flow. No PDF is ever produced for unknown/invalid ids.

### 12. PDF GENERATION — REPORTLAB (PURE-PYTHON) WITH HTML FALLBACK

- Import ReportLab inside a `try/except` (guard `_REPORTLAB_AVAILABLE`).
- `_build_result_pdf(session_dict)` and `_build_details_pdf(session_dict, custom_fields)` return
  `bytes` built with `SimpleDocTemplate(BytesIO(), pagesize=A4, 15mm margins)` using
  `Paragraph`, `Table/TableStyle`, `KeepTogether`, `Spacer`; color-code correct (green ✔) /
  incorrect (red ✘) / correct-answer highlighting; show "Awaiting manual review" for
  essay/coding only when needed.
- `pdf_response(template_name, download_name, **ctx)`: guarantee the filename ends with `.pdf`;
  if `_pdf_bytes()` returns bytes → `Response(body, Content-Type: application/pdf,
  Content-Disposition: attachment; filename="..." )`; else fall back to a print-friendly HTML
  render with an `inline; filename="...pdf"` header (browser "Save as…" still yields `.pdf`).

### 12.5 ANTI-CHEATING & BROWSER-LOCKDOWN SYSTEM (host-configurable proctoring) — `static/exam.js` + `app.py`

The proctoring suite lives in `static/exam.js` (frontend enforcement) and is backed by
server-authoritative endpoints in `app.py`. It is **skipped entirely** for exams generated with
`enable_proctoring=false`: no camera gate, no lockdown listeners, no monitors installed, no strikes
counted — and `/exam/<session_id>/violation` is a no-op for such sessions (defense in depth).

**Policy resolution (`_exam_proctoring_policy(ex)` → `(enabled, max_violations)`):** for a real
`Exam`, the columns `enable_proctoring` / `max_violations` are the single source of truth (host
dropdown 3/5/7/10, clamped 1–10, mirrored into config JSON); for legacy attempts with no parent
Exam, enabled=True and the env `MAX_VIOLATIONS` (default 3) are used. The exam page renders the
policy into `#examConfig` as `enableProctoring`, `maxViolations`, `violationThreshold`,
`violationCount`, and `flagged`; a `flagged` attempt force-submits immediately on load.

**Camera gate (pre-exam, blocking):** `verifyCamera()` opens
`navigator.mediaDevices.getUserMedia` ({video: 640×480 ideal, audio: false}) and treats the stream
as verified only when the `<video>` element fires Canvas `loadeddata` (i.e. real frames arrive — a
stream that reports but delivers no pixels fails). A 10 s watchdog (`CAMERA_CONNECT_TIMEOUT_MS`),
friendly per-error messages (denied / no device / busy / overconstrained / insecure context) and a
retry button are shown until it passes. Only then does `/exam/<id>/start` fire, starting the
server-anchored countdown and its 30 s `/time` re-sync.

**Fullscreen enforcement:** the page requests browser fullscreen on load and re-enters it on EVERY
click and keystroke (capture phase), so `Esc` / `F11` cannot keep the student out; `fullscreenchange`
exits are reported as `fullscreen_exit` strikes and covered by a blocking "Enter Fullscreen"
overlay until the student clicks back in.

**Tab / focus / clipboard / shortcut lockdown (installed ONLY when PROCTORING_ENABLED):**
- `visibilitychange` (hidden) → `tab_switch`; `window blur` → `focus_loss`; returning focus /
  visibility re-requests fullscreen.
- `contextmenu` (right-click / inspect) and native `copy` / `cut` / `paste` events are
  preventDefault'ed.
- `keydown` blocks: `F12`, `Ctrl+Shift+I/J/C/E/K`, `Ctrl+U`, `Ctrl+S`, `Ctrl+P`, `F5`, `Ctrl+R`,
  `Ctrl+C/X/V`, `Ctrl+A` (Select All) and `F11` — each reported as a `devtools_shortcut`/
  `copy_paste` strike with the key name.
- On every strike `captureSnapshot()` JPEG-encodes one webcam frame (Canvas `toDataURL`, 1.5 s
  watchdog) as the proof payload.

**DevTools detection (2 s poll):** `checkDevTools()` uses (1) the outer-inner window-size delta
(`DEVTOOLS_DELTA_THRESHOLD = 100` px — catches docked panels incl. Network/Console/…) and (2) a
`debugger;` statement round-trip probe (`DEVTOOLS_DEBUGGER_THRESHOLD_MS = 120` — catches
undocked/remote DevTools). One `devtools_detected` strike per open session, re-armed after
`DEVTOOLS_REARM_MS = 5000` so keeping DevTools open keeps accruing strikes.

**Face-presence monitor (vanilla JS, NO external ML):** `startFaceMonitor()`/`checkFacePresence()`
every `FACE_MONITOR_INTERVAL_MS = 1000` draws the LIVE stream to a 160×120 canvas and analyzes the
central ~70%: (1) **skin-tone** — BT.601 YCbCr pixels ≥ `FACE_SKIN_THRESHOLD = 0.08` of central
pixels; (2) **motion** — normalized inter-frame channel delta > `FACE_MOTION_THRESHOLD = 0.012`
(a present-but-still student is never flagged). Both absent for `FACE_MONITOR_MISSES_TO_FLAG = 5`
consecutive ticks (~5 s) → `face_not_detected` strike. The miss-streak resets on any present frame
and on restart.

**Strike debounce (monotonic):** `reportViolation(type, detail)` drops anything inside
`VIOLATION_COOLDOWN_MS = 5000` measured with `performance.now()` (a monotonic clock), so the
`blur` + `visibilitychange` + `fullscreenchange` burst of ONE physical action counts exactly ONE
strike. The cooldown resets when the warning modal is acknowledged so the next action is fairly
counted.

**Strike pipeline (server-authoritative):**
1. `reportViolation` → `captureSnapshot` → `postViolation` POSTs `{type, detail, snapshot}` to
   `/exam/<session_id>/violation` (with the CSRF header).
2. Server: unproctored → no-op; otherwise validate (`type` ≤ 32, `detail` ≤ 255, snapshot only as
   `data:image/` ≤ `SNAPSHOT_MAX_BYTES`), then in ONE transaction increment
   `session.violation_count`, flag at `max_violations`, append the `ExamViolation` row, audit.
3. Response `{count, threshold, auto_submit, flagged}` → client sets `violationCount =
   Math.max(violationCount, data.count)`; `auto_submit` / threshold → `forceSubmitExam()`; else →
   `showWarningModal(count, ...)`.

**Warning modal (blocking — pauses monitoring):** `showWarningModal` shows the fixed full-viewport
`.violation-overlay` backdrop (`position:fixed; inset:0; z-index:99999; backdrop-filter:blur(3px)`)
with a **"Strike X of Y"** badge (`#violationStrikeBadge`), an explicit **"Reason: …"** line
(`#violationModalReason`, mapped from the type code via `VIOLATION_REASONS`:
`face_not_detected` / `tab_switch` / `focus_loss` / `fullscreen_exit` / `devtools_detected` /
`devtools_shortcut` / `contextmenu` / `copy_paste`), the caution text, the remaining-strikes
sentence, and the "I Understand — Continue Exam" button. While open:

- a capture-phase `keydown` handler swallows EVERY keystroke (only Enter/Space on the
  auto-focused `#violationAckBtn` passes, and Tab is blocked so focus cannot drift), and the
  backdrop blocks all clicks — the student cannot click, type, or answer;
- `warningModalOpen = true` + `pauseProctoringMonitors()` immediately clear the face-monitor,
  DevTools, countdown and server-sync intervals, and `reportViolation()` drops all further
  reports — so NO new strike can ever stack behind an un-acknowledged warning.

Only the student's click on **"I Understand — Continue Exam"** runs `resumeProctoringMonitors()`
(restarts the countdown/server-sync + DevTools detector + face monitor with a fresh absence grace,
resets the strike cooldown, re-anchors the clock via `/time`), hides the modal, and re-asserts
fullscreen.

**Teardown on submission:** `teardownExamMedia()` stops every media track immediately (webcam
hardware light turns off NOW, not after the fetch), nulls `srcObject` on preview/video elements,
clears the countdown / server-sync / face-monitor / DevTools intervals and hides all overlays. It
runs on manual submit, time-out auto-submit, and forced (violation-threshold) submit; a failed
submission restores monitors via `resumeExamAfterFailedSubmit()` (+ `acquireCameraStream()`).

**Host review:** `details.html` renders the red "Proctoring Audit — Repeated Violations
(Auto-Submitted)" card ONLY when `session.flagged and session.auto_submitted and
session.violation_count >= threshold`, listing every strike chronologically (type, detail,
timestamp, thumbnail proof snapshot, or the "no snapshot captured" placeholder).

### 13. FRONTEND TEMPLATES (`templates/`) — 17 FILES

All templates load Bootstrap 5.3.3 from `cdn.jsdelivr.net`, the local `style.css`, and the logo
favicon. Every form includes `{{ csrf_token() }}` hidden input; all confirmations for destructive
POSTs run client-side. Rebuild each with these context variables and content:

| Template | Purpose & required context |
|---|---|
| `welcome.html` | Landing page (brand `<APP_NAME>`, hero, features, how-it-works, about, contact `<ADMIN_EMAIL>`/`<ADMIN_PHONE>`, footer with © year, tagline, and a **Privacy Policy & Legal Disclaimer link** to `/privacy`). No context vars. |
| `privacy.html` | Legal page with numbered sections: 1 Purpose, 2 No Liability for Data Loss, 3 No Liability for Server Downtime, 4 No Liability for Illegal Activities/Cheating, 5 User Responsibility & Code of Conduct, 6 Data We Collect (account details, host profile, examination & test records), 7 How Your Data Is Stored Securely (relational DB, hashed passwords/OTPs, HttpOnly+SameSite cookies, CSRF, per-host access isolation), 8 Data Retention & Why We Keep It (account persistence, session management, exam history across logins), 9 Data Confidentiality & Third Parties (no selling; no disclosure to unauthorized third parties; minimal sharing only for core services such as email delivery), 10 Changes to This Policy. Footer "Last updated" auto-set via JS. |
| `host_login.html` / `host_login_verify.html` | 2-step login. Login form: email, password, math CAPTCHA display (`captcha.left op captcha.right`), terms checkbox, error flash. Verify form: email echo, OTP input, resend + verify actions. |
| `host_register.html` / `host_register_verify.html` | 2-step registration. Input: name, email, password, confirm, CAPTCHA, terms checkbox. Verify: email echo, OTP input, resend + verify. |
| `host_forgot.html` | Email + CAPTCHA; after POST show a static "sent" message (anti-enumeration). |
| `host_reset.html` | Email + OTP + new password + confirm + CAPTCHA; success screen on success. |
| `host.html` | Dashboard: navbar with host email badge + logout; feedback `query` alerts (submissions_cleared, exam_deleted, errors empty_bank/invalid_question/invalid_config/must_agree/session_not_found); **Daily Registration Capacity notice & progress card** (`daily_registration_limit`, `daily_student_registrations`, `daily_registration_window_hours`, `completed_submissions`, `max_capacity`, contact `<ADMIN_EMAIL>`/`<ADMIN_PHONE>`); left column = Add Question form (type select mcq/essay/coding, dynamic options rows, correct index) + question bank list with delete buttons + Clear All; right column = Generate Exam form (exam_title, time_limit, ratio, **Proctoring policy block: `enable_proctoring` switch + `max_violations` dropdown 3/5/7/10 with JS grey-out/drop when disabled**, custom registration fields builder via `cf_name`/`cf_required`/`cf_slug` formed as `field_<i>[]` arrays, terms checkbox) + exams list showing each link (`{{ url_for('exam_register', exam_id=x.exam_id) }}`), attempts link, delete button. |
| `exam_attempts.html` | Per-exam attempt list: exam title, each candidate (name/email/phone/status/score), link to `/host/session/<id>`. Context: `exam`, `attempts`, `custom_registration_fields`. |
| `details.html` | Session details: student/candidate info, per-question answered review, score / "Awaiting manual review", download links (details PDF, result PDF), and the **Proctoring Audit** card shown ONLY when the attempt was flagged + auto-submitted at the strike threshold (chronological strikes with `type`, `detail`, `created_at`, thumbnail proof snapshots or the "no snapshot captured" placeholder). Context: `session` (serialized dict incl. `violations`, `violation_count`, `flagged`, `auto_submitted`), `violation_threshold`. |
| `register.html` | Candidate registration portal: exam title, dynamic required fields (name/email/phone + custom), CAPTCHA, mandatory privacy checkbox; used by BOTH the universal and legacy flows (different `form.action`s). Context: `error`, `form`, `captcha`, `required_fields`, `field_labels`, `custom_registration_fields` (+ `exam_id` or `session_id`). |
| `register_verify.html` | Candidate OTP step with resend/verify. Context: `email`, `error`, `exam_id`/`session_id`. |
| `exam.html` | Distraction-free exam: title bar (`exam_title`), candidate-details panel, question counter + server countdown (`#timer`) + Finish btn; single-question card; prev/next footer; hidden `#resultBox`; then a JSON script tag `#examConfig` containing `{sessionId, questions, remainingSeconds, deadlineUnix, serverNowUnix, totalQuestions, student, csrfToken, enableProctoring, maxViolations, violationThreshold, violationCount, flagged}` serialized with `| tojson`, loaded by `exam.js`. **When `enable_proctoring` is true the page also renders the proctoring overlays:** `#cameraGateOverlay` (blocking webcam-verification gate with preview + Start Exam), `#violationModal` (strict warning with `#violationStrikeBadge`, `#violationModalReason`, `#violationModalText`, `#violationModalCount`, `#violationAckBtn`), `#fullscreenBlockOverlay` (Enter Fullscreen), and `#forceSubmitOverlay` (auto-submitting…). |
| `result.html` | Result view: score card (percent, score/total) with "Download Result PDF" button linking `/exam/<session_id>/result.pdf`, plus answer review. Context: `session` (serialized dict). |
| `details_pdf.html` / `result_pdf.html` | Print-friendly HTML mirroring the ReportLab PDFs (used both as the fallback and as a reference): title header, student/candidate info, per-question answer review, print CSS (`@media print`). |

The JSON config tag uses `tojson` (no series of inline `<script>` JavaScript); `exam.js` reads it
via `JSON.parse(document.getElementById("examConfig").textContent)`.

### 14. FRONTEND LOGIC (`static/exam.js`)

IIFE reading `#examConfig` (parsed via
`JSON.parse(document.getElementById("examConfig").textContent)`) with THREE responsibilities:
(A) the exam UI, (B) the server-anchored clock, and (C) — when `cfg.enableProctoring !== false` —
the complete anti-cheating engine described in §12.5.

**Exam UI:** single-question rendering (MCQ radios labeled `A./B./C.`; essay/coding textareas with
placeholders); previous/next navigation; `answers = {index: value}` kept in memory; Prev/Next/Finish
disabled until the camera gate passes.

**Clock:** server-anchored countdown derived ONLY from `deadlineUnix - serverNowUnix` (browser
clock never trusted), ticking every second and re-synced via `fetch("/exam/<id>/time")` every 30 s;
when time hits zero it auto-submits once (guards `submitting`/`submitted` so repeated triggers
can't double POST). `syncFromServer()` also re-anchors `violationCount` and force-submits if the
server reports the attempt flagged.

**Submit:** manual submit asks `window.confirm`; POST JSON to `/exam/<id>/submit` with header
`X-CSRFToken: cfg.csrfToken` and body `{student: cfg.student, answers}`; parse the JSON response
defensively (a non-JSON error body never throws raw tokens at the student); render a "Thank You"
card for `manual_review`, else a percent/success-fail score card in `#resultBox`; alert only when
the submission did NOT already succeed. `teardownExamMedia()` (stop webcam tracks, clear every
interval, hide overlays) runs the instant ANY submission is triggered, and a failed submission
restores everything via `resumeExamAfterFailedSubmit()`.

**Anti-cheating engine (PROCTORING_ENABLED only)** — implement it exactly as described in §12.5:
blocking camera gate (verifyCamera → `/start` once real frames arrive), fullscreen recapture,
tab/focus/clipboard/shortcut lockdown, DevTools window-size + debugger-probe detection, the vanilla
face monitor, the monotonic 5 s strike debounce, the `reportViolation → /violation` pipeline, the
blocking "Strike X of Y" warning modal that pauses monitoring and resumes only on the ack button,
and the 5 s / 30 s / 2 s / 1 s interval bookkeeping with null-guards so nothing leaks.

### 15. INPUT-VALIDATION SCHEMAS (`schemas.py`) — MARSHMALLOW

Replicate exactly:
- `StudentSchema` — typed `name`/`phone`/`registered_at` strings with `unknown="INCLUDE"` (so
  arbitrary custom registration slugs pass through).
- `SubmitPayloadSchema` — `student: Nested(StudentSchema)`, `answers: Dict(keys=String,
  values=Raw)` with `unknown="RAISE"` at the top level; a `validates_schema` hook rejects any
  answer value that is neither `int` nor `str` (blocks smuggling lists/dicts/booleans).
- `QuestionSchema` — `text` required+non-blank, `type ∈ {mcq, essay, coding}`, `options: List[String]`
  optional, `correct_index: Integer` optional. Note: NOT `unknown="RAISE"` because the HTML form
  posts many helper fields.
- Helpers `validate_submit_payload(payload) -> str | None` and
  `validate_question_payload(data) -> str | None` return flattened, human-readable error strings
  (used by the routes to render `error=...`).

### 16. WSGI ENTRYPOINT (`wsgi.py`)

```python
from app import app, init_db
init_db()                      # create tables + guarded migrations at boot
if __name__ == "__main__":
    app.run()
```

### 17. ADMIN / AUDIT FILE OUTPUTS (build these too)

- `log_user_login(host, event)` appends a line to `user_logins/<safe_email>.log`:
  `EVENT=... | EMAIL=... | NAME=... | TESTS_CONDUCTED_TO_DATE=... | WHEN_UTC=...`.
- `write_tests_conducted(host_email)` regenerates `tests_conducted/<safe_email>.log`: a
  "HOST TEST REPORT" header followed by every exam session (session id, exam title, status,
  timestamps, time limit, ratio, score, questions asked, submissions, student details, and each
  answer line). `_safe_filename` strips anything not `A-Za-z0-9._-`.

### 18. DEPLOYMENT & OPERATIONS

- **Repository-root `Procfile`:** `web: gunicorn --chdir production_build wsgi:app` — the `--chdir`
  is REQUIRED because the modules use plain absolute imports (`from app import app`,
  `from models import ...`); dotted-module form fails. Paths are resolved from `__file__`, not the
  working directory.
- **Local dev:** `pip install -r requirements.txt`; run with `waitress-serve --listen=0.0.0.0:8000
  wsgi:app` (Windows) or `gunicorn --workers 4 --threads 2 --bind 0.0.0.0:8000 wsgi:app`.
- **Cloud (Render/whatever):** build `pip install -r production_build/requirements.txt`; start =
  Procfile (gunicorn). For persistence on ephemeral free tiers attach a Persistent Disk and set
  `RENDER_PERSISTENT_DISK_PATH=<mount>` (SQLite stored at `<mount>/exam.db`) — otherwise accounts
  vanish on every redeploy. For Neon/PostgreSQL just set `DATABASE_URL=<NEON_DATABASE_URL>`.
- **UptimeRobot:** an external uptime monitor pinging the site every 5 minutes is recommended so
  free-tier hosts don't spin down from inactivity.

### 19. FULL ROUTE MAP (implement every route with these exact paths + methods)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/favicon.ico` | — | serve `static/logo.png` |
| GET | `/` | — | redirect to dashboard if logged in, else `welcome.html` |
| GET | `/privacy` | — | public legal page |
| GET+POST | `/host/register` | 20/hr POST | host 2-step registration w/ OTP |
| GET+POST | `/host/login` | 20/min POST | host 2-step login w/ OTP |
| POST | `/host/logout` | — | end session |
| GET+POST | `/host/forgot-password` | 10/hr POST | request reset OTP (anti-enumeration) |
| GET+POST | `/host/reset-password` | 10/hr POST | verify OTP + set new password |
| GET | `/host` | login | dashboard |
| POST | `/host/question` | login | add bank question |
| POST | `/host/question/<qid>/delete` | login | delete owned question |
| POST | `/clear_questions` | login | clear owned question bank |
| POST | `/host/clear-submissions` | login | wipe sessions (keep bank) |
| GET | `/host/download-all.zip` | login | bulk ZIP export |
| GET | `/host/exam/<exam_id>/attempts` | login | per-exam attempt list |
| GET | `/host/session/<session_id>` | login | session details |
| GET | `/host/session/<session_id>/details.pdf` | login | details PDF |
| POST | `/host/exam/<exam_id>/delete` | login | cascade-delete exam |
| POST | `/host/generate` | login | generate exam + snapshot bank |
| GET+POST | `/exam/register/<exam_id>` | 10000/min POST | universal candidate portal |
| GET | `/exam/<session_id>` | — | exam page (auth-gated) |
| GET | `/exam/<session_id>/result.pdf` | — | result PDF (completed only) |
| GET | `/exam/<session_id>/time` | 120/min | server-anchored time + violation re-anchor JSON |
| POST | `/exam/<session_id>/start` | 10000/min | start server-anchored countdown (after camera gate; idempotent) |
| POST | `/exam/<session_id>/violation` | 10000/min | anti-cheating strike reporter (server-authoritative; no-op when unproctored) |
| GET+POST | `/exam/<session_id>/register`, `/register/<session_id>` | 10000/min POST | legacy registration gate |
| POST | `/exam/<session_id>/submit` | 10000/min | save + grade (JSON) |

### 20. VALIDATION / ACCEPTANCE CHECKLIST (the agent must self-verify)

1. `python -m py_compile app.py models.py schemas.py wsgi.py` — zero errors.
2. **Dual dialect:** import the app (a) with `DATABASE_URL` unset → `SQLALCHEMY_ENGINE_OPTIONS
   ["connect_args"] == {"check_same_thread": False}` and (b) with `DATABASE_URL=postgresql://...`
   → `connect_args == {}` and `set_sqlite_pragma` no-ops on a non-`sqlite3.Connection`. On a real
   Neon DB, `/` must boot without the `invalid connection option "check_same_thread"` error and
   `db.create_all()` must produce all tables.
3. **Host register → OTP → dashboard** works end-to-end with a stubbed Brevo client (dev fallback
   prints the code).
4. Add MCQ + essay questions; generate an Exam; open `/exam/register/<exam_id>`; register a
   student (CAPTCHA + OTP + privacy checkbox) → the exam page shows the server-anchored timer.
5. Complete + submit → JSON result; auto-graded MCQ score correct; essay shows
   `manual_review=True`; `result.pdf` downloads a real PDF (or HTML fallback).
6. Host dashboard shows the attempt, score/status, and the `details.pdf` download; ZIP export
   contains `details.pdf`/`submission.json` per attempt + `index.json`.
7. Re-submit the same session returns "invalid or already submitted"; a second parallel ticket
   also can't double-complete (atomic gate).
8. Security headers + CSP present on a sample response; CSRF missing fails as JSON 400; all JSON
   error paths are parseable by the client.
9. `init_db()` re-runs on an existing DB without error (guarded migrations are idempotent).
10. The privacy page renders all sections and is linked in the site footer.
11. **Proctoring migration + pages:** run `python _verify_proctoring.py` — the guarded migration
    adds `enable_proctoring`/`max_violations` to `exams`; a proctored page renders the camera gate +
    `#violationModal` + `"maxViolations": 5`; an unproctored page renders NONE of them; the
    `/exam/<id>/violation` endpoint is a no-op for unproctored and honours the host's threshold
    (flags exactly at 5 for proctored).
12. **Gate + force-submit flow:** with proctoring enabled the exam stays BLOCKED until the camera
    gate confirms real frames (`/start` is only callable then); a `flagged` session force-submits on
    load; and a flagged submission persists `auto_submitted=True` so the host's Proctoring Audit
    card renders in `details.html`.
13. **No-strike-stacking:** with the v2 modal, opening the warning pauses face/DevTools/countdown/
    server-sync intervals and drops further reports until the student clicks "I Understand —
    Continue Exam".

### 21. OPERATOR HANDOFF NOTES (write these into a `README.md`)

- How to generate a `SECRET_KEY` (`python -c "import secrets; print(secrets.token_hex(32))"`).
- How to copy `production_build/.env.example` → `.env` and fill it (new Brevo keys, senders,
  Neon URL).
- How to run locally (waitress / gunicorn) and deploy (Procfile, `--chdir production_build`,
  persistent disk OR `DATABASE_URL`).
- The dual-dialect database explanation (`check_same_thread` is SQLite-only; PRAGMAs guarded).
- The privacy-policy summary and footer link.
- How to configure **per-exam proctoring** in the Generate-Exam form (the `Enable Proctoring` switch
  and the `Max Violations` dropdown 3/5/7/10): unproctored exams are a true no-lockdown mode (the
  server ignores violation reports), the warning modal hard-blocks the screen and PAUSES monitoring
  until the student clicks "I Understand — Continue Exam", and `MAX_VIOLATIONS`/`SNAPSHOT_MAX_BYTES`
  are only global fallbacks for legacy attempts without a parent Exam.

---

**End of mega-prompt.** The output of running this prompt must be a complete, runnable
`production_build/` package that behaves identically to the reference VeloTest platform, branded
under the new `<APP_NAME>`.