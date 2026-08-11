"""
Exam Platform — Flask + SQLAlchemy (SQLite) production version
==============================================================

Migrated from JSON file storage to a relational database with
concurrency-safe transaction gates for registration and submission.

Database Schema
---------------
  questions           master question bank
  sessions            exam session (UUID id, JSON config, status)
  students            candidate registration (1:1 with sessions via UNIQUE FK)
  session_questions   per-candidate dealt-question snapshot (isolated)
  answers             submitted response per question (FK -> sessions)

Concurrency Safety
------------------
  * Registration: UNIQUE constraint on students.session_id ensures only one
    row can exist per session — simultaneous INSERTs collide and the loser
    is rolled back gracefully.
  * Submission: Atomic UPDATE on sessions.status with a WHERE clause —
    only one worker can advance status to 'completed' per session.
  * Randomization: random.sample() + session creation happen inside a single
    transaction, so the dealt question subset is atomically persisted.
  * SQLite WAL mode + busy_timeout allows concurrent readers while
    serializing writers.
"""

import copy
import io
import logging
import logging.handlers
import os
import random
import re
import secrets
import shutil
import smtplib
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash

from models import (
    Answer,
    Exam,
    ExamQuestion,
    HostUser,
    OtpToken,
    Question,
    Session,
    SessionQuestion,
    Student,
    bank_question_to_dict,
    db,
    exam_to_dict,
    session_to_dict,
    student_to_dict,
)
from schemas import validate_question_payload, validate_submit_payload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")  # kept for backward compat; logs still use it
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.environ.get(
    "LOG_FILE", os.path.join(LOGS_DIR, "exam.log")
)
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "instance", "exam.db"))

# ---------------------------------------------------------------------------
# Maximum capacity — the total number of COMPLETED submissions the platform
# will accept across ALL generated exam sessions. This is a hardcoded, fixed
# limit of 500 (not configurable by the host). The host cannot raise or lower
# it via the UI; it is enforced globally in the backend on both registration
# and submission.
# ---------------------------------------------------------------------------
MAX_SUBMISSIONS = int(os.environ.get("MAX_SUBMISSIONS", "500"))

# ---------------------------------------------------------------------------
# Secrets & configuration — loaded from .env (never from the code itself)
# ---------------------------------------------------------------------------
load_dotenv()  # reads SECRET_KEY, FLASK_ENV, SESSION_COOKIE_SECURE, ...

FLASK_ENV = os.environ.get("FLASK_ENV", "production")
IS_PRODUCTION = FLASK_ENV == "production"

app = Flask(__name__)

# SECRET_KEY: fail-fast in production instead of silently using an insecure
# fallback. In development, a clearly-marked fallback is acceptable.
_secret_key = os.environ.get("SECRET_KEY", "")
if IS_PRODUCTION and not _secret_key:
    raise RuntimeError(
        "SECRET_KEY is not set. Set SECRET_KEY in the .env file "
        "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`)."
    )
app.config["SECRET_KEY"] = _secret_key or "insecure-dev-fallback-change-me"

# --- Session cookie hardening ---
# HttpOnly      : the cookie is invisible to JavaScript (blocks XSS cookie theft).
# SameSite=Strict: the cookie is never sent on cross-site requests — the
#                 strongest CSRF defense-in-depth (blocks the large majority
#                 of CSRF attacks even without a token check).
# Secure        : only sent over HTTPS (must be true in production).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
)
# Suppress the default Flask banner; we log HTTP status codes ourselves.
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("MAX_CONTENT_LENGTH", 1 * 1024 * 1024)
)  # 1 MiB request cap by default

# --- Global CSRF protection (Flask-WTF) ---
# Enforces a valid CSRF token on EVERY unsafe method (POST/PUT/PATCH/DELETE),
# including JSON routes via the X-CSRFToken header. Forms include the token
# as a hidden field via {{ csrf_token() }}.
csrf = CSRFProtect(app)

# Long-running exams (up to N minutes) may legitimately exceed the default
# 3600-second CSRF lifetime, which would cause a submission to fail after
# the token aged out. Configurable via WTF_CSRF_TIME_LIMIT (seconds).
app.config["WTF_CSRF_TIME_LIMIT"] = int(
    os.environ.get("WTF_CSRF_TIME_LIMIT", str(6 * 60 * 60))  # 6 hours default
)

# ---------------------------------------------------------------------------
# SQLAlchemy (SQLite) configuration
# ---------------------------------------------------------------------------
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{DB_PATH}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "connect_args": {"check_same_thread": False},
}

db.init_app(app)


# Enable WAL mode + busy_timeout for concurrent reads and safe writes.
# This is critical when running under Gunicorn with multiple workers, as
# each worker has its own SQLite connection. WAL mode allows readers to
# proceed without blocking on a writer, and busy_timeout prevents premature
# "database is locked" errors under contention.
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


PRIVATE_DIRS = ("user_logins", "tests_conducted")


def _ensure_private_dirs() -> None:
    """Create the private, git-ignored data folders if they do not exist."""
    for name in PRIVATE_DIRS:
        os.makedirs(os.path.join(BASE_DIR, name), exist_ok=True)


def _safe_filename(token: str, fallback: str = "unknown") -> str:
    """Turn a user-supplied string into a safe single-segment filename."""
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(token).strip()).strip("._")
    return token or fallback


def _current_host_email() -> str | None:
    """Return the authenticated host's email, or None if not logged in."""
    host = current_host()
    return (host.email if host else None) or session.get(SESSION_HOST_EMAIL)


def _host_tests_count(host_email: str) -> int:
    """Number of exam sessions a host has generated so far."""
    return Session.query.filter_by(host_email=host_email).count()


def log_user_login(host, event: str) -> str:
    """
    Append a login/registration audit line to the host's private file under
    user_logins/. The line records Email, Name, total tests conducted to date,
    and a precise timestamp. Returns the path written (for confirmation).
    """
    _ensure_private_dirs()
    email = (host.email or "unknown").strip().lower()
    name = (host.name or "").strip() or email.split("@")[0]
    total_tests = _host_tests_count(email)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = (
        f"EVENT={event} | EMAIL={email} | NAME={name} | "
        f"TESTS_CONDUCTED_TO_DATE={total_tests} | WHEN_UTC={now}\n"
    )
    path = os.path.join(BASE_DIR, "user_logins", f"{_safe_filename(email)}.log")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    return path


def write_tests_conducted(host_email: str) -> str:
    """
    Write (or refresh) the host's complete test report under tests_conducted/.
    The host's details appear first, followed by every exam session and its
    submissions with timestamps. Returns the path written.
    """
    _ensure_private_dirs()
    host_email = (host_email or "").strip().lower()
    host = HostUser.query.filter_by(email=host_email).first()
    name = (host.name if host else "").strip() or host_email.split("@")[0]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines = []
    lines.append("=" * 62)
    lines.append("HOST TEST REPORT")
    lines.append("=" * 62)
    lines.append(f"Host Email : {host_email}")
    lines.append(f"Host Name  : {name}")
    lines.append(f"Report UTC : {now}")
    total_tests = _host_tests_count(host_email)
    lines.append(f"Total Tests Conducted : {total_tests}")
    lines.append("")

    sessions = (
        Session.query.filter_by(host_email=host_email)
        .order_by(Session.created_at.desc())
        .all()
    )
    if not sessions:
        lines.append("(No tests conducted yet.)")
    else:
        for i, s in enumerate(sessions, 1):
            lines.append("-" * 62)
            lines.append(f"TEST #{i}")
            lines.append("-" * 62)
            lines.append(f"Session ID      : {s.id}")
            lines.append(f"Exam Title      : {(s.config or {}).get('exam_title', '') or '(untitled)'}")
            lines.append(f"Status          : {s.status}")
            lines.append(f"Created (UTC)   : {s.created_at or '-'}")
            lines.append(f"Started (UTC)   : {s.started_at or '-'}")
            lines.append(f"Completed (UTC) : {s.completed_at or '-'}")
            lines.append(f"Time Limit      : {(s.config or {}).get('time_limit_minutes', 30)} min")
            lines.append(f"Ratio           : {(s.config or {}).get('ratio', 0)}")
            lines.append(f"Score           : {s.score if s.score is not None else 'manual / n/a'}")
            lines.append(f"Questions asked : {s.total_selected or 0}")
            lines.append(f"Submissions     : {len(s.answers)}")
            if s.student:
                st = s.student
                lines.append(f"Student Name    : {st.name or '-'}")
                lines.append(f"Student Phone   : {st.phone or '-'}")
                lines.append(f"Registered (UTC): {st.registered_at or '-'}")
            lines.append("  Answers:")
            for a in (s.answers or []):
                resp = a.response
                lines.append(f"    Q{a.position + 1} [{a.type or 'mcq'.upper()}] -> {resp!r}")
            lines.append("")

    path = os.path.join(BASE_DIR, "tests_conducted", f"{_safe_filename(host_email)}.log")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def init_db() -> None:
    """Create all database tables if they do not exist.
    Call this after the app config is finalized (e.g. in wsgi.py or __main__).
    Lightweight migration: add any new columns introduced in newer model
    definitions (ALREADY-existing SQLite tables get ALTER TABLE ADD COLUMN).
    """
    with app.app_context():
        db.create_all()
        _ensure_private_dirs()
        # --- Lightweight migration for schema additions -------------------
        insp = db.inspect(db.engine)
# HostUser.name
        host_cols = [c["name"] for c in insp.get_columns("host_users")]
        if "name" not in host_cols:
            db.session.execute(text("ALTER TABLE host_users ADD COLUMN name VARCHAR(200) NOT NULL DEFAULT ''"))
        # Question.host_email (per-host question-bank isolation)
        q_cols = [c["name"] for c in insp.get_columns("questions")]
        if "host_email" not in q_cols:
            db.session.execute(text("ALTER TABLE questions ADD COLUMN host_email VARCHAR(255)"))
# Session.host_email
        sess_cols = [c["name"] for c in insp.get_columns("sessions")]
        if "host_email" not in sess_cols:
            db.session.execute(text("ALTER TABLE sessions ADD COLUMN host_email VARCHAR(255)"))
        # Session.exam_id (per-student attempt -> parent Exam)
        if "exam_id" not in sess_cols:
            db.session.execute(text("ALTER TABLE sessions ADD COLUMN exam_id VARCHAR(32)"))
        # Student.agreed_to_policy / agreed_at (back-fill for older DBs)
        stu_cols = [c["name"] for c in insp.get_columns("students")]
        if "agreed_to_policy" not in stu_cols:
            db.session.execute(text("ALTER TABLE students ADD COLUMN agreed_to_policy BOOLEAN NOT NULL DEFAULT 0"))
        if "agreed_at" not in stu_cols:
            db.session.execute(text("ALTER TABLE students ADD COLUMN agreed_at VARCHAR(64)"))
        db.session.commit()

# ---------------------------------------------------------------------------
# Logging & auditing
# ---------------------------------------------------------------------------
os.makedirs(LOGS_DIR, exist_ok=True)
log_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

# File handler — rotating at 1 MB, keeps 5 backups on disk.
file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Console handler — mirrors important events to stdout/stderr for dev.
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# Root logger for the app + werkzeug access/error logs.
_audit_logger = logging.getLogger("exam_audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.addHandler(file_handler)
_audit_logger.addHandler(console_handler)
_audit_logger.propagate = False

werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.setLevel(logging.INFO)
werkzeug_logger.addHandler(file_handler)


def audit(event: str, **details) -> None:
    """Write one structured audit-line to the log."""
    import json as _json
    _audit_logger.info("%s | %s", event, _json.dumps(details))


# ---------------------------------------------------------------------------
# Host authentication & security helpers
# ---------------------------------------------------------------------------
SESSION_HOST_EMAIL = "host_email"

# Password policy: enforce a reasonable minimum length.
MIN_PASSWORD_LEN = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_ok(password: str) -> bool:
    """Returns True when the password satisfies the minimum-length policy."""
    return isinstance(password, str) and len(password) >= MIN_PASSWORD_LEN


def hash_password(password: str) -> str:
    """Hash a plaintext password with a per-user random salt (werkzeug)."""
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored hash."""
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        return False


def find_host(email: str) -> HostUser | None:
    """Look up a host account by (lowercased) email."""
    if not email:
        return None
    return HostUser.query.filter_by(email=email.strip().lower()).first()


def current_host() -> HostUser | None:
    """Return the logged-in host user, or None if not authenticated."""
    email = session.get(SESSION_HOST_EMAIL)
    if not email:
        return None
    return HostUser.query.filter_by(email=email).first()


def login_required(view):
    """
    Decorator that protects host-only routes. Anonymous users are redirected
    to the host login page; after a successful login they are sent back to
    the page they originally requested.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_host() is None:
            return redirect(url_for("host_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# --- Math CAPTCHA (session-based, no external service / API keys) ----------
def captcha_payload() -> dict:
    """
    Generate a simple arithmetic CAPTCHA challenge and stash the answer in
    the Flask session. Returns the operands/operator so the template can
    render the human-readable question.
    """
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    op = random.choice(["+", "-", "*"])
    if op == "-":
        # keep the result non-negative for readability
        if a < b:
            a, b = b, a
        answer = a - b
    elif op == "*":
        answer = a * b
    else:
        answer = a + b
    session["captcha_answer"] = str(answer)
    session["captcha_text"] = f"{a} {op} {b}"
    return {"left": a, "op": op, "right": b}


def verify_captcha(submitted) -> bool:
    """
    Verify the submitted CAPTCHA answer against the value stored in the
    session. Always consumes the stored answer (single-use) so a stale,
    reused challenge cannot be replayed.
    """
    expected = session.pop("captcha_answer", None)
    if expected is None:
        return False
    try:
        return str(int(submitted)) == str(expected)
    except (TypeError, ValueError):
        return False


# --- OTP (one-time password) for password reset ----------------------------
OTP_LIFETIME_MINUTES = 10


def _generate_otp() -> str:
    """Return a cryptographically-random 6-digit code."""
    return f"{secrets.randbelow(1000000):06d}"


def _send_otp_email(email: str, code: str) -> bool:
    """
    Deliver the OTP code to the host's email address.

    If SMTP credentials are configured via environment variables the code is
    emailed. Otherwise (e.g. local development) the code is logged to the
    server console/log so the flow works out-of-the-box.
    """
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    # Debug: Always log the OTP to the console for local testing/ngrok.
    print(f"\n[OTP DEBUG] Target: {email} | Code: {code}\n")

    if smtp_host and smtp_user:
        try:
            msg = EmailMessage()
            msg["Subject"] = "Your Exam Platform password reset code"
            msg["From"] = smtp_from
            msg["To"] = email
            msg.set_content(
                "Your Exam Platform password reset code is: "
                f"{code}\n\nThis code expires in {OTP_LIFETIME_MINUTES} minutes."
            )
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                if int(os.environ.get("SMTP_STARTTLS", "1")) == 1:
                    server.starttls()
                    server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            print(f"[OTP ERROR] SMTP Failure: {exc}")
            traceback.print_exc()
            audit("otp_email_failed", email=email, error=str(exc), ip=get_remote_address())
            # fall through to console logging so the user can still get the code
    else:
        print("[OTP] SMTP not configured. OTP printed to console only.")

    # No SMTP configured, or delivery failed -> log the code (dev-friendly).
    audit("otp_generated", email=email, code=code, ip=get_remote_address())
    return False




def issue_otp(email: str, purpose="reset") -> str:
    """Create and store a fresh OTP token for the given email; return the code."""
    code = _generate_otp()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=OTP_LIFETIME_MINUTES)
    ).isoformat()
    token = OtpToken(
        email=email.strip().lower(),
        code_hash=hash_password(code),

        purpose=purpose,
        expires_at=expires_at,
        used=False,
    )
    db.session.add(token)
    db.session.commit()
    _send_otp_email(email.strip().lower(), code)
    return code



def verify_otp(email: str, code: str, purpose="reset") -> bool:
    """
    Validate a submitted OTP. Succeeds only if:
      * a matching token exists for the email,
      * it is not already used,
      * it has not expired.
    A successful match marks the token as used (single-use).
    """
    email = email.strip().lower()
    token = (

        OtpToken.query.filter_by(email=email, purpose=purpose, used=False)
        .order_by(OtpToken.created_at.desc())
        .first()
    )
    if token is None:
        return False
    try:
        expires = datetime.fromisoformat(token.expires_at)
    except (TypeError, ValueError):
        return False
    if expires < datetime.now(timezone.utc):
        return False
    if not verify_password(code, token.code_hash):
        return False
    token.used = True
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Maximum-capacity helpers
# ---------------------------------------------------------------------------
def completed_submissions_count(host_email: str | None = None) -> int:
    """
    Total number of COMPLETED submissions tied to a host.


    When ``host_email`` is provided, only that host'S COMPLETED sessions are
    counted — giving each host their own independent 500-submission capacity.
    When omitted, the global count across all hosts is returned.
    """
    q = Session.query.filter_by(status="completed")
    if host_email:
        q = q.filter_by(host_email=host_email)
    return q.count()


def session_max_capacity(s: Session) -> int:
    """
    The capacity limit for a given session. This is a fixed, hardcoded value
    (MAX_SUBMISSIONS = 500) that applies to every session. The host cannot
    override it via the UI; it is enforced globally in the backend.
    """
    return MAX_SUBMISSIONS


def capacity_error(max_cap: int) -> str:
    """Human-readable error message when an exam is at capacity."""
    return f"Exam capacity reached. Maximum {max_cap} submissions allowed."


@app.context_processor
def inject_capacity_context():
    """
    Inject the current host's submission count and capacity into every
    template. This powers the host dashboard counter (Submissions: X / 500)
    without needing to pass it explicitly to every render call.

    The count is scoped to the CURRENT logged-in host, so each host sees
    their own independent capacity usage.
    """
    return {
        "completed_submissions": completed_submissions_count(
            _current_host_email()
        ),
        "max_capacity": MAX_SUBMISSIONS,
    }


# ---------------------------------------------------------------------------
# Rate limiting (defense against brute force / DDoS)
# ---------------------------------------------------------------------------
limiter = Limiter(
    get_remote_address,  # key the rate limit on the caller's IP address
    app=app,
    default_limits=["10000 per minute"],  # global safety net per IP
)


# ---------------------------------------------------------------------------
# Security headers (all responses)
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    """Apply defense-in-depth HTTP headers to every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' https://cdn.jsdelivr.net; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    return response


# ---------------------------------------------------------------------------
# HTTP error logging (4xx / 5xx) — flags potential attack probes
# ---------------------------------------------------------------------------
@app.errorhandler(400)
def log_bad_request(e):
    audit("http_400", status=400, path=request.path, method=request.method,
          ip=get_remote_address())
    return jsonify({"error": "Bad request."}), 400


@app.errorhandler(404)
def log_not_found(e):
    audit("http_404", status=404, path=request.path, method=request.method,
          ip=get_remote_address())
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(500)
def log_server_error(e):
    audit("http_500", status=500, path=request.path, method=request.method,
          ip=get_remote_address())
    return jsonify({"error": "Internal server error."}), 500


# ---------------------------------------------------------------------------
# PDF export helpers — pure-Python (ReportLab) with a print-HTML fallback.
#
# The old implementation used pdfkit + the wkhtmltopdf SYSTEM BINARY, which
# many hosting providers do not install. That caused the PDF feature to break
# (or silently fall back to HTML) in production.
#
# This rewrite uses ReportLab — a pure-Python library that runs on ANY host
# with no external binary — so real `.pdf` downloads always work. As a final
# safety net, if ReportLab is unavailable or fails for any reason, we fall
# back to rendering the print-friendly HTML template so the app NEVER crashes.
# ---------------------------------------------------------------------------

# Reports are built with ReportLab.pdfbase (pure Python, no system binary).
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import (
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    _REPORTLAB_AVAILABLE = True
except Exception:  # pragma: no cover - ReportLab missing on the host
    _REPORTLAB_AVAILABLE = False


def _pdf_fallback(template_name, **ctx):
    """
    Render the print-friendly HTML template as the graceful fallback.
    The templates already contain print CSS so they can be saved to PDF from
    any browser, and this guarantees the app never crashes when PDF tooling
    is unavailable.
    """
    return render_template(template_name, **ctx)


# --- ReportLab document builders -------------------------------------------

def _build_result_pdf(session: dict) -> bytes:
    """
    Build a real PDF (BytesIO) for the graded RESULT view using ReportLab.
    Mirrors the content of result_pdf.html:
      * score summary box
      * per-question answer review (MCQ correct/incorrect, essay/coding text)
    """
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], spaceAfter=4)
    sub_style = ParagraphStyle("SubX", parent=styles["Normal"], textColor=colors.HexColor("#666666"), spaceAfter=14)
    h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], spaceBefore=10, spaceAfter=6)
    q_style = ParagraphStyle("QX", parent=styles["Normal"], spaceBefore=8, spaceAfter=2)
    muted_style = ParagraphStyle("MutedX", parent=styles["Normal"], textColor=colors.HexColor("#666666"), fontSize=9)

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Exam Result - {session.get('session_id')}",
    )

    story = []
    story.append(Paragraph("Exam Result", title_style))
    story.append(
        Paragraph(
            f"Session {session.get('session_id')} &nbsp;·&nbsp; "
            f"Submitted {session.get('completed_at')}",
            sub_style,
        )
    )

    qs = session.get("questions") or []
    graded = session.get("graded") or []

    if session.get("score") is not None:
        story.append(
            Paragraph(
                f"<font size=16><b>{session.get('score')} / {session.get('total_selected')}</b></font>",
                h2_style,
            )
        )
        story.append(Paragraph("Auto-graded multiple-choice score", muted_style))
    else:
        story.append(Paragraph("<b>Awaiting review</b>", h2_style))
        story.append(
            Paragraph(
                "This exam contains essay/coding answers which require manual review.",
                muted_style,
            )
        )

    story.append(Spacer(1, 8))
    story.append(Paragraph("Answer Review", h2_style))

    for i, q in enumerate(qs):
        qtype = q.get("type", "mcq")
        g = graded[i] if i < len(graded) else {}
        block = []
        block.append(Paragraph(f"Q{i + 1}. {q.get('text', '')} [{qtype.upper()}]", q_style))

        if qtype == "mcq":
            options = q.get("options") or []
            correct_idx = g.get("correct_index")
            selected = g.get("selected")
            if g.get("correct"):
                status_txt = '<font color="#198754"><b>Correct</b></font>'
            elif g.get("correct") is not None:
                status_txt = '<font color="#dc3545"><b>Incorrect</b></font>'
            else:
                status_txt = ""
            block.append(Paragraph(status_txt, muted_style))

            opt_lines = []
            for oi, opt in enumerate(options):
                marker = "•"
                color = "#222222"
                suffix = ""
                if oi == correct_idx:
                    marker = "✔"
                    color = "#198754"
                    suffix = " — Correct answer"
                elif oi == selected:
                    marker = "✘"
                    color = "#dc3545"
                    suffix = " — Your answer"
                opt_lines.append(
                    Paragraph(
                        f'<font color="{color}">{marker} {opt}</font>{suffix}',
                        ParagraphStyle("opt", parent=styles["Normal"], fontSize=10, spaceAfter=1),
                    )
                )
            block.extend(opt_lines)
            if selected is None:
                block.append(Paragraph("⚠️ Not answered", muted_style))
        else:
            block.append(Paragraph("Answer:", muted_style))
            answer_txt = g.get("selected") or "⚠️ Not answered"
            block.append(
                Paragraph(
                    answer_txt.replace("\n", "<br/>"),
                    ParagraphStyle("aw", parent=styles["Normal"], borderPadding=6, backColor=colors.HexColor("#fafafa")),
                )
            )
            block.append(Paragraph("Awaiting manual review", muted_style))

        story.append(KeepTogether(block))
        story.append(Spacer(1, 8))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


def _build_details_pdf(session: dict, custom_registration_fields: list) -> bytes:
    """
    Build a real PDF for the HOST session-details view using ReportLab.
    Mirrors details_pdf.html: status, student info, score, per-question
    submitted answers with the correct answers highlighted.
    """
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], spaceAfter=4)
    sub_style = ParagraphStyle("SubX", parent=styles["Normal"], textColor=colors.HexColor("#666666"), spaceAfter=14)
    h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], spaceBefore=10, spaceAfter=6)
    q_style = ParagraphStyle("QX", parent=styles["Normal"], spaceBefore=8, spaceAfter=2)
    muted_style = ParagraphStyle("MutedX", parent=styles["Normal"], textColor=colors.HexColor("#666666"), fontSize=9)

    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Session Details - {session.get('session_id')}",
    )
    story = []
    story.append(Paragraph("Session Details", title_style))
    story.append(
        Paragraph(
            f"Session {session.get('session_id')} &nbsp;·&nbsp; Status: {session.get('status', '').upper()}",
            sub_style,
        )
    )

    # --- Registered student ---
    story.append(Paragraph("Registered Student", h2_style))
    student = session.get("student")
    if student:
        rows = [["Field", "Value"]]
        if student.get("name"):
            rows.append(["Name", student.get("name")])
        if student.get("phone"):
            rows.append(["Phone", student.get("phone")])
        for cf in custom_registration_fields:
            slug = cf.get("slug")
            if student.get(slug):
                rows.append([cf.get("name", slug), student.get(slug)])
        if student.get("registered_at"):
            rows.append(["Registered At", student.get("registered_at")])
        t = Table(rows, colWidths=[45 * mm, 135 * mm])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f5f5f5")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(t)
    else:
        story.append(Paragraph("Not registered yet.", muted_style))

    # --- Score ---
    if session.get("score") is not None:
        story.append(
            Paragraph(
                f"<b>Score:</b> {session.get('score')} / {session.get('total_selected')}",
                h2_style,
            )
        )
    elif session.get("status") == "completed":
        story.append(Paragraph("<b>Score:</b> Awaiting manual review", h2_style))

    # --- Submitted answers ---
    story.append(Paragraph("Submitted Answers", h2_style))
    qs = session.get("questions") or []
    graded = session.get("graded") or []

    if graded:
        for i, q in enumerate(qs):
            qtype = q.get("type", "mcq")
            g = graded[i] if i < len(graded) else {}
            block = []
            block.append(Paragraph(f"Q{i + 1}. {q.get('text', '')} [{qtype.upper()}]", q_style))

            if qtype == "mcq":
                options = q.get("options") or []
                correct_idx = g.get("correct_index")
                selected = g.get("selected")
                opt_lines = []
                for oi, opt in enumerate(options):
                    marker = "•"
                    color = "#222222"
                    suffix = ""
                    if correct_idx is not None and oi == correct_idx:
                        marker = "✔"
                        color = "#198754"
                        suffix = " — Correct answer"
                    elif selected is not None and oi == selected:
                        marker = "✘"
                        color = "#dc3545"
                        suffix = " — Student's choice"
                    opt_lines.append(
                        Paragraph(
                            f'<font color="{color}">{marker} {opt}</font>{suffix}',
                            ParagraphStyle("opt", parent=styles["Normal"], fontSize=10, spaceAfter=1),
                        )
                    )
                block.extend(opt_lines)
                if selected is None:
                    block.append(Paragraph("⚠️ Not answered", muted_style))
            else:
                block.append(Paragraph("Answer:", muted_style))
                answer_txt = g.get("selected") or "⚠️ Not answered"
                block.append(
                    Paragraph(
                        answer_txt.replace("\n", "<br/>"),
                        ParagraphStyle("aw", parent=styles["Normal"], borderPadding=6, backColor=colors.HexColor("#fafafa")),
                    )
                )
                block.append(Paragraph("Awaiting manual review", muted_style))

            story.append(KeepTogether(block))
            story.append(Spacer(1, 8))
    else:
        story.append(Paragraph("No submission yet.", muted_style))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


def _pdf_bytes(template_name: str, **ctx) -> bytes | None:
    """
    Return the PDF bytes for the given template. Returns None if ReportLab
    is unavailable or fails, so the caller can use the HTML fallback.
    """
    if not _REPORTLAB_AVAILABLE:
        return None
    try:
        if template_name == "result_pdf.html":
            return _build_result_pdf(ctx.get("session") or {})
        if template_name == "details_pdf.html":
            return _build_details_pdf(
                ctx.get("session") or {},
                ctx.get("custom_registration_fields") or [],
            )
        return None
    except Exception:
        audit("pdf_generation_failed", template=template_name)
        return None


def pdf_response(template_name: str, download_name: str, **ctx):
    """
    Render a template into a downloadable PDF attachment.

    Uses ReportLab (pure Python, no system binary) to generate a real PDF so
    it works on ANY hosting provider. If ReportLab is unavailable or throws
    at render time, we gracefully fall back to the print-friendly HTML view —
    the app never crashes.
    """
    pdf_data = _pdf_bytes(template_name, **ctx)
    if pdf_data is None:
        return _pdf_fallback(template_name, **ctx)

    from flask import Response

    return Response(
        pdf_data,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={download_name}",
        },
    )


# ---------------------------------------------------------------------------
# Registration configuration — fields the host can require from each student
# ---------------------------------------------------------------------------
REG_FIELD_LABELS = {
    "name": "Full Name",
    "phone": "Phone Number",
    "address": "Address",
    "department": "Department",
}
DEFAULT_REQUIRED_FIELDS = ["name", "phone"]

# Strict server-side validation (enforced even if the frontend is bypassed).
#   * name  -> alphabetic characters only (no numbers anywhere)
#   * phone -> exactly 10 digits
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s.'-]*$")
PHONE_RE = re.compile(r"^\d{10}$")


def slugify(text: str) -> str:
    """
    Convert an arbitrary display label ("My Field!") into a safe input name
    ("my_field"). This lets the host ask ANY question while the system keeps
    a predictable, HTML-safe key to bind the form input to the stored value.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return slug or "field"


def validate_registration_fields(fields: dict) -> str | None:
    """
    Validate a submitted registration dict.

    Returns an error message string when validation fails, otherwise None.
      * name  must be alphabetic only (letters + spaces + . ' - separators)
      * phone must be exactly 10 digits
    """
    name = fields.get("name", "")
    if name and not NAME_RE.match(name):
        return "Name may only contain alphabetic characters (no numbers)."

    phone = fields.get("phone", "")
    if phone and not PHONE_RE.match(phone):
        return "Phone number must be exactly 10 digits."

    return None


# ---------------------------------------------------------------------------
# Randomizer — the heart of the per-candidate exam
# ---------------------------------------------------------------------------
def randomize_questions(bank: list, ratio: int) -> list:
    """
    Build the exact question subset a single candidate will receive.

    The host sets a "question ratio": the number of random questions a
    candidate gets from the master bank. This function:

      1. Clamps the ratio so it is always at least 1 and never exceeds the
         size of the bank (we cannot hand out more questions than exist).
      2. Uses random.sample() to draw `ratio` questions WITHOUT repetition,
         so the same question never appears twice in one exam.
      3. Shuffles the selected questions, so each candidate sees the
         questions in a different order.
      4. Deep-copies each question and shuffles its answer options, then
         recomputes `correct_index` against the shuffled order.

    Why deep-copy? The selected questions initially reference the SAME dict
    objects as the master bank. If we shuffled their options in place we
    would corrupt the master bank for every future session. Deep-copying
    guarantees every session is fully isolated.

    Supports three question types (each deep-copied to isolate the session):
      * mcq    — multiple-choice: options are shuffled and `correct_index`
                 is recomputed against the shuffled order.
      * essay  — free-text answer (textarea); passed through untouched.
      * coding — code answer (textarea); passed through untouched.

    Returns a list of question dicts ready to store on the session:
        [{ id, type, text, options (shuffled, mcq only),
           correct_index (updated, mcq only) }, ...]
    """
    # 1) Sanity-clamp the ratio against the available bank size.
    safe_ratio = max(1, min(int(ratio), len(bank)))

    # 2) Random subset drawn without repetition.
    selected = random.sample(bank, safe_ratio)

    # 3) Random question order per session.
    random.shuffle(selected)

    # 4) Isolate each question and, for MCQ, shuffle the answer options.
    shuffled_questions = []
    for q in selected:
        q = copy.deepcopy(q)
        q.setdefault("type", "mcq")  # legacy questions default to mcq
        if q["type"] == "mcq":
            correct_answer = q["options"][q["correct_index"]]
            random.shuffle(q["options"])
            q["correct_index"] = q["options"].index(correct_answer)
        shuffled_questions.append(q)

    return shuffled_questions


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/favicon.ico")
def favicon():
    """Serve the site logo as /favicon.ico so browsers pick it up correctly.

    Placed at the very top of the route definitions so no wildcard / catch-all
    route can intercept the browser's automatic favicon request.
    """
    import os
    from flask import send_from_directory
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "logo.png",
        mimetype="image/png",
    )


@app.route("/")
def index():
    """Root — if logged in, go to the dashboard; otherwise the login page."""
    if current_host() is not None:
        return redirect(url_for("host"))
    return redirect(url_for("host_login"))


@app.route("/privacy")
def privacy():
    """Privacy policy & legal liability disclaimer (public)."""
    return render_template("privacy.html")


# --- Host registration -----------------------------------------------------
@app.route("/host/register", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def host_register():
    """
    Create a new host account. Requires email verification via OTP.
    """
    error = None
    step = session.get("reg_step", "input") # 'input' or 'verify'

    if request.method == "POST":
        if step == "input":
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            captcha = request.form.get("captcha", "")

            if not EMAIL_RE.match(email):
                error = "Please enter a valid email address."
            elif not name:
                error = "Please enter your full name."
            elif not password_ok(password):
                error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
            elif password != confirm:
                error = "Passwords do not match."
            elif find_host(email) is not None:
                error = "An account with that email already exists."
            elif not verify_captcha(captcha):
                error = "Incorrect CAPTCHA answer. Please try again."
            elif request.form.get("agree") != "1":
                error = "You must accept the Terms & Privacy Policy to register."
            else:
                # Store pending data in session
                session["reg_pending"] = {
                    "email": email,
                    "name": name,
                    "password_hash": hash_password(password)
                }
                # Issue OTP
                issue_otp(email)
                session["reg_step"] = "verify"
                return redirect(url_for("host_register"))

        elif step == "verify":
            # Action: 'verify' or 'resend'
            action = request.form.get("action", "verify")
            pending = session.get("reg_pending")
            
            if not pending:
                session.pop("reg_step", None)
                return redirect(url_for("host_register"))

            if action == "resend":
                issue_otp(pending["email"])
                flash("A new verification code has been sent.")
                return redirect(url_for("host_register"))
            
            code = request.form.get("otp", "").strip()
            if verify_otp(pending["email"], code):
                # Success: Create the host
                host = HostUser(
                    email=pending["email"],
                    name=pending["name"],
                    password_hash=pending["password_hash"],
                )
                db.session.add(host)
                db.session.commit()
                
                audit("host_registered", email=pending["email"], name=pending["name"], ip=get_remote_address())
                log_user_login(host, "REGISTER")
                
                email = pending["email"]
                session.clear() # Clear registration data
                session[SESSION_HOST_EMAIL] = email
                return redirect(url_for("host"))
            else:
                error = "Invalid or expired verification code."

    # Render based on current step
    if step == "verify":
        pending = session.get("reg_pending")
        if not pending:
            session.pop("reg_step", None)
            return redirect(url_for("host_register"))
        
        return render_template(
            "host_register_verify.html",
            email=pending["email"],
            error=error
        )

    return render_template(
        "host_register.html",
        error=error,
        captcha=captcha_payload(),
        form=request.form,
    )
# --- Host login ------------------------------------------------------------
@app.route("/host/login", methods=["GET", "POST"])
@limiter.limit("20 per minute", methods=["POST"])
def host_login():
    """
    Log a host in. Requires their email + password and a correct CAPTCHA.
    After success the user is redirected back to the page they were trying
    to reach (`next`), or to the dashboard.
    """
    error = None
    step = session.get("login_step", "input")

    if request.method == "POST":
        if step == "input":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            captcha = request.form.get("captcha", "")
            host = find_host(email)

            if host is None or not verify_password(password, host.password_hash):
                error = "Invalid email or password."
                audit("host_login_failed", email=email, ip=get_remote_address())
            elif not verify_captcha(captcha):
                error = "Incorrect CAPTCHA answer. Please try again."
            elif request.form.get("agree") != "1":
                error = "You must accept the Terms & Privacy Policy to log in."
            else:
                session["login_pending"] = host.email
                issue_otp(host.email, purpose="login")
                session["login_step"] = "verify"
                return redirect(url_for("host_login"))

        elif step == "verify":
            action = request.form.get("action", "verify")
            email = session.get("login_pending")

            if not email:
                session.pop("login_step", None)
                return redirect(url_for("host_login"))

            if action == "resend":
                issue_otp(email, purpose="login")
                flash("A new verification code has been sent.")
                return redirect(url_for("host_login"))

            code = request.form.get("otp", "").strip()
            if verify_otp(email, code, purpose="login"):
                session[SESSION_HOST_EMAIL] = email
                session.pop("login_step", None)
                session.pop("login_pending", None)

                host = find_host(email)
                if host:
                    audit("host_login", email=host.email, ip=get_remote_address())
                    log_user_login(host, "LOGIN")

                next_url = request.args.get("next") or request.form.get("next")
                if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                    return redirect(next_url)
                return redirect(url_for("host"))
            else:
                error = "Invalid or expired verification code."

    if step == "verify":
        email = session.get("login_pending")
        if not email:
            session.pop("login_step", None)
            return redirect(url_for("host_login"))
        return render_template("host_login_verify.html", email=email, error=error)

    next_url = request.args.get("next", "")
    return render_template(
        "host_login.html",
        error=error,
        captcha=captcha_payload(),
        next=next_url,
        form=request.form,
    )

@app.route("/host/logout", methods=["POST"])
def host_logout():
    """End the host session."""
    email = session.pop(SESSION_HOST_EMAIL, None)
    if email:
        audit("host_logout", email=email, ip=get_remote_address())
    next_url = request.args.get("next") or request.form.get("next")
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("host_login"))


# --- Host forgot / reset password (OTP) -------------------------------------
@app.route("/host/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def host_forgot_password():
    """
    Request a password-reset OTP. If the email matches a registered host, an
    OTP is issued (delivered by email if SMTP is configured, otherwise logged
    to the console/audit log). We always show the same message whether or not
    the account exists, to avoid leaking which emails are registered.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        captcha = request.form.get("captcha", "")

        # Always verify CAPTCHA first to block bots and prevent email enumeration.
        if not verify_captcha(captcha):
            return render_template(
                "host_forgot.html",
                sent=False,
                captcha=captcha_payload(),
                error="Incorrect CAPTCHA answer. Please try again.",
            )

        if EMAIL_RE.match(email) and find_host(email) is not None:
            issue_otp(email)
            audit("forgot_password_requested", email=email, ip=get_remote_address())

        # Always return the same success message to avoid account enumeration.
        return render_template(
            "host_forgot.html",
            sent=True,
            captcha=captcha_payload(),
            error=None,
        )

    return render_template(
        "host_forgot.html",
        sent=False,
        captcha=captcha_payload(),
        error=None,
    )
@app.route("/host/reset-password", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def host_reset_password():
    """
    Verify the OTP + reset the host's password. Requires a valid email, a
    correct OTP, a matching new password meeting the policy, and a CAPTCHA.
    """
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        code = request.form.get("otp", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        captcha = request.form.get("captcha", "")

        # 1. Validation check
        if not verify_captcha(captcha):
            error = "Incorrect CAPTCHA answer. Please try again."
        elif not email or not code:
            error = "Email and OTP code are required."
        elif not password_ok(password):
            error = f"Password must be at least {MIN_PASSWORD_LEN} characters."
        elif password != confirm:
            error = "Passwords do not match."
    else:
            # 2. OTP and Host verification
            host = find_host(email)
            if host is None:
                # Generic error to avoid leakage
                error = "Invalid reset request."
            elif not verify_otp(email, code):
                error = "Invalid or expired OTP code."
            else:
                host.password_hash = hash_password(password)
                db.session.commit()
                audit("host_password_reset", email=email, ip=get_remote_address())
                return render_template(
                    "host_reset.html",
                    success=True,
                    error=None,
                    captcha=captcha_payload(),
                )

    return render_template(
        "host_reset.html",
        success=False,
        error=error,
        captcha=captcha_payload(),
    )



@app.route("/host")
@login_required
def host():
    """
    Host Dashboard — question bank, exam configuration, generated sessions.

Per-host data isolation: a host only ever sees the exam sessions and
    questions they created (filtered by their own email). No host can see
another host's exams or questions.
    """
    host_user = current_host()
    host_email = host_user.email if host_user else None
    bank_questions = (
        Question.query.filter_by(host_email=host_email)
        .order_by(Question.created_at.desc())
        .all()
    )
    exams = (
        Exam.query.filter_by(host_email=host_email)
        .order_by(Exam.created_at.desc())
        .all()
    )
    return render_template(
        "host.html",
        questions=[bank_question_to_dict(q) for q in bank_questions],
        exams=[exam_to_dict(x) for x in exams],
        field_labels=REG_FIELD_LABELS,
        host=host_user,
    )


@app.route("/host/question", methods=["POST"])
@limiter.limit("10000 per minute", methods=["POST"])
@login_required
def add_question():
    """Add one question to the master bank (host can add an unlimited number).

    Supports three types selected via the `type` form field:
      * mcq    — requires >= 2 options and a correct_index (auto-graded)
      * essay  — free-text answer, graded manually by the host
* coding — code answer, graded manually by the host
    """
    text_val = request.form.get("text", "").strip()
    qtype = request.form.get("type", "mcq")

    # Strict input validation (Marshmallow) — reject malformed question data
    # before it reaches the database.
    question_err = validate_question_payload(
        {
            "text": text_val,
            "type": qtype,
            "options": request.form.getlist("options"),
            "correct_index": request.form.get("correct_index"),
        }
    )
    if question_err:
        return redirect(url_for("host", error="invalid_question"))

    if not text_val:
        return redirect(url_for("host", error="invalid_question"))

    if qtype not in ("mcq", "essay", "coding"):
        qtype = "mcq"

    question = Question(
        id=f"q_{uuid.uuid4().hex[:12]}",
        type=qtype,
        text=text_val,
        host_email=_current_host_email(),
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    if qtype == "mcq":
        options = [o.strip() for o in request.form.getlist("options") if o.strip()]
        if len(options) < 2:
            return redirect(url_for("host", error="invalid_question"))
        try:
            correct_index = int(request.form.get("correct_index", "0"))
        except ValueError:
            correct_index = 0
        if not 0 <= correct_index < len(options):
            correct_index = 0
        question.options = options
        question.correct_index = correct_index

    db.session.add(question)
    db.session.commit()
    return redirect(url_for("host"))


@app.route("/host/question/<qid>/delete", methods=["POST"])
@login_required
def delete_question(qid):
    """Remove a question from the master bank.

    Ownership-guarded: only the host who created this question may delete it.
    """
    question = db.session.get(Question, qid)
    if question and question.host_email == _current_host_email():
        db.session.delete(question)
        db.session.commit()
    return redirect(url_for("host"))


@app.route("/clear_questions", methods=["POST"])
@login_required
def clear_questions():
    """Remove ALL of THIS host's questions from the master bank (destructive — cannot be undone).

    Per-host isolation: only this host's own questions are deleted. Other
    hosts' questions are never touched. The host is prompted for confirmation
    client-side before this route runs.
    """
    host_email = _current_host_email()
    Question.query.filter_by(host_email=host_email).delete(
        synchronize_session=False
    )
    db.session.commit()
    audit("bank_cleared", host_email=host_email, ip=get_remote_address())
    return redirect(url_for("host"))


@app.route("/host/clear-submissions", methods=["POST"])
@login_required
def clear_submissions():
    """
    Reset Exam Data — delete ALL student submissions & generated exam
    sessions while keeping the master Question Bank completely untouched.

    Deletes (in FK-safe order):
      * answers            — every submitted response
      * session_questions  — the dealt-question snapshots
      * students           — all candidate registrations
      * sessions           — all generated exam links

    Keeps:
      * questions          — the host's master question bank
      * host configuration (lives in application code, not worth deleting)

    This is a destructive, irreversible operation. The host is prompted for
    confirmation client-side before this route can run, and the action is
audited server-side.
    """
    # Per-host isolation: only clear THIS host's exam sessions and their
    # child records (answers, session_questions, students). Other hosts'
    # data is never touched.
    host_email = _current_host_email()
    if not host_email:
        return redirect(url_for("host_login"))

    my_sessions = [s.id for s in Session.query.filter_by(host_email=host_email).all()]

    # Order matters: children must be deleted before parents.
    n_answers = 0
    n_session_questions = 0
    n_students = 0
    n_sessions = 0
    if my_sessions:
        n_answers = Answer.query.filter(Answer.session_id.in_(my_sessions)).delete(synchronize_session=False)
        n_session_questions = SessionQuestion.query.filter(
            SessionQuestion.session_id.in_(my_sessions)
        ).delete(synchronize_session=False)
        n_students = Student.query.filter(Student.session_id.in_(my_sessions)).delete(synchronize_session=False)
        n_sessions = Session.query.filter(Session.id.in_(my_sessions)).delete(synchronize_session=False)
    else:
        n_sessions = 0
        db.session.commit()
        audit(
        "submissions_cleared",
        host_email=host_email,
        answers=n_answers,
        session_questions=n_session_questions,
        students=n_students,
        sessions=n_sessions,
                ip=get_remote_address(),
            )


    # Redirect back to the dashboard with a visible success message.
    return redirect(url_for("host", msg="submissions_cleared"))
@app.route("/host/generate", methods=["POST"])
@limiter.limit("10000 per minute", methods=["POST"])
@login_required

def generate_session():
    """
    Create a brand-new Exam — the UNIVERSAL, reusable entry point.

    Flow:
      1. Read the config (total time minutes + question ratio).
      2. Snapshot the FULL question bank into this Exam's `exam_questions`
         (so later edits to the bank never affect the exam).
      3. Store the Exam keyed by a unique exam_id (the shareable token).
      4. Redirect back to the dashboard, where the reusable portal link
         /exam/register/<exam_id> is displayed for copying.

    Unlike the old design (where one Session = one student), an Exam is a
    single shared link that ANY number of students can open. Each student who
    registers gets their OWN isolated Session (attempt) under this Exam, so
    100+ students can take the same exam concurrently without overriding one
    another.

    This entire operation happens inside a single database transaction, so
    the snapshot + Exam creation are atomically persisted.
    """
    host_email = _current_host_email()
    bank_questions = (
            Question.query.filter_by(host_email=host_email)
            .order_by(Question.created_at.desc())
            .all()
        )
    if not bank_questions:
        return redirect(url_for("host", error="empty_bank"))

    # Mandatory legal disclaimer — the host must explicitly agree to the
    # Terms & Privacy Policy before generating an exam (they are responsible
    # for how the exams and materials are used).
    if request.form.get("agree") != "1":
        audit("exam_generation_denied", reason="policy_not_accepted", ip=get_remote_address())
        return redirect(url_for("host", error="must_agree"))

    try:
        time_limit = max(1, min(240, int(request.form.get("time_limit", "30") or 30)))
        ratio = max(1, int(request.form.get("ratio", str(len(bank_questions))) or len(bank_questions)))
    except ValueError:
        return redirect(url_for("host", error="invalid_config"))


    # The exam capacity is permanently hardcoded to MAX_SUBMISSIONS (500).
    max_capacity = MAX_SUBMISSIONS

    exam_id = uuid.uuid4().hex[:16]

    # ---- Dynamic Form Builder ------------------------------------------
    cf_names = request.form.getlist("cf_name")
    cf_required = request.form.getlist("cf_required")
    cf_slugs = request.form.getlist("cf_slug")

    custom_registration_fields = []
    seen_slugs = set()
    for i, raw_name in enumerate(cf_names):
        name = raw_name.strip()
        if not name:
            continue
        required = (cf_required[i] if i < len(cf_required) else "") in (
            "true",
            "on",
            "1",
        )
        slug = slugify(name)
        if slug in seen_slugs:
            slug = f"{slug}_{i}"
        seen_slugs.add(slug)
        custom_registration_fields.append(
            {"name": name, "required": required, "slug": slug}
    )

    required_fields = list(DEFAULT_REQUIRED_FIELDS)


    # --- Build the Exam object (the universal entry point) ---
    # The custom Exam Title / Heading is stored on the Exam's config JSON so
    # the registration portal and every student's exam page show it.
    exam_title = request.form.get("exam_title", "").strip()
    host_user = current_host()
    ex = Exam(
        id=exam_id,
        host_email=(host_user.email if host_user else None),
        config={
            "exam_title": exam_title,
            "time_limit_minutes": time_limit,
            "ratio": ratio,
            "max_capacity": max_capacity,
            "custom_registration_fields": custom_registration_fields,
            "required_fields": required_fields,
        },
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.session.add(ex)

    # --- Snapshot the FULL question bank into ExamQuestion rows ---
    # Each student's attempt later re-samples `ratio` random questions from
    # this immutable snapshot, so every attempt is isolated and the bank can
    # never be corrupted or affected by later edits.
    bank_dicts = [bank_question_to_dict(q) for q in bank_questions]
    for pos, q in enumerate(bank_dicts):
        eq = ExamQuestion(
            exam_id=exam_id,
            position=pos,
            question_id=q.get("id"),
            type=q.get("type", "mcq"),
            text=q["text"],
            options=q.get("options"),
            correct_index=q.get("correct_index"),
        )
        db.session.add(eq)

    db.session.commit()

    audit(
        "exam_generated",
        exam_id=exam_id,
        time_limit=time_limit,
        ratio=ratio,
        bank_size=len(bank_questions),
        ip=get_remote_address(),
    )
    if ex.host_email:
        write_tests_conducted(ex.host_email)
    return redirect(url_for("host"))


@app.route("/exam/register/<exam_id>", methods=["GET", "POST"])
@limiter.limit("10000 per minute", methods=["POST"])
def exam_register(exam_id):
    """
    UNIVERSAL Entry Point — the shared registration portal for an Exam.
    """
    from sqlalchemy.exc import IntegrityError

    ex = db.session.get(Exam, exam_id)
    if ex is None:
        return redirect(url_for("host", error="session_not_found"))

    cfg = ex.config or {}
    required_fields = [
        f
        for f in (cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS)
        if f in REG_FIELD_LABELS
    ]
    custom_fields = cfg.get("custom_registration_fields") or []
    time_limit = int(cfg.get("time_limit_minutes", 30) or 30)
    ratio = int(cfg.get("ratio", 0) or 0)

    template_vars = {
        "exam_id": exam_id,
        "exam_title": cfg.get("exam_title", ""),
        "required_fields": required_fields,
        "field_labels": REG_FIELD_LABELS,
        "custom_registration_fields": custom_fields,
    }

    # --- Capacity gate ---
    max_cap = MAX_SUBMISSIONS
    if completed_submissions_count(ex.host_email) >= max_cap:
        # ... existing capacity logic ...
        err_msg = capacity_error(max_cap)
        audit("capacity_reached", exam_id=exam_id, step="register", reason="max_submissions", max_capacity=max_cap, ip=get_remote_address())
        if request.method == "POST" and request.is_json:
            return jsonify({"error": err_msg}), 400
        return (render_template("register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars), 400 if request.method == "POST" else 200)

    if request.method == "POST":
        student_data = {}
        missing = []

        # 1. Check for duplicate phone number for THIS specific exam
        phone_val = request.form.get("phone", "").strip()
        if phone_val:
            existing = (
                Student.query.join(Session)
                .filter(Session.exam_id == exam_id, Student.phone == phone_val)
                .first()
            )
            if existing:
                err = "You have already registered or completed this exam."
                audit("registration_failed", exam_id=exam_id, phone=phone_val, reason="duplicate_phone", ip=get_remote_address())
                if request.is_json:
                    return jsonify({"error": err}), 400
                return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400

        # --- core fields (name / phone) ---------------------------------
        for field in required_fields:
            value = request.form.get(field, "").strip()
            if not value:
                missing.append(REG_FIELD_LABELS.get(field, field))
            if field == "name" and len(value) > 35:
                err = "Full Name cannot exceed 35 characters."
                return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400
            student_data[field] = value

        # --- custom dynamic fields ---------------------------------------
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            value = request.form.get(slug, "").strip()
            if cf.get("required") and not value:
                missing.append(cf.get("name", slug))
            if len(value) > 30:
                err = f"{cf.get('name', slug)} cannot exceed 30 characters."
                return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400
            student_data[slug] = value

        if missing:
            error = f"Missing required field(s): {', '.join(missing)}"
            audit("registration_failed", exam_id=exam_id, reason="missing_fields", ip=get_remote_address())
            if request.is_json:
                return jsonify({"error": error}), 400
            return (
                render_template(
                    "register.html", error=error, form=request.form,
                    captcha=captcha_payload(), **template_vars,
                ),
                400,
            )

        # Server-side validation — reject invalid names / phones with 400.
        err = validate_registration_fields(student_data)
        if err:
            audit("registration_failed", exam_id=exam_id, reason="invalid_value", error=err, ip=get_remote_address())
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # CAPTCHA — blocks automated bots from spamming the registration form.
        if not verify_captcha(request.form.get("captcha", "")):
            err = "Incorrect CAPTCHA answer. Please try again."
            audit("registration_failed", exam_id=exam_id, reason="captcha_failed", ip=get_remote_address())
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # Mandatory legal disclaimer — the candidate must explicitly accept the
        # privacy policy & liability disclaimer before the exam can start.
        if request.form.get("agree") != "1":
            err = "You must accept the Privacy Policy & Liability Disclaimer to continue."
            audit("registration_failed", exam_id=exam_id, reason="policy_not_accepted", ip=get_remote_address())
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # --- Build the custom_fields JSON blob ---
        custom_vals = {}
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            if slug in student_data:
                custom_vals[slug] = student_data[slug]

        # Generate OTP code for the student's phone verification step
        otp_code = f"{secrets.randbelow(1000000):06d}"
        print(f"\n[STUDENT OTP DEBUG] Phone: {phone_val} | Code: {otp_code}\n")

        # Store pending registration details in the session
        session["student_pending"] = {
            "exam_id": exam_id,
            "name": student_data.get("name", ""),
            "phone": phone_val,
            "custom_fields": custom_vals,
            "otp_code": otp_code,
            "otp_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        }

        return redirect(url_for("exam_verify", exam_id=exam_id))
    return render_template(
        "register.html", error=None, form={}, captcha=captcha_payload(), **template_vars
    )

@app.route("/exam/verify/<exam_id>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def exam_verify(exam_id):
    """
    OTP Verification page for student phone number.
    """
    from sqlalchemy.exc import IntegrityError

    pending = session.get("student_pending")
    if not pending or pending.get("exam_id") != exam_id:
        return redirect(url_for("exam_register", exam_id=exam_id))
    ex = db.session.get(Exam, exam_id)
    if ex is None:
        return redirect(url_for("host", error="session_not_found"))

    cfg = ex.config or {}
    time_limit = int(cfg.get("time_limit_minutes", 30) or 30)
    ratio = int(cfg.get("ratio", 0) or 0)
    max_cap = MAX_SUBMISSIONS

    error = None
    if request.method == "POST":
        action = request.form.get("action", "verify")

        if action == "resend":
            otp_code = f"{secrets.randbelow(1000000):06d}"
            print(f"\n[STUDENT OTP RESEND DEBUG] Phone: {pending['phone']} | Code: {otp_code}\n")
            pending["otp_code"] = otp_code
            pending["otp_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            session["student_pending"] = pending
            flash("A new verification code has been sent.")
            return redirect(url_for("exam_verify", exam_id=exam_id))

        submitted_code = request.form.get("otp", "").strip()
        expires_at = datetime.fromisoformat(pending.get("otp_expires_at", datetime.now(timezone.utc).isoformat()))

        if datetime.now(timezone.utc) > expires_at:
            error = "Verification code has expired. Please request a new one."
        elif submitted_code != pending.get("otp_code"):
            error = "Invalid verification code. Please try again."
    else:
            # Code is valid! Create the student's isolated Session and Student record.
            session_id = uuid.uuid4().hex[:16]
            now = datetime.now(timezone.utc)
            s = Session(
                id=session_id,
                exam_id=exam_id,
                host_email=ex.host_email,
                status="registered",
                config={
                    "exam_title": cfg.get("exam_title", ""),
                    "time_limit_minutes": time_limit,
                    "ratio": ratio,
                    "max_capacity": max_cap,
                    "custom_registration_fields": cfg.get("custom_registration_fields", []),
                    "required_fields": cfg.get("required_fields", DEFAULT_REQUIRED_FIELDS),
                },
                created_at=now.isoformat(),
            )
            db.session.add(s)

            exam_snapshot = list(ex.exam_questions)
            if exam_snapshot:
                bank_dicts = [
                    {
                        "id": eq.question_id or f"pos_{eq.position}",
                        "type": eq.type or "mcq",
                        "text": eq.text,
                        "options": eq.options,
                        "correct_index": eq.correct_index,
                    }
                    for eq in exam_snapshot
                ]
                randomized = randomize_questions(bank_dicts, ratio)
                for pos, q in enumerate(randomized):
                    sq = SessionQuestion(
                        session_id=session_id,
                        position=pos,
                        question_id=q.get("id"),
                        type=q.get("type", "mcq"),
                        text=q["text"],
                        options=q.get("options"),
                        correct_index=q.get("correct_index"),
                    )
                    db.session.add(sq)

            student = Student(
                session_id=session_id,
                name=pending.get("name"),
                phone=pending.get("phone"),
                custom_fields=pending.get("custom_fields", {}),
                registered_at=now.isoformat(),
                agreed_to_policy=True,
                agreed_at=now.isoformat(),
            )
            db.session.add(student)

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                audit("registration_race_lost", exam_id=exam_id, session_id=session_id, ip=get_remote_address())
                return redirect(url_for("exam_register", exam_id=exam_id))
            audit(
                "registration_success",
                exam_id=exam_id,
                session_id=session_id,
                name=pending.get("name"),
                ip=get_remote_address(),
            )
            if s.host_email:
                write_tests_conducted(s.host_email)

            # Clear pending session and store authorized session_id
            session.pop("student_pending", None)
            session[f"auth_{session_id}"] = True

            return redirect(url_for("exam", session_id=session_id))

    return render_template(
        "exam_verify.html",
        exam_id=exam_id,
        phone=pending.get("phone"),
        error=error
    )

@app.route("/exam/<session_id>")
def exam(session_id):
    """
    Candidate Exam Page — dynamic route backed by the session_id in the URL.

    The generated URL (/exam/<session_id>) hits this route. We look the id
    up in the database; if it does not exist we bounce the user back to the
    dashboard (security: no page is ever rendered for unknown/invalid ids).
    """
    s = db.session.get(Session, session_id)

    if s is None:
        return redirect(url_for("host", error="session_not_found"))

    # Already finished -> show the result page instead of the exam again.
    if s.status == "completed":
        return render_template("result.html", session=session_to_dict(s))

    # Verification Enforced: check if authorized session_id matches
    if not session.get(f"auth_{session_id}"):
        return redirect(url_for("exam_register", exam_id=s.exam_id))

    # REGISTRATION GATE: force the student to register before the exam starts.
    # If this session belongs to an Exam, send them to the exam's universal
    # portal; otherwise fall back to the legacy single-session registration.
    if s.student is None:
        if s.exam_id:
            return redirect(url_for("exam_register", exam_id=s.exam_id))
        return redirect(url_for("register", session_id=session_id))

    # First visit: start the clock and persist a server-side deadline so the
    # countdown survives page refreshes (a refresh cannot reset the timer).
    now = datetime.now(timezone.utc)
    if s.started_at is None:
        s.started_at = now.isoformat()
        s.deadline = (
            now + timedelta(minutes=s.config["time_limit_minutes"])
        ).isoformat()
        s.status = "started"
        db.session.commit()

    deadline = datetime.fromisoformat(s.deadline)
    remaining_seconds = max(0, int((deadline - now).total_seconds()))

    # Server-anchored timeline (both UTC Unix timestamps). The client timer is
    # driven by these absolute values + periodic re-sync from the server, so a
    # user changing their system clock or refreshing the page CANNOT extend time.
    deadline_unix = int(deadline.timestamp())
    server_now_unix = int(now.timestamp())

    # Security: NEVER send the correct answers to the client. The exam page
    # only receives question text + options; grading happens server-side.
    public_questions = [
        {
            "id": q.question_id or f"pos_{q.position}",
            "type": q.type or "mcq",
            "text": q.text,
            "options": q.options,
        }
        for q in s.questions
    ]

    # Registration field configuration for this session (set by the host when
    # the exam link was generated). Used to dynamically render the student's
    # captured details in the exam header.
    cfg = s.config or {}
    required_fields = [
        f
        for f in (cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS)
        if f in REG_FIELD_LABELS
    ]
    custom_registration_fields = cfg.get("custom_registration_fields") or []

    return render_template(
        "exam.html",
        session_id=session_id,
        exam_title=cfg.get("exam_title", ""),
        questions=public_questions,
        remaining_seconds=remaining_seconds,
        deadline_unix=deadline_unix,
        server_now_unix=server_now_unix,
        time_limit_minutes=s.config["time_limit_minutes"],
        student=student_to_dict(s.student) if s.student else None,
        required_fields=required_fields,
        custom_registration_fields=custom_registration_fields,
        field_labels=REG_FIELD_LABELS,
    )


@app.route("/exam/<session_id>/time")
@limiter.limit("120 per minute")
def exam_time(session_id):
    """
    Server-anchored time endpoint.

    Returns the CURRENT server UTC time and the session's absolute UTC
    deadline (both as Unix timestamps). The client polls this to correct any
    countdown drift and to stay anchored to the server clock — so tampering
    with the browser clock or refreshing the page can never extend the exam.
    """
    s = db.session.get(Session, session_id)
    if s is None or s.status == "completed":
        return jsonify({"error": "invalid or completed session"}), 400

    # If the exam hasn't started yet, there is no deadline to enforce.
    if not s.deadline:
        return jsonify({"error": "exam not started"}), 400

    try:
        deadline = datetime.fromisoformat(s.deadline)
    except (TypeError, ValueError):
        return jsonify({"error": "invalid deadline"}), 400

    now = datetime.now(timezone.utc)
    return jsonify(
        {
            "server_now_unix": int(now.timestamp()),
            "deadline_unix": int(deadline.timestamp()),
        }
    )

@app.route("/exam/<session_id>/register", methods=["GET", "POST"])
@app.route("/register/<session_id>", methods=["GET", "POST"])
@limiter.limit("10000 per minute", methods=["POST"])
def register(session_id):
    """
    Registration Gate — the student must fill in the fields the host selected
    (via `required_fields` on the session) before the exam starts.

    The template loops over `required_fields` to generate inputs dynamically.
    Validation is enforced SERVER-SIDE (even if the frontend is bypassed):
      * name  must be alphabetic only            -> 400 Bad Request
      * phone must be exactly 10 digits          -> 400 Bad Request
    All submitted fields are saved onto the student record.

    Concurrency: A UNIQUE constraint on students.session_id guarantees that
    two simultaneous registrations for the same session cannot both succeed.
    The second INSERT will raise IntegrityError, which we catch and redirect
    the loser to the exam page (already registered).
    """
    from sqlalchemy.exc import IntegrityError

    s = db.session.get(Session, session_id)

    if s is None:
        return redirect(url_for("host", error="session_not_found"))

    # Already registered / completed -> move on.
    if s.student is not None:
        if s.status == "completed":
            return render_template("result.html", session=session_to_dict(s))
        return redirect(url_for("exam", session_id=session_id))

    # Fields this session collects (set by the host when generating the link).
    cfg = s.config or {}
    required_fields = [
        f
        for f in (cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS)
        if f in REG_FIELD_LABELS
    ]
    custom_fields = cfg.get("custom_registration_fields") or []

    template_vars = {
        "session_id": session_id,
        "required_fields": required_fields,
        "field_labels": REG_FIELD_LABELS,
        "custom_registration_fields": custom_fields,
    }

    # --- Capacity gate: block registration once THIS session'S OWNER has
    # reached their maximum number of COMPLETED submissions. ------------
    max_cap = session_max_capacity(s)
    if completed_submissions_count(s.host_email) >= max_cap:
        err_msg = capacity_error(max_cap)
        audit(
            "capacity_reached",
            session_id=session_id,
            step="register",
            reason="max_submissions",
            max_capacity=max_cap,
            ip=get_remote_address(),
        )
        if request.method == "POST" and request.is_json:
            return jsonify({"error": err_msg}), 400
        return (
            render_template(
                "register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars
            ),
            400 if request.method == "POST" else 200,
        )

    if request.method == "POST":
        student_data = {}
        missing = []

        # --- core fields (name / phone) ---------------------------------
        for field in required_fields:
            value = request.form.get(field, "").strip()
            if not value:
                missing.append(REG_FIELD_LABELS.get(field, field))
            student_data[field] = value

        # --- custom dynamic fields ---------------------------------------
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            value = request.form.get(slug, "").strip()
            if cf.get("required") and not value:
                missing.append(cf.get("name", slug))
            student_data[slug] = value


        if missing:
            error = f"Missing required field(s): {', '.join(missing)}"
            audit(
                "registration_failed",
                session_id=session_id,
                reason="missing_fields",
                ip=get_remote_address(),
            )
            if request.is_json:
                return jsonify({"error": error}), 400
            return (
                render_template(
                    "register.html",
                    error=error,
                    form=request.form,
                    captcha=captcha_payload(),
                    **template_vars,
                ),
                400,
            )

        # Server-side validation — reject invalid names / phones with 400.
        err = validate_registration_fields(student_data)
        if err:
            audit(
                "registration_failed",
                session_id=session_id,
                reason="invalid_value",
                error=err,
                ip=get_remote_address(),
            )
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # CAPTCHA — blocks automated bots from spamming the registration form.
        if not verify_captcha(request.form.get("captcha", "")):
            err = "Incorrect CAPTCHA answer. Please try again."
            audit(
                "registration_failed",
                session_id=session_id,
                reason="captcha_failed",
                ip=get_remote_address(),
            )
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # Mandatory legal disclaimer — the candidate must explicitly accept the
        # privacy policy & liability disclaimer before the exam can start.
        if request.form.get("agree") != "1":
            err = "You must accept the Privacy Policy & Liability Disclaimer to continue."
            audit(
                "registration_failed",
                session_id=session_id,
                reason="policy_not_accepted",
                ip=get_remote_address(),
            )
            if request.is_json:
                return jsonify({"error": err}), 400
            return (
                render_template(
                    "register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                400,
            )

        # --- Build the custom_fields JSON blob ---
        # Everything except name/phone/registered_at goes into custom_fields
        custom_vals = {}
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            if slug in student_data:
                custom_vals[slug] = student_data[slug]

        student = Student(
            session_id=session_id,
            name=student_data.get("name", ""),
            phone=student_data.get("phone", ""),
            custom_fields=custom_vals,
            registered_at=datetime.now(timezone.utc).isoformat(),
            agreed_to_policy=True,
            agreed_at=datetime.now(timezone.utc).isoformat(),
        )
        db.session.add(student)

        # Update session status
        if s.status == "pending":
            s.status = "registered"

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # Another concurrent registration won — this is fine.
            audit(
                "registration_race_lost",
                session_id=session_id,
                ip=get_remote_address(),
            )
            return redirect(url_for("exam", session_id=session_id))

        audit(
            "registration_success",
            session_id=session_id,
            name=student_data.get("name", ""),
            ip=get_remote_address(),
        )
        return redirect(url_for("exam", session_id=session_id))

    return render_template(
        "register.html", error=None, form={}, captcha=captcha_payload(), **template_vars
    )

@app.route("/exam/<session_id>/submit", methods=["POST"])
@limiter.limit("10000 per minute", methods=["POST"])
def submit_exam(session_id):
    """
    Save + grade a finished exam.

    Receives JSON: {
      "student": { name, phone, id },  # from the registration gate
      "answers": { question_index: answer }
    }
    Answer format by question type:
      * mcq    -> selected option index (auto-graded vs correct_index)
      * essay  -> free-text answer (saved verbatim, manual review)
      * coding -> code answer (saved verbatim, manual review)

    The student's registration info and their answers are saved TOGETHER on
    the session, so the host can review them from the dashboard.

    Concurrency: The status update is an atomic SQL UPDATE with a WHERE
    clause that only advances the status if it is NOT already 'completed'.
    This is the core fix for the "simultaneous submission" problem: even if
    two Gunicorn workers receive POST /submit for the same session at the
    same microsecond, only one will see rowcount > 0 and proceed to grade.
    The loser gets a 400 error and the single correct grade is preserved.
    """
    # ---- INPUT VALIDATION (Marshmallow) ---------------------------------
    # Strictly validate the incoming JSON BEFORE any database write. Rejects
    # malformed payloads and unexpected fields with a 400, so untrusted data
    # never reaches the grading/storage logic.
    payload = request.get_json(silent=True) or {}
    validation_error = validate_submit_payload(payload)
    if validation_error:
        audit(
            "submit_rejected",
            session_id=session_id,
            reason="invalid_payload",
            error=validation_error,
            ip=get_remote_address(),
        )
        return jsonify({"error": validation_error}), 400

    # Load the session and its capacity limit up-front so we can build a
    # single atomic gate that couples the "already submitted" check with the
    # "capacity reached" check.
    s = db.session.get(Session, session_id)
    if s is None:
        return jsonify({"error": "invalid or already submitted"}), 400
    max_cap = session_max_capacity(s)

    # ---- DEADLINE ENFORCEMENT (server-side, tamper-proof) ----------------
    # The exam's absolute UTC deadline is stored on the server (in the DB),
    # so the client can never extend time by changing its system clock or by
    # refreshing the page. If the deadline has passed by more than a small
    # grace window (to absorb network latency around the auto-submit), the
    # submission is rejected outright — even if the client was bypassed.
    if s.deadline:
        try:
            deadline_dt = datetime.fromisoformat(s.deadline)
            now_utc = datetime.now(timezone.utc)
            # Allow a small grace period (10s) so a legitimate auto-submit that
            # crosses the wire right at 00:00 still succeeds.
            grace = timedelta(seconds=10)
            if now_utc > deadline_dt + grace and s.status != "completed":
                audit(
                    "submit_rejected_deadline",
                    session_id=session_id,
                    reason="deadline_passed",
                    now=now_utc.isoformat(),
                    deadline=s.deadline,
                    ip=get_remote_address(),
                )
                return jsonify({"error": "The exam time has expired."}), 400
        except (TypeError, ValueError):
            # Malformed deadline (legacy data) — do NOT block the submission,
            # but log it so we know the session has bad timing data.
            audit(
                "submit_invalid_deadline",
                session_id=session_id,
                deadline=s.deadline,
                ip=get_remote_address(),
            )

    # ATOMIC GATE: only one worker can flip status from non-completed to
    # completed, AND only while the total number of completed submissions is
    # still below the capacity limit. This raw UPDATE bypasses the ORM
    # identity map so the check-and-set happens in a single SQLite statement,
    # serialized by the database write lock. This makes the capacity cap
    # safe against the "simultaneous submission" race.
    result = db.session.execute(
        text(
            "UPDATE sessions SET status='completed' "
            "WHERE id=:sid AND status != 'completed' "
            "AND (SELECT COUNT(*) FROM sessions "
            "     WHERE status='completed' AND host_email=:owner) < :max_cap"
        ),
        {"sid": session_id, "max_cap": max_cap, "owner": s.host_email},
    )
    if result.rowcount == 0:
        # Either the session was already completed, or the capacity cap was
        # reached. Distinguish so we can return a clean, accurate message.
        if completed_submissions_count(s.host_email) >= max_cap:
            audit(
                "capacity_reached",
                session_id=session_id,
                step="submit",
                reason="max_submissions",
                max_capacity=max_cap,
                ip=get_remote_address(),
            )
            return jsonify({"error": capacity_error(max_cap)}), 400
        return jsonify({"error": "invalid or already submitted"}), 400

    # Reload the session — it's now "owned" by this worker.
    s = db.session.get(Session, session_id)
    answers = payload.get("answers", {})
    session_owner = s.host_email

    # Belt-and-braces: merge submitted student info with stored record.
    student = payload.get("student")
    if student is not None and s.student is not None:
        current = s.student
        current.name = student.get("name", current.name) or current.name
        current.phone = student.get("phone", current.phone) or current.phone
        # Merge custom fields from the payload into the existing record
        existing_custom = dict(current.custom_fields or {})
        for key, value in student.items():
            if key not in ("name", "phone", "registered_at") and value:
                existing_custom[key] = value
        if existing_custom:
            current.custom_fields = existing_custom

    # Get the session's dealt questions (snapshot) for grading.
    session_questions = list(s.questions)

    score = 0
    auto_graded_only = True

    # Delete any existing answers for this session (shouldn't exist, but safe)
    Answer.query.filter_by(session_id=session_id).delete()
    for idx, q in enumerate(session_questions):
        qtype = q.type or "mcq"
        selected = answers.get(str(idx))
        if qtype == "mcq":
            try:
                selected = int(selected) if selected is not None else None
            except (TypeError, ValueError):
                selected = None
            is_correct = selected is not None and selected == q.correct_index
            score += int(is_correct)
            answer = Answer(
                session_id=session_id,
                position=idx,
                question_id=q.question_id,
                type=qtype,
                response=selected,
                correct=is_correct,
                correct_index=q.correct_index,
            )
        else:





            # essay / coding: store the raw text for manual review.
            auto_graded_only = False
            if isinstance(selected, str):
                selected = selected.strip()
            else:
                selected = None
            answer = Answer(
                session_id=session_id,
                position=idx,
                question_id=q.question_id,
                type=qtype,
                response=selected,
                correct=None,
                correct_index=None,
            )
        db.session.add(answer)

    s.score = score if auto_graded_only else None
    s.total_selected = len(session_questions)
    s.completed_at = datetime.now(timezone.utc).isoformat()
    db.session.commit()

    if auto_graded_only:
        percent = round((score / len(session_questions)) * 100) if session_questions else 0
    else:

        percent = None  # essay/coding answers require manual review

    if s.host_email:
        write_tests_conducted(s.host_email)

    return jsonify(
        {
            "score": s.score,
            "total": len(session_questions),
            "percent": percent,
            "manual_review": not auto_graded_only,
        }
    )

@app.route("/exam/<session_id>/result.pdf")
def result_pdf(session_id):
    """
    Download the candidate's graded result as a PDF.
    """
    s = db.session.get(Session, session_id)

    if s is None or s.status != "completed":
        return redirect(url_for("host", error="session_not_found"))

    return pdf_response(
        "result_pdf.html",
        f"result_{session_id}.pdf",
        session=session_to_dict(s),
    )

@app.route("/host/session/<session_id>/details.pdf")
@login_required
def details_pdf(session_id):
    """
    Download the host's session-details view as a PDF.

    Ownership-guarded: only the host who created this session may download
    its details PDF. Any other host is redirected away.
    """
    s = db.session.get(Session, session_id)

    if s is None or s.host_email != _current_host_email():
        return redirect(url_for("host", error="session_not_found"))

    cfg = s.config or {}
    return pdf_response(
        "details_pdf.html",
        f"details_{session_id}.pdf",
        session=session_to_dict(s),
        custom_registration_fields=cfg.get("custom_registration_fields") or [],
    )

@app.route("/host/session/<session_id>/details")
@login_required
def session_details(session_id):
    """
    Host 'View Details' page — shows the registered student's name, phone,
    and ID, plus every submitted answer for that session. Also passes the
    session's custom registration fields (from the Dynamic Form Builder) so
    the template can render arbitrary student fields by display name.

    Ownership-guarded: only the host who created this session may view its
    details. Any other host is redirected away.
    """
    s = db.session.get(Session, session_id)

    if s is None or s.host_email != _current_host_email():
        return redirect(url_for("host", error="session_not_found"))

    cfg = s.config or {}
    return render_template(
        "details.html",
        session=session_to_dict(s),
        custom_registration_fields=cfg.get("custom_registration_fields") or [],
    )


@app.route("/host/session/<session_id>/delete", methods=["POST"])
@login_required
def delete_session(session_id):
    """Remove a generated session from the dashboard.

    Ownership-guarded: only the host who created this session may delete it.
    """
    s = db.session.get(Session, session_id)
    if s and s.host_email == _current_host_email():
        db.session.delete(s)
        db.session.commit()
    return redirect(url_for("host"))

@app.route("/host/exam/<exam_id>/delete", methods=["POST"])
@login_required
def delete_exam(exam_id):
    """
    Delete a generated Exam (universal portal) AND all of its per-student
    attempts (sessions), answers, session_questions, and student records.

    Ownership-guarded: only the host who created this exam may delete it.
    """
    ex = db.session.get(Exam, exam_id)
    if ex and ex.host_email == _current_host_email():
        db.session.delete(ex)
        db.session.commit()
        audit("exam_deleted", exam_id=exam_id, ip=get_remote_address())
    return redirect(url_for("host"))

@app.route("/host/exam/<exam_id>/attempts")
@login_required
def host_exam_attempts(exam_id):
    """
    Host 'View Attempts' page — lists every student who registered for a
    shared Exam along with their individual attempt status and score.

    Ownership-guarded: only the host who created this exam may view it.
    """
    ex = db.session.get(Exam, exam_id)

    if ex is None or ex.host_email != _current_host_email():
        return redirect(url_for("host", error="session_not_found"))

    cfg = ex.config or {}
    custom_fields = cfg.get("custom_registration_fields") or []

    # All attempts (sessions) for this exam, newest first.
    attempts = (
        Session.query.filter_by(exam_id=exam_id)
        .order_by(Session.created_at.desc())
        .all()
    )

    return render_template(
        "exam_attempts.html",
        exam=exam_to_dict(ex),
        attempts=[session_to_dict(s) for s in attempts],
        custom_registration_fields=custom_fields,
        field_labels=REG_FIELD_LABELS,
    )

@app.route("/host/download-all-zip")
@login_required
def download_all_zip():
    """
    Download ALL student submissions as a single .zip archive.

    Each registered student becomes one readable text ".txt" report inside
    the archive, containing their registration details plus every submitted
    answer (MCQ selection, coding answer, and essay Q&A). The archive is
    built entirely in memory with the stdlib `zipfile` + `io` modules, so no
    temporary files are written to disk.

    Returns a .zip attachment via send_file, or a friendly message if there
    are no submissions yet.
    """
    import re as _re

    host_email = _current_host_email()

    # Per-host isolation: only zip THIS host's students (via their sessions).
    my_session_ids = [
        s.id for s in Session.query.filter_by(host_email=host_email).all()
    ]
    if my_session_ids:
        students = (
            Student.query.filter(Student.session_id.in_(my_session_ids))
            .order_by(Student.registered_at)
            .all()
        )
    else:
        students = []

    if not students:
        # Re-render the dashboard in its current form (uses `exams`).
        exams = (
            Exam.query.filter_by(host_email=host_email)
            .order_by(Exam.created_at.desc())
            .all()
        )
        questions = (
            Question.query.filter_by(host_email=host_email)
            .order_by(Question.created_at.desc())
            .all()
        )
        return (
            render_template(
                "host.html",
                questions=[bank_question_to_dict(q) for q in questions],
                exams=[exam_to_dict(x) for x in exams],
                field_labels=REG_FIELD_LABELS,
                error="No student submissions to download yet.",
            ),
            200,
        )

    memory_file = io.BytesIO()

    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for st in students:
            # Build a safe, unique filename for this student's report.
            safe_name = _re.sub(r"[^A-Za-z0-9]+", "_", st.name or "student").strip("_")
            safe_name = safe_name or "student"
            filename = f"{safe_name}_{st.session_id}.txt"

            report = _build_student_report(st)
            zf.writestr(filename, report.encode("utf-8"))

    memory_file.seek(0)

    audit("download_all_zip", student_count=len(students), ip=get_remote_address())

    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name="all_submissions.zip",
    )

def _build_student_report(st: Student) -> str:
    """
    Compile a single student's registration + answers into a readable text
    report. Handles MCQ (shows the selected option), essay and coding (shows
    the free-text answer verbatim), using the session's question snapshot so
    the text lines up with what the student actually saw.
    """
    lines = []
    lines.append("=" * 60)
    lines.append("EXAM SUBMISSION REPORT")
    lines.append("=" * 60)
    lines.append(f"Student : {st.name}")
    lines.append(f"Phone   : {st.phone or '-'}")
    lines.append(f"Session : {st.session_id}")
    lines.append(f"Time    : {st.registered_at or '-'}")

    # Custom registration fields (address, department, ...) stored as JSON.
    custom = st.custom_fields or {}
    if custom:
        lines.append("")
        lines.append("--- Registration Details ---")
        for key, value in custom.items():
            if value:
                lines.append(f"{key.replace('_', ' ').title()}: {value}")

    # The 1:1 session gives us both the dealt questions and the answers.
    session = st.session
    if session is None:
        lines.append("")
        lines.append("(No session data found for this student.)")
        return "\n".join(lines) + "\n"

    answers_by_position = {a.position: a for a in (session.answers or [])}

    lines.append("")
    lines.append("--- Submitted Answers ---")

    if not session.questions:
        lines.append("(No questions in this session.)")
    else:
        for q in session.questions:
            lines.append("")
            lines.append(f"Q{q.position + 1} [{q.type or 'mcq'.upper()}]")
            lines.append(f"  {q.text}")

            answer = answers_by_position.get(q.position)
            qtype = (q.type or "mcq").lower()

            if qtype == "mcq":
                opts = q.options or []
                selected = answer.response if answer else None
                if selected is not None and isinstance(selected, int) and 0 <= selected < len(opts):
                    lines.append(f"  Selected: ({selected}) {opts[selected]}")
                elif selected is not None and str(selected).isdigit() and 0 <= int(selected) < len(opts):
                    sel = int(selected)
                    lines.append(f"  Selected: ({sel}) {opts[sel]}")
                else:
                    lines.append("  Selected: (no answer)")
                # Correct answer for host reference.
                ci = q.correct_index
                if ci is not None and 0 <= ci < len(opts):
                    lines.append(f"  Correct : ({ci}) {opts[ci]}")
            else:
                # essay / coding -> free-text
                response = answer.response if answer else None
                lines.append("  Answer:")
                if response:
                    for rline in str(response).splitlines():
                        lines.append(f"    {rline}")
                else:
                    lines.append("    (no answer)")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)



