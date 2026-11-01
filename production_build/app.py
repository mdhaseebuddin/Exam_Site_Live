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
import hashlib
import hmac
import html
import io
import json
import logging
import logging.handlers
import os
import random
import re
import secrets
import shutil
import sqlite3
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import sib_api_v3_sdk
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
from sqlalchemy import event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash


def _sanitize_null_bytes(val):
    """Recursively strip null bytes (\\x00 / \\u0000) from incoming strings, dicts, or lists."""
    if isinstance(val, str):
        return val.replace("\x00", "").replace("\u0000", "")
    elif isinstance(val, dict):
        return {k: _sanitize_null_bytes(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_sanitize_null_bytes(v) for v in val]
    return val

from models import (
    AdminLockout,
    Answer,
    BlacklistEntry,
    DailyRegistration,
    Exam,
    ExamAttempt,
    ExamQuestion,
    ExamViolation,
    HostUser,
    OtpLog,
    OtpToken,
    Question,
    Session,
    SessionQuestion,
    Student,
    SystemSetting,
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
# Daily per-host registration limit — the STRICT cap on how many STUDENT
# registrations a single host may collect in any rolling 24-hour window,
# summed across ALL of that host's exam links combined (regardless of how many
# individual exam codes / links the host generates). This is enforced in
# addition to (and is typically far stricter than) the permanent
# MAX_SUBMISSIONS lifetime cap. Once a host hits DAILY_REGISTRATION_LIMIT,
# any further student registration on ANY of their links is blocked with a
# clear message until registrations age out of the 24-hour window — that is
# the automatic 24-hour cooldown/reset.
# ---------------------------------------------------------------------------
DAILY_REGISTRATION_LIMIT = int(os.environ.get("DAILY_REGISTRATION_LIMIT", "70"))
DAILY_REGISTRATION_WINDOW_HOURS = int(os.environ.get("DAILY_REGISTRATION_WINDOW_HOURS", "24"))

# ---------------------------------------------------------------------------
# Anti-cheating / browser-lockdown policy
# ---------------------------------------------------------------------------
# The number of security violations (tab switches, focus losses, blocked
# copy/paste, devtools attempts, ...) a student may accrue before the exam is
# FORCE-SUBMITTED and the attempt is flagged for the host. The first
# (threshold - 1) incidents show a strict warning modal; the final one triggers
# an automatic final submission + redirect to the results page.
MAX_VIOLATIONS = int(os.environ.get("MAX_VIOLATIONS", "3"))


def _exam_proctoring_policy(ex):
    """Resolve the proctoring policy for an Exam -> (enabled, max_violations).

    `ex` may be None (legacy single-session attempt with no parent Exam), in
    which case the previous global behavior is preserved: proctoring enabled
    with the env-configured MAX_VIOLATIONS threshold. For a real Exam, the
    host-configured columns are the single source of truth (falling back to
    their model defaults if somehow unset).
    """
    if ex is None:
        return True, MAX_VIOLATIONS
    enabled = ex.enable_proctoring
    if enabled is None:
        enabled = True
    max_violations = ex.max_violations
    if not max_violations or max_violations < 1:
        max_violations = MAX_VIOLATIONS
    return bool(enabled), int(max_violations)

# ---------------------------------------------------------------------------
# Secrets & configuration — loaded from .env (never from the code itself)
# ---------------------------------------------------------------------------
load_dotenv()  # reads SECRET_KEY, FLASK_ENV, SESSION_COOKIE_SECURE, ...

FLASK_ENV = os.environ.get("FLASK_ENV", "production")
IS_PRODUCTION = FLASK_ENV == "production"

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Human-readable UTC timestamp filter (`utc_fmt`) for templates
# ---------------------------------------------------------------------------
def _format_utc_timestamp(value):
    """Render an ISO-8601 UTC timestamp as 'YYYY-MM-DD HH:MM:SS UTC'.

    The DB stores timestamps as ISO-8601 strings (e.g.
    '2026-09-17T17:42:29.123456+00:00'), which wrap awkwardly when rendered
    raw inside the host's violation audit cards. This filter normalizes the
    value to a compact, single-line format. Naive values are assumed to be
    UTC; anything unparseable is returned unchanged so legacy/odd data never
    crashes the page.
    """
    if not value:
        return ""
    try:
        ts = str(value).strip()
        # Python < 3.11 cannot parse a trailing 'Z' — normalize it first.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    except (TypeError, ValueError, OverflowError):
        return value


app.jinja_env.filters["utc_fmt"] = _format_utc_timestamp

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

# --- Email delivery (Brevo transactional email) ---
# Sender address for HOST OTP/verification emails (password reset, host
# login). Overridable via the MAIL_DEFAULT_SENDER environment variable.
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
    "MAIL_DEFAULT_SENDER", "mdhaseebuddin77@gmail.com"
)

# Sender address for STUDENT OTP/verification emails. Kept SEPARATE from
# MAIL_DEFAULT_SENDER because the student Brevo keys
# (BREVO_STUDENT_API_KEY_1/_2) are verified against a DIFFERENT sender
# address (e.g. velotest2026@gmail.com). Passing the host's sender to those
# keys makes Brevo reject/drop the email (unverified sender or freemail-domain
# block). Overridable via the MAIL_STUDENT_SENDER environment variable.
app.config["MAIL_STUDENT_SENDER"] = os.environ.get(
    "MAIL_STUDENT_SENDER", "velotest2026@gmail.com"
)

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
def _resolve_database_uri() -> str:
    """Resolve the database URI with persistence-safe fallbacks.

    Priority order:
      1. DATABASE_URL — an explicit, operator-provided connection string
         (e.g. a managed external database). Used verbatim.
      2. DB_PATH       — an explicit absolute path to the SQLite file, pinned
         by an operator (development, or when a specific volume is required).
      3. RENDER_PERSISTENT_DISK_PATH — when deployed on Render with an attached
         Persistent Disk, Render injects this variable pointing at the mount.
         The instance / free-tier filesystem is EPHEMERAL and wiped on every
         redeploy — which is exactly why host accounts "disappear" and force
         re-registration. Routing the SQLite DB into the persistent disk keeps
         host accounts and submissions across restarts and redeploys.
      4. <BASE_DIR>/instance/exam.db — the portable local fallback for local
         development and non-ephemeral single-instance hosts.
    """
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif db_url.startswith("postgresql://") and "+psycopg2" not in db_url:
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return db_url

    render_disk = os.environ.get("RENDER_PERSISTENT_DISK_PATH", "").strip()
    # Only fall back to the Render persistent disk when the operator has not
    # pinned DB_PATH explicitly — an explicit path always wins.
    if render_disk and not os.environ.get("DB_PATH"):
        sqlite_file = os.path.join(render_disk, "exam.db")
    else:
        sqlite_file = DB_PATH

    # Ensure the parent directory exists (empty instance/ on a fresh clone, a
    # just-attached persistent-disk mount, etc.) before SQLite opens the file.
    os.makedirs(os.path.dirname(sqlite_file), exist_ok=True)
    return f"sqlite:///{sqlite_file}"


_DATABASE_URI = _resolve_database_uri()
_is_sqlite = make_url(_DATABASE_URI).get_backend_name() == "sqlite"

app.config["SQLALCHEMY_DATABASE_URI"] = _DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ``check_same_thread`` is a SQLite-only DBAPI option and MUST NOT appear in
# ``connect_args`` when DATABASE_URL points at PostgreSQL (or any other
# non-SQLite dialect) -- psycopg2 rejects it with::
#
#     ProgrammingError: invalid connection option "check_same_thread"
#
# Therefore it is applied ONLY for SQLite URIs and left empty otherwise.
if _is_sqlite:
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "connect_args": {"check_same_thread": False},
    }
else:
    # Database Connection Pool Safety for 15+ Concurrent Candidates on Neon PostgreSQL:
    # - pool_size=10, max_overflow=10: absorbs concurrency bursts without exhausting Neon pooler limits.
    # - pool_pre_ping=True: drops stale or closed sockets automatically before issuing queries.
    # - pool_recycle=280: refreshes idle connections under 5 minutes to stay ahead of Neon compute sleep.
    # - pool_timeout=30: bounds blocking wait times under severe traffic spikes.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 10,
        "max_overflow": 10,
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "pool_timeout": 30,
        "connect_args": {},
    }
_engine_options = SQLALCHEMY_ENGINE_OPTIONS
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = SQLALCHEMY_ENGINE_OPTIONS

db.init_app(app)


@app.teardown_appcontext
def shutdown_session(exception=None):
    """Ensure scoped SQLAlchemy session connections are promptly returned to the pool."""
    db.session.remove()


# Enable WAL mode + busy_timeout for concurrent reads and safe writes.
# This is critical when running under Gunicorn with multiple workers, as
# each worker has its own SQLite connection. WAL mode allows readers to
# proceed without blocking on a writer, and busy_timeout prevents premature
# "database is locked" errors under contention.
#
# The connect event fires for EVERY backend (including PostgreSQL when
# DATABASE_URL is active), so these SQLite-only PRAGMAs are guarded by an
# SQLite DBAPI check to avoid "syntax error at or near PRAGMA" on PG.
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
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
        if "status" not in host_cols:
            db.session.execute(text("ALTER TABLE host_users ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'active'"))
        if "suspended_until" not in host_cols:
            db.session.execute(text("ALTER TABLE host_users ADD COLUMN suspended_until TIMESTAMP"))
        if "is_banned" not in host_cols:
            db.session.execute(text("ALTER TABLE host_users ADD COLUMN is_banned BOOLEAN NOT NULL DEFAULT FALSE"))
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
            db.session.execute(text("ALTER TABLE students ADD COLUMN agreed_to_policy BOOLEAN NOT NULL DEFAULT FALSE"))
        if "agreed_at" not in stu_cols:
            db.session.execute(text("ALTER TABLE students ADD COLUMN agreed_at VARCHAR(64)"))
        # Student.email (email verification OTP for student registration)
        stu_cols = [c["name"] for c in insp.get_columns("students")]
        if "email" not in stu_cols:
            db.session.execute(text("ALTER TABLE students ADD COLUMN email VARCHAR(255) NOT NULL DEFAULT ''"))
        # Session.violation_count / flagged (anti-cheating browser-lockdown)
        sess_cols = [c["name"] for c in insp.get_columns("sessions")]
        if "violation_count" not in sess_cols:
            db.session.execute(
                text("ALTER TABLE sessions ADD COLUMN violation_count INTEGER NOT NULL DEFAULT 0")
            )
        if "flagged" not in sess_cols:
            db.session.execute(
                text("ALTER TABLE sessions ADD COLUMN flagged BOOLEAN NOT NULL DEFAULT FALSE")
            )
        # Session.auto_submitted (submission forced by the 3-strike threshold)
        if "auto_submitted" not in sess_cols:
            db.session.execute(
                text("ALTER TABLE sessions ADD COLUMN auto_submitted BOOLEAN NOT NULL DEFAULT FALSE")
            )
        if "extra_time_seconds" not in sess_cols:
            db.session.execute(
                text("ALTER TABLE sessions ADD COLUMN extra_time_seconds INTEGER NOT NULL DEFAULT 0")
            )
        if "submitted_at" not in sess_cols:
            db.session.execute(
                text("ALTER TABLE sessions ADD COLUMN submitted_at TIMESTAMP")
            )
        if "blacklist_entries" in insp.get_table_names():
            bl_cols = [c["name"] for c in insp.get_columns("blacklist_entries")]
            if "entry_type" not in bl_cols:
                db.session.execute(text("ALTER TABLE blacklist_entries ADD COLUMN entry_type VARCHAR(32)"))
                if "type" in bl_cols:
                    db.session.execute(text("UPDATE blacklist_entries SET entry_type = type WHERE entry_type IS NULL"))
        # ExamViolation.snapshot (proctoring proof image captured at strike time)
        viol_cols = [c["name"] for c in insp.get_columns("exam_violations")]
        if "snapshot" not in viol_cols:
            db.session.execute(text("ALTER TABLE exam_violations ADD COLUMN snapshot TEXT"))
        # Exam.enable_proctoring / Exam.max_violations (host-facing proctoring
        # policy — pre-existing exams keep the old behavior: proctored, 3 strikes).
        exam_cols = [c["name"] for c in insp.get_columns("exams")]
        if "enable_proctoring" not in exam_cols:
            db.session.execute(
                text("ALTER TABLE exams ADD COLUMN enable_proctoring BOOLEAN NOT NULL DEFAULT TRUE")
            )
        if "max_violations" not in exam_cols:
            db.session.execute(
                text("ALTER TABLE exams ADD COLUMN max_violations INTEGER NOT NULL DEFAULT 3")
            )
        # Ensure answers table has a unique index on (session_id, position)
        try:
            db.session.execute(text(
                "DELETE FROM answers WHERE id NOT IN ("
                "  SELECT MAX(id) FROM answers GROUP BY session_id, position"
                ")"
            ))
            db.session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_session_position_answer ON answers (session_id, position)"
            ))
        except Exception as e:
            app.logger.warning("Could not create unique index on answers: %s", e)
        db.session.commit()

        # --- Back-fill the durable daily-registration LEDGER from existing
        #     Student rows, so pre-existing hosts keep their rolling 24-hour
        #     counts when switching to ledger-based counting. Idempotent: only
        #     inserts (host_email, registered_at) pairs that are not already
        #     recorded. Runs inside a try/except so it can never block startup.
        try:
            known = {
                (r.host_email, r.registered_at)
                for r in db.session.query(
                    DailyRegistration.host_email,
                    DailyRegistration.registered_at,
                ).all()
            }
            source_rows = (
                db.session.query(
                    Session.host_email,
                    db.func.coalesce(
                        Student.registered_at, Session.created_at
                    ).label("reg_time"),
                )
                .join(Student, Student.session_id == Session.id)
                .filter(Session.host_email.isnot(None))
                .all()
            )
            added = 0
            for host_email, reg_time in source_rows:
                if host_email and reg_time and (host_email, reg_time) not in known:
                    db.session.add(
                        DailyRegistration(host_email=host_email, registered_at=reg_time)
                    )
                    known.add((host_email, reg_time))
                    added += 1
            if added:
                db.session.commit()
        except Exception:  # pragma: no cover - never block startup on backfill
            db.session.rollback()

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
    """Look up a host account by (lowercased) email.

    When multiple rows exist for the same email (e.g. legacy duplicates created
    before duplicate registration was prevented), the most recently created
    account wins so an old/stale row can never shadow the active account and
    reject its valid password.
    """
    if not email:
        return None
    return (
        HostUser.query.filter_by(email=email.strip().lower())
        .order_by(HostUser.id.desc())
        .first()
    )


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



# --- Emergency Ops System Settings, Lockout & Blacklist Helpers -------------
def get_system_setting(key: str, default=None) -> str | None:
    """Retrieve a persistent setting value from SystemSetting, checking expiry."""
    try:
        setting = db.session.get(SystemSetting, key)
        if not setting:
            return default
        if setting.expires_at is not None:
            now = datetime.now(timezone.utc)
            exp = setting.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return default
        return setting.value
    except Exception:
        return default


def set_system_setting(key: str, value: str, expires_at: datetime | None = None) -> None:
    """Persist a setting value in SystemSetting."""
    try:
        setting = db.session.get(SystemSetting, key)
        if not setting:
            setting = SystemSetting(key=key)
            db.session.add(setting)
        setting.value = str(value)
        setting.expires_at = expires_at
        setting.updated_at = datetime.now(timezone.utc)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"[SystemSetting ERROR] Failed to set {key}: {exc}")


def is_force_agent_otp_enabled() -> bool:
    """True when email_dispatcher must skip Brevo and route directly to Render OTP Agent."""
    val = get_system_setting("force_agent_otp", "false")
    return str(val).lower() in ("true", "1", "yes")


def is_master_universal_otp_active() -> tuple[bool, str, int]:
    """Returns (is_active, universal_otp_code, remaining_minutes)."""
    try:
        now = datetime.now(timezone.utc)
        code = get_system_setting("master_universal_otp_code", "999888") or "999888"

        # Check master_otp_active & master_otp_expires
        act_val = get_system_setting("master_otp_active")
        if act_val and str(act_val).lower() in ("true", "1", "yes"):
            exp_str = get_system_setting("master_otp_expires")
            if exp_str:
                exp = datetime.fromisoformat(exp_str)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now <= exp:
                    rem_min = max(1, int(((exp - now).total_seconds() + 59) // 60))
                    return True, code, rem_min
                else:
                    set_system_setting("master_otp_active", "false")
                    return False, code, 0

        # Check master_universal_otp row
        setting = db.session.get(SystemSetting, "master_universal_otp")
        if not setting or str(setting.value).lower() not in ("true", "1", "yes"):
            return False, code, 0
        rem_min = 0
        if setting.expires_at is not None:
            exp = setting.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return False, code, 0
            rem_min = max(1, int(((exp - now).total_seconds() + 59) // 60))
        return True, code, rem_min
    except Exception:
        return False, "999888", 0


def is_blacklisted(b_type: str, value: str) -> bool:
    """Check if a roll number or IP is actively blacklisted."""
    if not value:
        return False
    try:
        clean = str(value).strip().lower()
        kinds = ["roll", "roll_number"] if "roll" in str(b_type).lower() else ["ip"]
        entry = db.session.query(BlacklistEntry).filter(
            db.or_(
                BlacklistEntry.entry_type.in_(kinds),
                BlacklistEntry.type.in_(kinds)
            ),
            db.func.lower(BlacklistEntry.value) == clean
        ).first()
        return entry is not None
    except Exception:
        return False


def check_candidate_blacklist(ip: str, student_data: dict) -> str | None:
    """Check both IP and roll number against the BlacklistEntry table."""
    if is_blacklisted("ip", ip):
        return "Access denied: Your IP address has been restricted from participating in exams."
    for k, v in student_data.items():
        if not v or not isinstance(v, (str, int)):
            continue
        v_str = str(v).strip()
        k_lower = str(k).lower()
        if any(term in k_lower for term in ("roll", "rollno", "roll_no", "reg_no", "regno")):
            if is_blacklisted("roll", v_str):
                return f"Access denied: Roll number '{v_str}' has been restricted."
        if is_blacklisted("roll", v_str):
            return f"Access denied: Identifier '{v_str}' has been restricted."
    return None


def log_otp_dispatch(recipient_email: str, otp_code: str, channel: str = "student", status: str = "SENT", error: str | None = None) -> None:
    """Record an OTP dispatch event in the otp_logs table for audit & resend queue flush."""
    try:
        log_entry = OtpLog(
            recipient_email=(recipient_email or "").strip().lower(),
            otp_code=str(otp_code or "").strip(),
            channel=channel,
            status=status,
            error=str(error) if error else None,
            created_at=datetime.now(timezone.utc),
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f"[OtpLog ERROR] Failed to record OTP dispatch: {exc}")


def is_candidate_session_revoked(session_id: str, student_email: str | None = None, student_data: dict | None = None) -> bool:
    """Check if an active candidate's attempt token has been revoked by roll number, email, or global purge."""
    try:
        # Global purge check
        purge_time_str = get_system_setting("candidate_session_purged_at")
        if purge_time_str:
            purge_time = datetime.fromisoformat(purge_time_str)
            if purge_time.tzinfo is None:
                purge_time = purge_time.replace(tzinfo=timezone.utc)
            auth_time_str = session.get(f"auth_time_{session_id}")
            if not auth_time_str:
                return True
            auth_time = datetime.fromisoformat(auth_time_str)
            if auth_time.tzinfo is None:
                auth_time = auth_time.replace(tzinfo=timezone.utc)
            if auth_time < purge_time:
                return True

        # Targeted purge check
        purged_raw = get_system_setting("purged_candidates_json", "[]")
        if purged_raw:
            import json as _json
            purged_list = _json.loads(purged_raw)
            if isinstance(purged_list, list) and len(purged_list) > 0:
                purged_set = {str(item).strip().lower() for item in purged_list if item}
                if student_email and str(student_email).strip().lower() in purged_set:
                    return True
                if student_data and isinstance(student_data, dict):
                    for v in student_data.values():
                        if v and str(v).strip().lower() in purged_set:
                            return True
        return False
    except Exception:
        return False


def purge_candidate_identifier(identifier: str) -> int:
    """Revoke candidate attempt sessions by roll number or email."""
    if not identifier:
        return 0
    clean_id = str(identifier).strip().lower()
    import json as _json
    try:
        purged_raw = get_system_setting("purged_candidates_json", "[]")
        purged_list = _json.loads(purged_raw) if purged_raw else []
        if clean_id not in purged_list:
            purged_list.append(clean_id)
            set_system_setting("purged_candidates_json", _json.dumps(purged_list))
    except Exception as exc:
        print(f"[Purge List ERROR] {exc}")

    # Mark corresponding active sessions as SUBMITTED / completed
    affected = 0
    try:
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        students = Student.query.all()
        for st in students:
            match = False
            if st.email and st.email.strip().lower() == clean_id:
                match = True
            elif st.custom_fields and isinstance(st.custom_fields, dict):
                for val in st.custom_fields.values():
                    if val and str(val).strip().lower() == clean_id:
                        match = True
                        break
            if match and st.session and st.session.status not in ("completed", "SUBMITTED"):
                st.session.status = "SUBMITTED"
                st.session.completed_at = now_iso
                st.session.auto_submitted = True
                if hasattr(st.session, "submitted_at"):
                    st.session.submitted_at = now_utc
                affected += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[Purge Session ERROR] {e}")
    return affected


def get_admin_lockout() -> AdminLockout:
    """Fetch or initialize the singleton AdminLockout row."""
    lockout = db.session.query(AdminLockout).filter_by(key="master").first()
    if not lockout:
        lockout = AdminLockout(key="master", failed_attempts=0, locked_until=None)
        db.session.add(lockout)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            lockout = db.session.query(AdminLockout).filter_by(key="master").first()
    return lockout


def check_admin_lockout() -> tuple[bool, int]:
    """Returns (is_locked, remaining_minutes)."""
    try:
        lockout = get_admin_lockout()
        if lockout and lockout.locked_until:
            now = datetime.now(timezone.utc)
            lu = lockout.locked_until
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            if now < lu:
                rem_seconds = (lu - now).total_seconds()
                rem_minutes = max(1, int((rem_seconds + 59) // 60))
                return True, rem_minutes
            else:
                lockout.failed_attempts = 0
                lockout.locked_until = None
                lockout.updated_at = now
                db.session.commit()
                return False, 0
    except Exception as exc:
        print(f"[AdminLockout ERROR] {exc}")
    return False, 0


def record_failed_admin_attempt() -> tuple[bool, int]:
    """Increments failed attempts; locks for 30 minutes if reaches 3. Returns (is_locked, remaining_attempts_or_minutes)."""
    lockout = get_admin_lockout()
    now = datetime.now(timezone.utc)
    lockout.failed_attempts += 1
    lockout.updated_at = now
    if lockout.failed_attempts >= 3:
        lockout.locked_until = now + timedelta(minutes=30)
        db.session.commit()
        return True, 30
    db.session.commit()
    return False, max(0, 3 - lockout.failed_attempts)


def record_successful_admin_login():
    """Reset failed attempts on successful authorization."""
    lockout = get_admin_lockout()
    now = datetime.now(timezone.utc)
    lockout.failed_attempts = 0
    lockout.locked_until = None
    lockout.updated_at = now
    db.session.commit()


def is_ops_authenticated() -> bool:
    """Check if the current session has an encrypted ops authorization valid for 60 minutes."""
    if not session.get("ops_authenticated"):
        return False
    auth_time_str = session.get("ops_auth_time")
    if not auth_time_str:
        return False
    try:
        auth_time = datetime.fromisoformat(auth_time_str)
        if auth_time.tzinfo is None:
            auth_time = auth_time.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - auth_time > timedelta(minutes=60):
            session.pop("ops_authenticated", None)
            session.pop("ops_auth_time", None)
            return False
        return True
    except Exception:
        session.pop("ops_authenticated", None)
        session.pop("ops_auth_time", None)
        return False



def _trigger_tier3_webhook(recipient_email: str, otp_code: str) -> bool:
    """
    Tier 3 failover: If both Tier 1 and Tier 2 fail, trigger Tier 3 by making
    an HTTP POST request to:
       https://render-otp-agent.onrender.com/webhook/render-logs
    Send the data as: f"OTP sent to {recipient_email}: {otp_code}" with header {"Content-Type": "text/plain"}.
    Wrapped in a try-except block so it doesn't crash the server, and logs any error.
    """
    url = "https://render-otp-agent.onrender.com/webhook/render-logs"
    payload = f"OTP sent to {recipient_email}: {otp_code}"
    headers = {"Content-Type": "text/plain"}

    try:
        req_lib = None
        try:
            import requests as req_lib
        except ImportError:
            pass

        if req_lib is not None:
            resp = req_lib.post(url, data=payload, headers=headers, timeout=10)
            status_code = getattr(resp, "status_code", 200)
            print(f"[OTP Tier 3] Webhook POST to {url} returned status {status_code}")
            return status_code < 400
        else:
            req = urllib.request.Request(
                url,
                data=payload.encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status_code = getattr(resp, "status", getattr(resp, "code", 200))
                print(f"[OTP Tier 3] Webhook POST to {url} returned status {status_code}")
                return status_code < 400
    except Exception as exc:
        print(f"[OTP Tier 3 ERROR] Webhook delivery failed for {recipient_email}: {exc}")
        traceback.print_exc()
        try:
            audit(
                "otp_tier3_failed",
                email=recipient_email,
                error=repr(exc),
                ip=get_remote_address(),
            )
        except Exception:
            pass
        return False


def _send_otp_email(email: str, code: str) -> bool:
    """
    Deliver the OTP code to the host's email address via the Brevo
    (formerly Sendinblue) Transactional Email API with 3-tier failover:

      1. Try Tier 1 Brevo API key (BREVO_HOST_API_KEY_1 / BREVO_HOST_API_KEY / BREVO_API_KEY_1 / BREVO_API_KEY).
      2. If it fails, try Tier 2 Brevo API key (BREVO_HOST_API_KEY_2 / BREVO_API_KEY_2).
      3. If both Tier 1 and Tier 2 fail, trigger Tier 3 by making an HTTP POST request to:
         https://render-otp-agent.onrender.com/webhook/render-logs
         Send the data as: f"OTP sent to {recipient_email}: {otp_code}" with header {"Content-Type": "text/plain"}.
      4. Wrap Tier 3 in a try-except block so it doesn't crash the server, and log any error.
    """
    recipient_email = (email or "").strip()
    otp_code = (code or "").strip()
    sender_email = app.config.get("MAIL_DEFAULT_SENDER", "mdhaseebuddin77@gmail.com")

    if not recipient_email:
        print("[OTP ERROR] Host recipient email is empty; OTP cannot be sent.")
        audit("otp_email_failed", email=recipient_email, error="empty_recipient", ip=get_remote_address(), channel="host")
        return False

    if is_force_agent_otp_enabled():
        print(f"[OTP OVERRIDE] force_agent_otp enabled; skipping Brevo and routing directly to Render OTP Agent for {recipient_email}")
        res = _trigger_tier3_webhook(recipient_email=recipient_email, otp_code=otp_code)
        audit("otp_force_agent_dispatched", email=recipient_email, channel="host", success=res)
        log_otp_dispatch(recipient_email, otp_code, channel="host", status="SENT" if res else "FAILED")
        return res

    tier1_key = (
        os.environ.get("BREVO_HOST_API_KEY_1", "").strip()
        or os.environ.get("BREVO_HOST_API_KEY", "").strip()
        or os.environ.get("BREVO_API_KEY_1", "").strip()
        or os.environ.get("BREVO_API_KEY", "").strip()
    )
    tier2_key = (
        os.environ.get("BREVO_HOST_API_KEY_2", "").strip()
        or os.environ.get("BREVO_API_KEY_2", "").strip()
    )

    keys = []
    if tier1_key:
        keys.append((1, "Tier 1", tier1_key, "BREVO_HOST_API_KEY"))
    if tier2_key:
        keys.append((2, "Tier 2", tier2_key, "BREVO_HOST_API_KEY_2"))

    last_detail = None
    for index, tier_label, api_key, key_env_name in keys:
        try:
            configuration = sib_api_v3_sdk.Configuration()
            configuration.api_key["api-key"] = api_key
            api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
                sib_api_v3_sdk.ApiClient(configuration)
            )
            send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
                sender={"email": sender_email},
                to=[{"email": recipient_email}],
                subject="Your Exam Platform password reset code",
                text_content=(
                    "Your Exam Platform password reset code is: "
                    f"{otp_code}\n\nThis code expires in {OTP_LIFETIME_MINUTES} minutes."
                ),
            )
            response = api_instance.send_transac_email(send_smtp_email)
            message_id = getattr(response, "message_id", None) if response else None
            if not message_id:
                detail = f"Brevo responded without a messageId (response={response!r})"
                last_detail = f"{tier_label} ({key_env_name}): {detail}"
                print(f"[OTP WARN] Brevo did not return a messageId for {tier_label} ({key_env_name}) — message NOT accepted: {detail}")
                audit(
                    "otp_email_failed",
                    email=recipient_email,
                    sender=sender_email,
                    key=key_env_name,
                    reason="no_message_id",
                    response=repr(response),
                    ip=get_remote_address(),
                    channel="host",
                )
                continue
            print(f"[OTP] Sent host OTP via {tier_label} ({key_env_name}) (messageId={message_id}).")
            audit(
                "otp_email_sent",
                email=recipient_email,
                sender=sender_email,
                key=key_env_name,
                message_id=message_id,
                ip=get_remote_address(),
                channel="host",
            )
            return True
        except Exception as exc:
            status = getattr(exc, "status", None)
            reason = getattr(exc, "reason", None)
            body = getattr(exc, "body", None)
            if status is not None or body is not None:
                detail = f"HTTP {status} {reason} body={body!r}"
                print(f"[OTP ERROR] Brevo API rejected host OTP for {tier_label} ({key_env_name}): {detail}")
            else:
                detail = repr(exc)
                print(f"[OTP ERROR] Brevo delivery exception for host OTP via {tier_label} ({key_env_name}): {detail}")
            traceback.print_exc()
            last_detail = f"{tier_label} ({key_env_name}): {detail}"
            audit(
                "otp_email_failed",
                email=recipient_email,
                sender=sender_email,
                key=key_env_name,
                status=status,
                reason=reason,
                body=repr(body) if body is not None else None,
                error=detail,
                ip=get_remote_address(),
                channel="host",
            )

    # All host Brevo keys failed (or neither Tier 1 nor Tier 2 was configured)
    print(f"[OTP ERROR] All host Brevo keys failed: {last_detail}")
    try:
        _trigger_tier3_webhook(recipient_email=recipient_email, otp_code=otp_code)
    except Exception as tier3_exc:
        print(f"[OTP ERROR] Tier 3 failover failed for host OTP: {tier3_exc}")
        traceback.print_exc()

    print(f"\n[OTP DEBUG] Target: {recipient_email} | Code: {otp_code}\n")
    audit("otp_generated", email=recipient_email, code=otp_code, ip=get_remote_address())
    return False


def _send_student_otp(email: str, code: str) -> bool:
    """
    Deliver the OTP code to the STUDENT's email address via the Brevo
    (formerly Sendinblue) Transactional Email API with 3-tier failover:

      1. Try Tier 1 Brevo API key (BREVO_STUDENT_API_KEY_1 / BREVO_API_KEY_1).
      2. If it fails, try Tier 2 Brevo API key (BREVO_STUDENT_API_KEY_2 / BREVO_API_KEY_2).
      3. If both Tier 1 and Tier 2 fail, trigger Tier 3 by making an HTTP POST request to:
         https://render-otp-agent.onrender.com/webhook/render-logs
         Send the data as: f"OTP sent to {recipient_email}: {otp_code}" with header {"Content-Type": "text/plain"}.
      4. Wrap Tier 3 in a try-except block so it doesn't crash the server, and log any error.
    """
    sender_email = app.config.get("MAIL_STUDENT_SENDER")
    if not sender_email:
        sender_email = os.environ.get(
            "MAIL_STUDENT_SENDER", "velotest2026@gmail.com"
        )
    sender_email = (sender_email or "").strip()
    recipient_email = (email or "").strip()
    otp_code = (code or "").strip()

    if not recipient_email:
        print("[OTP ERROR] Student recipient email is empty; OTP cannot be sent.")
        audit("otp_email_failed", email=recipient_email, error="empty_recipient", ip=get_remote_address(), channel="student")
        return False
    if not sender_email:
        print("[OTP ERROR] MAIL_STUDENT_SENDER is empty; student OTP cannot be sent.")
        audit("otp_email_failed", email=recipient_email, error="missing_sender", ip=get_remote_address(), channel="student")
        return False

    if is_force_agent_otp_enabled():
        print(f"[OTP OVERRIDE] force_agent_otp enabled; skipping Brevo and routing directly to Render OTP Agent for {recipient_email}")
        res = _trigger_tier3_webhook(recipient_email=recipient_email, otp_code=otp_code)
        audit("otp_force_agent_dispatched", email=recipient_email, channel="student", success=res)
        log_otp_dispatch(recipient_email, otp_code, channel="student", status="SENT" if res else "FAILED")
        return res

    tier1_key = (
        os.environ.get("BREVO_STUDENT_API_KEY_1", "").strip()
        or os.environ.get("BREVO_API_KEY_1", "").strip()
    )
    tier2_key = (
        os.environ.get("BREVO_STUDENT_API_KEY_2", "").strip()
        or os.environ.get("BREVO_API_KEY_2", "").strip()
    )

    keys = []
    if tier1_key:
        keys.append((1, "Tier 1", tier1_key, "BREVO_STUDENT_API_KEY_1"))
    if tier2_key:
        keys.append((2, "Tier 2", tier2_key, "BREVO_STUDENT_API_KEY_2"))

    last_detail = None
    for index, tier_label, api_key, key_env_name in keys:
        try:
            configuration = sib_api_v3_sdk.Configuration()
            configuration.api_key["api-key"] = api_key
            api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
                sib_api_v3_sdk.ApiClient(configuration)
            )
            send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
                sender={"email": sender_email},
                to=[{"email": recipient_email}],
                subject="Your Exam Platform email verification code",
                text_content=(
                    "Your Exam Platform email verification code is: "
                    f"{otp_code}\n\n"
                    f"This code expires in {OTP_SESSION_LIFETIME_MINUTES} minutes."
                ),
            )
            response = api_instance.send_transac_email(send_smtp_email)
            message_id = getattr(response, "message_id", None) if response else None

            if not message_id:
                detail = f"Brevo responded without a messageId (response={response!r})"
                last_detail = f"{tier_label} ({key_env_name}): {detail}"
                print(
                    f"[OTP WARN] Brevo did not return a messageId for "
                    f"{tier_label} ({key_env_name}) — message NOT accepted: {detail}"
                )
                audit(
                    "otp_email_failed",
                    email=recipient_email,
                    sender=sender_email,
                    key=f"BREVO_STUDENT_API_KEY_{index}",
                    reason="no_message_id",
                    response=repr(response),
                    ip=get_remote_address(),
                    channel="student",
                )
                continue  # try the next student key

            print(
                f"[OTP] Sent student OTP via {tier_label} ({key_env_name}) "
                f"(messageId={message_id})."
            )
            audit(
                "otp_email_sent",
                email=recipient_email,
                sender=sender_email,
                key=f"BREVO_STUDENT_API_KEY_{index}",
                message_id=message_id,
                ip=get_remote_address(),
                channel="student",
            )
            return True
        except Exception as exc:  # pragma: no cover - network/API dependent
            status = getattr(exc, "status", None)
            reason = getattr(exc, "reason", None)
            body = getattr(exc, "body", None)
            if status is not None or body is not None:
                detail = f"HTTP {status} {reason} body={body!r}"
                print(
                    f"[OTP ERROR] Brevo API rejected {tier_label} "
                    f"({key_env_name}): {detail}"
                )
            else:
                detail = repr(exc)
                print(
                    f"[OTP ERROR] Brevo delivery exception via {tier_label} "
                    f"({key_env_name}): {detail}"
                )
                traceback.print_exc()
            last_detail = f"{tier_label} ({key_env_name}): {detail}"
            audit(
                "otp_email_failed",
                email=recipient_email,
                sender=sender_email,
                key=f"BREVO_STUDENT_API_KEY_{index}",
                status=status,
                reason=reason,
                body=repr(body) if body is not None else None,
                error=detail,
                ip=get_remote_address(),
                channel="student",
            )

    # All student Brevo keys failed (or neither Tier 1 nor Tier 2 was configured)
    print(f"[OTP ERROR] All student Brevo keys failed: {last_detail}")

    # Trigger Tier 3 failover webhook
    try:
        _trigger_tier3_webhook(recipient_email=recipient_email, otp_code=otp_code)
    except Exception as tier3_exc:
        print(f"[OTP ERROR] Tier 3 failover failed: {tier3_exc}")
        traceback.print_exc()

    print(f"\n[OTP DEBUG] Target: {recipient_email} | Code: {otp_code}\n")
    audit("otp_generated", email=recipient_email, code=otp_code, ip=get_remote_address())
    return False


def email_dispatcher(recipient_email: str, otp_code: str, channel: str = "student") -> bool:
    """
    Unified email dispatcher failover:
      1. If force_agent_otp is enabled in SystemSetting, skips Brevo entirely and routes to Render OTP Agent.
      2. Try Tier 1 Brevo API key.
      3. If it fails, try Tier 2 Brevo API key.
      4. If both Tier 1 and Tier 2 fail, trigger Tier 3 by making an HTTP POST request to:
         https://render-otp-agent.onrender.com/webhook/render-logs
         Send the data as: f"OTP sent to {recipient_email}: {otp_code}" with header {"Content-Type": "text/plain"}.
      5. Wrap Tier 3 in a try-except block so it doesn't crash the server, and log any error.
    """
    if is_force_agent_otp_enabled():
        print(f"[OTP OVERRIDE] force_agent_otp enabled; skipping Brevo and routing directly to Render OTP Agent for {recipient_email}")
        res = _trigger_tier3_webhook(recipient_email=recipient_email, otp_code=otp_code)
        audit("otp_force_agent_dispatched", email=recipient_email, channel=channel, success=res)
        log_otp_dispatch(recipient_email, otp_code, channel=channel, status="SENT" if res else "FAILED")
        return res
    if channel == "host":
        return _send_otp_email(recipient_email, otp_code)
    return _send_student_otp(recipient_email, otp_code)


send_email_dispatcher = email_dispatcher
dispatch_otp_email = email_dispatcher




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
      * Master Universal OTP is active and matches, OR
      * a matching token exists for the email,
      * it is not already used,
      * it has not expired.
    A successful match marks the token as used (single-use).
    """
    sub_code = (code or "").strip()
    is_univ_active, univ_code, _ = is_master_universal_otp_active()
    if is_univ_active and sub_code == univ_code:
        audit("universal_otp_accepted", email=email, purpose=purpose, ip=get_remote_address())
        return True

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
# Session-based OTP (login / registration) — 2-minute expiry
# ---------------------------------------------------------------------------
OTP_SESSION_LIFETIME_MINUTES = 2


def _set_session_otp(storage_key: str, code: str) -> None:
    """Record a freshly-issued OTP (its hash + UTC generation time) in the
    session. Storing the generation timestamp (datetime.utcnow()) lets
    verification enforce a strict N-minute window regardless of DB tokens.
    """
    session[storage_key] = {
        "code_hash": hash_password(code),
        "generated_at": datetime.utcnow(),
    }


def _issue_session_otp(
    storage_key: str, target: str, is_email: bool = True, channel: str = "host"
) -> str:
    """Generate, deliver and record a brand-new session OTP for the given
    target (email for hosts/students, phone for students). Returns the
    plaintext code so the caller can log it for local testing / SMS
    integration.

    ``channel`` selects the Brevo sender used for email delivery:
      * "host"    -> _send_otp_email (BREVO_API_KEY / host channel)
      * "student" -> _send_student_otp (BREVO_STUDENT_API_KEY_1/_2)
    The two channels are intentionally isolated on separate API keys.
    """
    code = _generate_otp()
    _set_session_otp(storage_key, code)
    if is_email:
        if channel == "student":
            _send_student_otp(target, code)
        else:
            _send_otp_email(target, code)
    else:
        # Phone target — log to console (a real SMS hook would go here).
        print(f"\n[OTP DEBUG] Target: {target} | Code: {code}\n")
    return code


def verify_session_otp(storage_key: str, submitted: str) -> str | None:
    """Validate a submitted code against the stored session OTP.

    Returns None when the code is valid, otherwise an error message. If the
    OTP has expired it is cleared from the session, matching the required
    "OTP has expired. Please request a new one." behaviour.
    Accepts Universal OTP when active.
    """
    sub_clean = (submitted or "").strip()
    is_univ_active, univ_code, _ = is_master_universal_otp_active()
    if is_univ_active and sub_clean == univ_code:
        session.pop(storage_key, None)
        audit("universal_otp_session_accepted", storage_key=storage_key, ip=get_remote_address())
        return None

    data = session.get(storage_key)
    if not data or not isinstance(data, dict):
        return "Invalid or expired verification code."

    generated_at = data.get("generated_at")
    if not isinstance(generated_at, datetime):
        session.pop(storage_key, None)
        return "Invalid or expired verification code."

    # Normalize to naive UTC before comparing. `datetime.utcnow()` returns a
    # naive datetime, while the value read back from the session cookie can be
    # either naive or offset-aware depending on how Flask's serializer handled
    # it — comparing naive vs aware would raise a TypeError. Making both naive
    # UTC guarantees a safe comparison.
    if generated_at.tzinfo is not None:
        generated_at = generated_at.astimezone(timezone.utc).replace(tzinfo=None)

    if datetime.utcnow() > generated_at + timedelta(minutes=OTP_SESSION_LIFETIME_MINUTES):
        session.pop(storage_key, None)  # clear the expired OTP
        return "OTP has expired. Please request a new one."

    if not verify_password(submitted, data.get("code_hash", "")):
        return "Invalid verification code. Please try again."

    return None  # valid


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


def _record_daily_registration(host_email: str | None, registered_at: str) -> None:
    """
    Write one durable daily-registration ledger row for this host.

    Called in the SAME transaction that creates the Student, so the row commits
    atomically with the registration (and rolls back together if the daily-cap
    gate rejects the registration). The "Delete Exam" and "Reset Exam Data"
    actions never touch this table, so a host's rolling 24-hour registration
    count stays accurate even after an individual exam link is deleted or
    reset.
    """
    if not host_email:
        return
    db.session.add(
        DailyRegistration(host_email=host_email, registered_at=registered_at)
    )


def daily_student_registrations_count(
    host_email: str | None = None, now=None
) -> int:
    """
    Number of STUDENT registrations a host has collected in the trailing
    ``DAILY_REGISTRATION_WINDOW_HOURS`` (24-hour) window, summed across ALL of
    the host's exam links combined.

    ``host_email`` is the per-host key, so the total is global to the host and
    independent of how many individual exams/links they generated. The count is
    taken from the durable ``DailyRegistration`` ledger (not the live Student
    rows), so deleting or resetting an individual exam link NEVER resets the
    host's daily progress back to zero — the rolling 24-hour cap stays tied to
    the host's actual historical registrations. Registration time is the moment
    the ledger row is written (identical to the Student row's
    ``registered_at``). A rolling window is used so each host is capped at
    DAILY_REGISTRATION_LIMIT registrations per 24 hours; the cap naturally
    resets (frees slots) as registrations age out of the window.
    """
    if not host_email:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=DAILY_REGISTRATION_WINDOW_HOURS)).isoformat()
    return (
        DailyRegistration.query.filter_by(host_email=host_email)
        .filter(DailyRegistration.registered_at >= cutoff)
        .count()
    )


def daily_registration_blocked(
    host_email: str | None = None, now=None
) -> bool:
    """True when a host has already hit the rolling 24-hour registration cap."""
    if not host_email:
        return False
    return (
        daily_student_registrations_count(host_email, now=now)
        >= DAILY_REGISTRATION_LIMIT
    )


def daily_limit_error() -> str:
    """Human-readable, user-facing message when a host's daily cap is hit."""
    return (
        "The daily registration limit has been reached. This host has collected "
        "the maximum of "
        f"{DAILY_REGISTRATION_LIMIT} student registrations in the last "
        f"{DAILY_REGISTRATION_WINDOW_HOURS} hours (across all of their exam "
        "links). No more students can register until the limit resets. Please "
        "try again later."
    )


@app.context_processor
def inject_capacity_context():
    """
    Inject the current host's capacity values into every template. This powers
    the host dashboard daily-registration counter (Daily Registrations: X / 70)
    without needing to pass them explicitly to every render call.

    The daily count is scoped to the CURRENT logged-in host, so each host sees
    their own independent rolling-24-hour usage.

    Zero-DB optimization: if no authenticated host is logged in, return default
    values immediately without querying PostgreSQL.
    """
    host_email = session.get(SESSION_HOST_EMAIL)
    if not host_email:
        return {
            "completed_submissions": 0,
            "max_capacity": MAX_SUBMISSIONS,
            "daily_student_registrations": 0,
            "daily_registration_limit": DAILY_REGISTRATION_LIMIT,
            "daily_registration_window_hours": DAILY_REGISTRATION_WINDOW_HOURS,
        }
    return {
        "completed_submissions": completed_submissions_count(
            host_email
        ),
        "max_capacity": MAX_SUBMISSIONS,
        "daily_student_registrations": daily_student_registrations_count(
            host_email
        ),
        "daily_registration_limit": DAILY_REGISTRATION_LIMIT,
        "daily_registration_window_hours": DAILY_REGISTRATION_WINDOW_HOURS,
    }


# ---------------------------------------------------------------------------
# Rate limiting (defense against brute force / DDoS)
# ---------------------------------------------------------------------------
limiter = Limiter(
    get_remote_address,  # key the rate limit on the caller's IP address
    app=app,
    default_limits=["10000 per minute"],  # global safety net per IP
)
# Explicitly exempt static assets from rate limits so CSS, JS, fonts, and question palette icons are never throttled
if "static" in app.view_functions:
    limiter.exempt(app.view_functions["static"])


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


@app.errorhandler(CSRFError)
def handle_csrf_rejection(e):
    # A CSRF failure (missing / expired / mismatched token) normally returns an
    # HTML error page. The exam client submits via fetch() and expects JSON, so
    # we echo the rejection back as JSON here — otherwise the client's
    # res.json() would choke on raw HTML and show tokens in the alert.
    audit("csrf_rejected", status=400, path=request.path, method=request.method,
          message=str(e), ip=get_remote_address())
    return jsonify({
        "error": "Your session or security token has expired. Please reload the page and try submitting again."
    }), 400


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


def _pdf_fallback(template_name, download_name=None, **ctx):
    """
    Render the print-friendly HTML template as the graceful fallback.
    The templates already contain print CSS so they can be saved to PDF from
    any browser, and this guarantees the app never crashes when PDF tooling
    is unavailable.

    The response stays inline HTML but advertises the ``download_name`` (e.g.
    "details.pdf") via Content-Disposition so a manual "Save as…" keeps the
    .pdf extension instead of saving a bare, extension-less "details" file.
    """
    from flask import make_response

    resp = make_response(render_template(template_name, **ctx))
    if download_name:
        resp.headers["Content-Disposition"] = f'inline; filename="{download_name}"'
    return resp


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
            escaped_answer = html.escape(str(answer_txt)).replace("\n", "<br/>")
            block.append(
                Paragraph(
                    escaped_answer,
                    ParagraphStyle("aw", parent=styles["Normal"], borderPadding=6, backColor=colors.HexColor("#fafafa")),
                )
            )
            block.append(Paragraph("Awaiting manual review", muted_style))

        if qtype == "mcq":
            story.append(KeepTogether(block))
        else:
            story.extend(block)
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
                escaped_answer = html.escape(str(answer_txt)).replace("\n", "<br/>")
                block.append(
                    Paragraph(
                        escaped_answer,
                        ParagraphStyle("aw", parent=styles["Normal"], borderPadding=6, backColor=colors.HexColor("#fafafa")),
                    )
                )
                block.append(Paragraph("Awaiting manual review", muted_style))

            if qtype == "mcq":
                story.append(KeepTogether(block))
            else:
                story.extend(block)
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

    The real-PDF response ALWAYS carries an explicit ``Content-Type:
    application/pdf`` and a quoted ``Content-Disposition: attachment;
    filename="...pdf"`` header, so browsers and operating systems (Windows
    included) recognize the download as a standard PDF file instead of a
    generic/no-extension file.
    """
    from flask import Response

    # Hard guarantee: the suggested filename ALWAYS ends in ".pdf". If a
    # caller passes e.g. "details" it becomes "details.pdf" — never a bare,
    # extension-less filename like "details".
    if not download_name.lower().endswith(".pdf"):
        download_name = f"{download_name}.pdf"

    pdf_data = _pdf_bytes(template_name, **ctx)
    if pdf_data is None:
        # Graceful HTML fallback (inline, print-to-PDF friendly) — but it still
        # advertises the ".pdf" filename so a manual "Save as…" on Windows
        # keeps the correct name instead of a bare "details".
        return _pdf_fallback(template_name, download_name=download_name, **ctx)

    return Response(
        pdf_data,
        headers={
            "Content-Type": "application/pdf",
            "Content-Disposition": f'attachment; filename="{download_name}"',
        },
    )


# ---------------------------------------------------------------------------
# Registration configuration — fields the host can require from each student
# ---------------------------------------------------------------------------
REG_FIELD_LABELS = {
    "name": "Full Name",
    "email": "Email Address",
    "phone": "Phone Number",
    "address": "Address",
    "department": "Department",
}
# email + phone are PERMANENT registration requirements: even if a host
# configures only some fields, we always collect (and verify) both.
PERMANENT_REQUIRED_FIELDS = ["email", "phone"]
DEFAULT_REQUIRED_FIELDS = ["name", "email", "phone"]

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

    email = fields.get("email", "")
    if email and not EMAIL_RE.match(email):
        return "Please enter a valid email address."

    phone = fields.get("phone", "")
    if phone and not PHONE_RE.match(phone):
        return "Phone number must be exactly 10 digits."

    return None


def _find_student_duplicate(exam_id, email_val="", phone_val=""):
    """Return an existing Student already registered on the SAME exam link.

    Student email/phone uniqueness is scoped PER EXAM LINK (the shared
    ``Exam`` that a registration portal belongs to), NOT across the whole
    database. The same student may register with the same email + phone for
    ANY number of different exam links, but only once per link.

    The lookup joins ``Student`` -> ``Session`` on the session's parent
    ``exam_id``, so registrations collected on OTHER exams never block this
    one. Returns the first matching Student row, or None.

    When ``exam_id`` is empty (a legacy single-session attempt with no parent
    Exam) there is no cross-session scope to check — a fresh Session can hold
    at most one Student anyway (UNIQUE on ``students.session_id``) — so the
    helper returns None and the legacy check is effectively skipped.
    """
    if not exam_id or (not email_val and not phone_val):
        return None
    q = (
        Student.query.join(Session, Student.session_id == Session.id)
        .filter(Session.exam_id == exam_id)
    )
    if email_val and phone_val:
        q = q.filter((Student.email == email_val) | (Student.phone == phone_val))
    elif email_val:
        q = q.filter(Student.email == email_val)
    else:
        q = q.filter(Student.phone == phone_val)
    return q.first()


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
@app.route("/healthz")
@limiter.exempt
def healthz():
    """Dedicated zero-DB health endpoint for keep-alive pingers and uptime monitors.

    Never imports models, queries the database, or initiates an SQLAlchemy
    session. Returns plain text immediately so keep-alive monitors can hit
    it 24/7 without Neon PostgreSQL compute waking up.
    """
    return "OK", 200


@app.route("/favicon.ico")
@limiter.exempt
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
    """Root — if logged in, go to the dashboard; otherwise the welcome page.

    Optimized for zero-DB overhead on initial GET requests: anonymous visitors
    and external pingers receive the welcome landing page immediately without
    triggering database queries or verification checks.
    """
    if session.get(SESSION_HOST_EMAIL):
        if current_host() is not None:
            return redirect(url_for("host"))
    return render_template("welcome.html")


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
                # Duplicate account — never allow a second account for the
                # same email. Ask the host to log in instead.
                audit("host_registration_duplicate", email=email, step="input", ip=get_remote_address())
                flash("Account already exists. Please log in.")
                return redirect(url_for("host_login"))
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
                _issue_session_otp("reg_otp", email)
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
                _issue_session_otp("reg_otp", pending["email"])
                flash("A new verification code has been sent.")
                return redirect(url_for("host_register"))
            
            code = request.form.get("otp", "").strip()
            otp_err = verify_session_otp("reg_otp", code)
            if otp_err is None:
                # ---- Authoritative duplicate check (finalize step) ----
                # The account is actually created HERE (not at the input
                # step), so the email must be re-checked against the database
                # to prevent a duplicate record being created — e.g. the
                # account was created after this browser submitted the input
                # form, or a stale pending registration is being finalized.
                if find_host(pending["email"]) is not None:
                    audit("host_registration_duplicate", email=pending["email"], step="verify", ip=get_remote_address())
                    session.pop("reg_step", None)
                    session.pop("reg_pending", None)
                    session.pop("reg_otp", None)
                    flash("Account already exists. Please log in.")
                    return redirect(url_for("host_login"))

                from sqlalchemy.exc import IntegrityError

                # Success: Create the host
                host = HostUser(
                    email=pending["email"],
                    name=pending["name"],
                    password_hash=pending["password_hash"],
                )
                try:
                    db.session.add(host)
                    db.session.commit()
                except IntegrityError:
                    # Lost a concurrent registration race — a row for this
                    # email already exists. Never allow a duplicate account.
                    db.session.rollback()
                    audit("host_registration_duplicate", email=pending["email"], step="verify", reason="integrity_error", ip=get_remote_address())
                    session.pop("reg_step", None)
                    session.pop("reg_pending", None)
                    session.pop("reg_otp", None)
                    flash("Account already exists. Please log in.")
                    return redirect(url_for("host_login"))
                
                audit("host_registered", email=pending["email"], name=pending["name"], ip=get_remote_address())
                log_user_login(host, "REGISTER")
                
                email = pending["email"]
                session.clear() # Clear registration data
                session[SESSION_HOST_EMAIL] = email
                return redirect(url_for("host"))
            else:
                error = otp_err

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

            # Password lookup + verification. The canonical host record
            # (find_host) is tried first; if it does not match, every account
            # row for this email is also tried so that valid credentials always
            # succeed even if legacy/duplicate rows exist — and the matched
            # account becomes the one we log in.
            auth_host = None
            if host is not None and host.password_hash and verify_password(password, host.password_hash):
                auth_host = host
            if auth_host is None and host is not None:
                for candidate in (
                    HostUser.query.filter_by(email=email)
                    .order_by(HostUser.id.desc())
                    .all()
                ):
                    if candidate.password_hash and verify_password(password, candidate.password_hash):
                        auth_host = candidate
                        break

            if auth_host is None:
                error = "Invalid email or password."
                audit("host_login_failed", email=email, ip=get_remote_address())
            elif getattr(auth_host, "is_suspended", lambda: False)():
                error = f"This account is {auth_host.status}. Access denied."
                audit("host_login_blocked_suspended", email=email, status=auth_host.status, ip=get_remote_address())
            elif not verify_captcha(captcha):
                error = "Incorrect CAPTCHA answer. Please try again."
            elif request.form.get("agree") != "1":
                error = "You must accept the Terms & Privacy Policy to log in."
            else:
                session["login_pending"] = auth_host.email
                _issue_session_otp("login_otp", auth_host.email)
                session["login_step"] = "verify"
                return redirect(url_for("host_login"))

        elif step == "verify":
            action = request.form.get("action", "verify")
            email = session.get("login_pending")

            if not email:
                session.pop("login_step", None)
                return redirect(url_for("host_login"))

            if action == "resend":
                _issue_session_otp("login_otp", email)
                flash("A new verification code has been sent.")
                return redirect(url_for("host_login"))

            code = request.form.get("otp", "").strip()
            otp_err = verify_session_otp("login_otp", code)
            if otp_err is None:
                host = find_host(email)
                if host and getattr(host, "is_suspended", lambda: False)():
                    session.pop("login_step", None)
                    session.pop("login_pending", None)
                    error = f"This account is {host.status}. Access denied."
                    return render_template(
                        "host_login.html",
                        error=error,
                        captcha=captcha_payload(),
                    )
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
                error = otp_err

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


# --- Host OTP resend (login / registration) ----------------------------------
@app.route("/host/resend-otp", methods=["POST"])
@limiter.limit("6 per minute", methods=["POST"])
def host_resend_otp():
    """Re-issue the active host-channel OTP (login or registration).

    This endpoint drives the async "Resend Code" buttons on the host verify
    pages. It NEVER trusts a client-supplied email: the recipient is taken
    exclusively from server-side session state, so only a browser that
    actually has a pending login/registration in progress can trigger a
    resend. A brand-new code is generated, the 2-minute expiry window is
    reset, and the code is dispatched through the host Brevo channel.
    """
    if session.get("login_step") == "verify" and session.get("login_pending"):
        target = session["login_pending"]
        token_key = "login_otp"
    elif (
        session.get("reg_step") == "verify"
        and isinstance(session.get("reg_pending"), dict)
        and (session.get("reg_pending") or {}).get("email")
    ):
        target = session["reg_pending"]["email"]
        token_key = "reg_otp"
    else:
        audit("otp_resend_rejected", ip=get_remote_address())
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No active verification request was found. Please start again.",
                }
            ),
            400,
        )

    _issue_session_otp(token_key, target)  # host email channel (default)
    audit("otp_resend", email=target, channel="host", ip=get_remote_address())
    return jsonify(
        {"ok": True, "message": "A new verification code has been sent to your email."}
    )


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
    # child records (answers, session_questions, students, exam_violations).
    # Other hosts' data is never touched.
    host_email = _current_host_email()
    if not host_email:
        return redirect(url_for("host_login"))

    my_sessions = [s.id for s in Session.query.filter_by(host_email=host_email).all()]

    # Order matters: children must be deleted before parents. exam_violations
    # (the anti-cheating proctoring records) also FK-reference sessions and
    # MUST be cleared too. Bulk Query.delete() bypasses ORM relationship
    # cascades, so without this explicit delete the final DELETE on sessions
    # would violate the foreign key constraint and raise a 500 as soon as any
    # attempt has recorded a violation (PostgreSQL always enforces FKs; this
    # local SQLite pool enables PRAGMA foreign_keys=ON).
    n_answers = 0
    n_session_questions = 0
    n_violations = 0
    n_students = 0
    n_sessions = 0

    try:
        if my_sessions:
            n_answers = Answer.query.filter(Answer.session_id.in_(my_sessions)).delete(synchronize_session=False)
            n_session_questions = SessionQuestion.query.filter(
                SessionQuestion.session_id.in_(my_sessions)
            ).delete(synchronize_session=False)
            n_violations = ExamViolation.query.filter(
                ExamViolation.session_id.in_(my_sessions)
            ).delete(synchronize_session=False)
            n_students = Student.query.filter(Student.session_id.in_(my_sessions)).delete(synchronize_session=False)
            n_sessions = Session.query.filter(Session.id.in_(my_sessions)).delete(synchronize_session=False)
            # Commit the bulk delete so the reset actually takes effect. Without
            # an explicit commit here the ORM would silently roll back the
            # deletions at request teardown, turning "Clear Exam Data" into a
            # no-op.
            db.session.commit()
        else:
            db.session.commit()
    except Exception:
        # A failed reset must never surface as a raw 500: roll back the
        # incomplete transaction so nothing is half-deleted, and send the host
        # back to the dashboard with a friendly error message.
        db.session.rollback()
        audit(
            "submissions_clear_failed",
            host_email=host_email,
            answers=n_answers,
            session_questions=n_session_questions,
            students=n_students,
            sessions=n_sessions,
            violations=n_violations,
            error=traceback.format_exc()[-600:],
            ip=get_remote_address(),
        )
        return redirect(url_for("host", error="clear_failed"))

    # The destructive action is audited server-side regardless of outcome.
    audit(
        "submissions_cleared",
        host_email=host_email,
        answers=n_answers,
        session_questions=n_session_questions,
        students=n_students,
        sessions=n_sessions,
        violations=n_violations,
        ip=get_remote_address(),
    )

    # Redirect back to the dashboard with a visible success message.
    return redirect(url_for("host", msg="submissions_cleared"))


def sanitize_filename(name: str) -> str:
    """Sanitize candidate names and identifiers for safe filesystem/ZIP names."""
    cleaned = re.sub(r'[^a-zA-Z0-9_\-]', '_', (name or 'Unknown_Candidate').strip())
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    return cleaned or 'Unknown_Candidate'


@app.route("/host/download-all.zip")
@app.route("/host/exam/<exam_id>/download-all.zip")
@app.route("/host/exam/<exam_id>/download.zip")
@login_required
def download_all_zip(exam_id=None):
    """
    Export student submissions as cleanly named individual PDF reports in a ZIP archive.

    Eliminates redundant JSON dumps, debug logs, and nested subfolders.
    Submissions are formatted neatly as:
      Exam_<exam_id>_Submissions/01_John_Doe_7670966268.pdf
    """
    host_email = _current_host_email()
    if not host_email:
        return redirect(url_for("host_login"))

    target_exam_id = exam_id or request.args.get("exam_id")
    if target_exam_id:
        ex = db.session.get(Exam, target_exam_id)
        if ex is not None and ex.host_email and ex.host_email != host_email:
            return redirect(url_for("host", error="Unauthorized"))
        all_sessions = (
            Session.query.filter_by(exam_id=target_exam_id)
            .order_by(Session.created_at.asc())
            .all()
        )
    else:
        all_sessions = (
            Session.query.filter(
                (Session.host_email == host_email)
                | (Session.exam_id.in_(
                    db.session.query(Exam.id).filter_by(host_email=host_email)
                ))
            )
            .order_by(Session.created_at.asc())
            .all()
        )

    sessions = [s for s in all_sessions if s.student is not None]
    if not sessions:
        return redirect(url_for("host", error="No candidate submissions found to download."))

    # Determine a clean root folder name
    if target_exam_id:
        root_folder = f"Exam_{target_exam_id[:8]}_Submissions"
    elif len(sessions) > 0 and all(s.exam_id == sessions[0].exam_id for s in sessions) and sessions[0].exam_id:
        root_folder = f"Exam_{sessions[0].exam_id[:8]}_Submissions"
    else:
        username = sanitize_filename(host_email.split("@", 1)[0])
        root_folder = f"Exam_Submissions_{username}"

    zip_buf = io.BytesIO()
    used_filenames = set()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for idx, s in enumerate(sessions, start=1):
            data = session_to_dict(s)
            st = data.get("student") or {}
            raw_name = (st.get("name") or "Candidate").strip()
            clean_name = sanitize_filename(raw_name)

            # Retrieve phone or roll number/student ID
            raw_id = (
                st.get("roll_number")
                or st.get("roll_no")
                or st.get("student_id")
                or st.get("roll")
                or st.get("id")
                or st.get("phone")
                or s.id[:8]
            )
            clean_id = sanitize_filename(str(raw_id))

            base_name = f"{idx:02d}_{clean_name}_{clean_id}"
            pdf_filename = f"{base_name}.pdf"
            if pdf_filename in used_filenames:
                dup = 2
                candidate_fn = f"{base_name}_{dup}.pdf"
                while candidate_fn in used_filenames:
                    dup += 1
                    candidate_fn = f"{base_name}_{dup}.pdf"
                pdf_filename = candidate_fn
            used_filenames.add(pdf_filename)

            # Generate ReportLab details PDF report
            pdf_bytes = _pdf_bytes(
                "details_pdf.html",
                session=data,
                custom_registration_fields=data.get("custom_registration_fields") or [],
            )

            archive_path = f"{root_folder}/{pdf_filename}"
            if pdf_bytes is not None:
                archive.writestr(archive_path, pdf_bytes)
            else:
                html_path = f"{root_folder}/{base_name}.html"
                archive.writestr(
                    html_path,
                    render_template(
                        "details_pdf.html",
                        session=data,
                        custom_registration_fields=data.get("custom_registration_fields") or [],
                    ),
                )

    zip_buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    download_name = f"{root_folder}_{stamp}.zip"

    audit(
        "download_all_zip",
        host_email=host_email,
        exam_id=target_exam_id,
        submissions=len(sessions),
        ip=get_remote_address(),
    )

    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/host/exam/<exam_id>/attempts")
@login_required
def host_exam_attempts(exam_id):
    """
    Per-exam attempts view — lists every student that registered for an Exam.

    Per-host isolation: a host can only see attempts for an Exam they own. The
    page is rendered from exam_attempts.html, which shows each candidate and
    links to the per-session details page.
    """
    host_email = _current_host_email()
    ex = db.session.get(Exam, exam_id)
    if ex is None or (ex.host_email and ex.host_email != host_email):
        return redirect(url_for("host", error="session_not_found"))

    cfg = ex.config or {}
    sessions = (
        Session.query.filter_by(exam_id=exam_id)
        .order_by(Session.created_at.asc())
        .all()
    )
    attempts = [session_to_dict(s) for s in sessions]

    return render_template(
        "exam_attempts.html",
        exam=exam_to_dict(ex),
        attempts=attempts,
        custom_registration_fields=cfg.get("custom_registration_fields") or [],
    )


@app.route("/host/session/<session_id>")
@login_required
def session_details(session_id):
    """
    Host view of a single session's details (submitted answers, score, student).

    Per-host isolation: only the owner of the session's Exam may view it.
    Rendered from details.html.
    """
    host_email = _current_host_email()
    s = db.session.get(Session, session_id)
    if s is None or (s.host_email and s.host_email != host_email):
        return redirect(url_for("host", error="session_not_found"))

    data = session_to_dict(s)
    cfg = s.config or {}
    # Per-exam proctoring threshold so the audit gate matches the host's
    # configured max_violations (falls back to the global default).
    _proctoring_enabled, max_violations = _exam_proctoring_policy(
        s.exam if s.exam_id else None
    )
    return render_template(
        "details.html",
        session=data,
        custom_registration_fields=cfg.get("custom_registration_fields") or [],
        # Anti-cheating: the strict proctoring-audit gate on the details page
        # compares the session's strike count against this server threshold.
        violation_threshold=max_violations,
    )


@app.route("/host/session/<session_id>/details.pdf")
@login_required
def session_details_pdf(session_id):
    """
    Download the per-session DETAILS PDF (host-facing, mirrors details.html).

    Uses the shared ReportLab pipeline (pdf_response) with a graceful HTML
    fallback when PDF tooling is unavailable.
    """
    host_email = _current_host_email()
    s = db.session.get(Session, session_id)
    if s is None or (s.host_email and s.host_email != host_email):
        return redirect(url_for("host", error="session_not_found"))

    data = session_to_dict(s)
    cfg = s.config or {}
    return pdf_response(
        "details_pdf.html",
        f"session_{session_id}.pdf",
        session=data,
        custom_registration_fields=cfg.get("custom_registration_fields") or [],
    )


@app.route("/host/exam/<exam_id>/session/<session_id>/unlock", methods=["POST"])
@app.route("/host/session/<session_id>/unlock", methods=["POST"])
@login_required
def unlock_candidate_session(session_id, exam_id=None):
    """Host Re-open / Unlock Endpoint.

    Authenticates that current_host() owns the exam.
    When invoked:
      - Finds target ExamSession.
      - Sets session.status = 'in_progress'.
      - Resets session.violations_count = 0 (and violation_count, flagged, auto_submitted).
      - Clears session.submitted_at = None (and completed_at, score).
      - Deletes any associated ExamViolation rows so strikes start completely fresh.
      - If timer extension requested (extra_minutes / extra_time_seconds),
        updates session.started_at, session.extra_time_seconds, and session.deadline.
      - Retains all previously saved Answer rows intact.
      - Commits to the database and returns JSON:
        {"success": True, "message": "Candidate session unlocked successfully"}.
    """
    host_email = _current_host_email()
    s = db.session.get(Session, session_id)
    if s is None:
        return jsonify({"success": False, "error": "Candidate session not found"}), 404

    target_exam_id = exam_id or s.exam_id
    if target_exam_id:
        ex = db.session.get(Exam, target_exam_id)
        if ex is not None and ex.host_email and ex.host_email != host_email:
            return jsonify({"success": False, "error": "Unauthorized"}), 403

    if s.host_email and s.host_email != host_email:
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or request.form or {}
    extra_minutes = int(payload.get("extra_minutes", 0) or 0)
    extra_seconds = int(payload.get("extra_time_seconds", 0) or 0)
    if extra_minutes > 0 and extra_seconds == 0:
        extra_seconds = extra_minutes * 60
    elif extra_seconds > 0 and extra_minutes == 0:
        extra_minutes = extra_seconds // 60

    # Re-open session & reset violation state
    s.status = "in_progress"
    s.violation_count = 0
    s.violations_count = 0
    s.flagged = False
    s.auto_submitted = False
    s.completed_at = None
    s.submitted_at = None
    s.score = None

    # Clear proctoring strike audit rows for this attempt
    ExamViolation.query.filter_by(session_id=session_id).delete()

    now = datetime.now(timezone.utc)
    if not s.started_at:
        s.started_at = now.isoformat()

    if extra_seconds > 0:
        s.extra_time_seconds = (getattr(s, "extra_time_seconds", 0) or 0) + extra_seconds

    try:
        current_deadline = datetime.fromisoformat(s.deadline) if s.deadline else now
    except Exception:
        current_deadline = now

    if extra_seconds > 0:
        if current_deadline < now:
            s.deadline = (now + timedelta(seconds=extra_seconds)).isoformat()
        else:
            s.deadline = (current_deadline + timedelta(seconds=extra_seconds)).isoformat()
    else:
        # If extra_seconds == 0 and deadline has already elapsed, grant a 5-minute review/submission buffer
        if current_deadline <= now:
            s.deadline = (now + timedelta(minutes=5)).isoformat()

    db.session.commit()
    audit("session_unlocked", exam_id=target_exam_id, session_id=session_id, host_email=host_email, extra_minutes=extra_minutes, ip=get_remote_address())
    if host_email:
        write_tests_conducted(host_email)

    return jsonify({"success": True, "message": "Candidate session unlocked successfully"})


@app.route("/host/exam/<exam_id>/delete", methods=["POST"])
@login_required
def delete_exam(exam_id):
    """
    Permanently delete an Exam (and everything under it).

    This is destructive and irreversible. Deleting the Exam cascades to every
    Session (and their Students, questions, answers) plus the ExamQuestion
    snapshot. Ownership-guarded: only the host who created the Exam may delete
    it. The host is prompted for confirmation client-side before this runs.
    """
    host_email = _current_host_email()
    ex = db.session.get(Exam, exam_id)
    if ex is not None and (ex.host_email is None or ex.host_email == host_email):
        db.session.delete(ex)
        db.session.commit()
        audit("exam_deleted", exam_id=exam_id, ip=get_remote_address())
        if host_email:
            write_tests_conducted(host_email)
    return redirect(url_for("host", msg="exam_deleted"))


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
    host_user = find_host(host_email)
    if host_user and getattr(host_user, "is_suspended", lambda: False)():
        audit("exam_generation_blocked_suspended", email=host_email, status=host_user.status, ip=get_remote_address())
        flash(f"Your host account has been {host_user.status}. You cannot generate new exams.", "error")
        return redirect(url_for("host"))
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

    # --- Proctoring policy (host-configurable) ------------------------------
    # `enable_proctoring` is a standard HTML checkbox (value="1" when checked);
    # `max_violations` is a small dropdown (3 / 5 / 7 / 10), clamped defensively.
    enable_proctoring = request.form.get("enable_proctoring") in ("1", "on", "true")
    try:
        max_violations = int(request.form.get("max_violations", "3") or 3)
    except (TypeError, ValueError):
        max_violations = 3
    max_violations = max(1, min(10, max_violations))

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
        enable_proctoring=enable_proctoring,
        max_violations=max_violations,
        config={
            "exam_title": exam_title,
            "time_limit_minutes": time_limit,
            "ratio": ratio,
            "max_capacity": max_capacity,
            "custom_registration_fields": custom_registration_fields,
            "required_fields": required_fields,
            # Host proctoring policy — mirrored into config JSON so session
            # copies and legacy serializers can read it without changes.
            "enable_proctoring": enable_proctoring,
            "max_violations": max_violations,
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
        enable_proctoring=enable_proctoring,
        max_violations=max_violations,
        bank_size=len(bank_questions),
        ip=get_remote_address(),
    )
    if ex.host_email:
        write_tests_conducted(ex.host_email)
    return redirect(url_for("host"))


# --- Student OTP resend (registration, universal + legacy entry points) ------
def get_student_otp_key():
    pending = session.get("stu_reg_pending") or {}
    return (pending.get("email") or "").strip().lower() or get_remote_address()


@app.route("/exam/resend-otp", methods=["POST"])
@limiter.limit("3 per minute", key_func=get_student_otp_key)
def exam_resend_otp():
    """Re-issue the active student-channel OTP for the in-progress registration.

    Drives the async "Resend Code" button on register_verify.html for BOTH the
    universal (/exam/register/<exam_id>) and legacy (/register/<session_id>)
    entry points. Resend uses ONLY server-side pending state — never a
    client-supplied email — and dispatches through the isolated student Brevo
    channel, resetting the 2-minute expiry window with a fresh code.
    """
    pending = session.get("stu_reg_pending")
    if (
        session.get("stu_reg_step") != "verify"
        or not pending
        or not pending.get("email")
    ):
        audit("otp_resend_rejected", ip=get_remote_address())
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No active registration was found. Please start the registration again.",
                }
            ),
            400,
        )

    _issue_session_otp("stu_reg_otp", pending["email"], is_email=True, channel="student")
    audit(
        "otp_resend",
        email=pending["email"],
        channel="student",
        ip=get_remote_address(),
    )
    return jsonify(
        {"ok": True, "message": "A new verification code has been sent to your email."}
    )


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

    if ex.host_email:
        exam_host = find_host(ex.host_email)
        if exam_host and getattr(exam_host, "is_suspended", lambda: False)():
            err_msg = f"This exam is currently unavailable because the organizer account has been {exam_host.status}."
            if request.method == "POST" and request.is_json:
                return jsonify({"error": err_msg}), 403
            return render_template("register.html", error=err_msg, form={}, captcha=captcha_payload()), 403

    client_ip = get_remote_address()
    if is_blacklisted("ip", client_ip):
        err_msg = "Access denied: Your IP address has been restricted from participating in exams."
        if request.method == "POST" and request.is_json:
            return jsonify({"error": err_msg}), 403
        return render_template("register.html", error=err_msg, form={}, captcha=captcha_payload()), 403

    cfg = ex.config or {}
    base_required = cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS
    required_fields = [
        f
        for f in base_required
        if f in REG_FIELD_LABELS
    ]
    # email + phone are PERMANENT requirements — always collected & verified.
    for perm in PERMANENT_REQUIRED_FIELDS:
        if perm not in required_fields:
            required_fields.append(perm)
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

    # --- Capacity gates ---
    max_cap = MAX_SUBMISSIONS
    if completed_submissions_count(ex.host_email) >= max_cap:
        # ... existing capacity logic ...
        err_msg = capacity_error(max_cap)
        audit("capacity_reached", exam_id=exam_id, step="register", reason="max_submissions", max_capacity=max_cap, ip=get_remote_address())
        if request.method == "POST" and request.is_json:
            return jsonify({"error": err_msg}), 400
        return (render_template("register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars), 400 if request.method == "POST" else 200)

    # --- Daily per-host registration cap (strict, across ALL links) ---
    # A host may collect at most DAILY_REGISTRATION_LIMIT student
    # registrations in any rolling 24-hour window, summed across every exam
    # link they generated. Checked here early for a clean user-facing message
    # and re-enforced atomically again at finalization (see below).
    if daily_registration_blocked(ex.host_email):
        err_msg = daily_limit_error()
        audit("daily_registration_limit_reached", exam_id=exam_id, step="register", host_email=ex.host_email, daily_limit=DAILY_REGISTRATION_LIMIT, window_hours=DAILY_REGISTRATION_WINDOW_HOURS, ip=get_remote_address())
        if request.method == "POST" and request.is_json:
            return jsonify({"error": err_msg}), 400
        return (render_template("register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars), 400 if request.method == "POST" else 200)

    step = session.get("stu_reg_step", "input")  # 'input' | 'verify'

    if request.method == "POST":
        # ---- OTP verify step: resend or confirm the emailed code ------
        # This branch ALWAYS returns, so the input-step handler below only
        # ever runs for a fresh registration (step == 'input').
        if step == "verify":
            action = request.form.get("action", "verify")
            pending = session.get("stu_reg_pending")
            if not pending or pending.get("exam_id") != exam_id:
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                return redirect(url_for("exam_register", exam_id=exam_id))

            ex = db.session.get(Exam, exam_id)
            if ex is None:
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                return redirect(url_for("host", error="session_not_found"))

            if action == "resend":
                _issue_session_otp("stu_reg_otp", pending["email"], is_email=True, channel="student")
                flash("A new verification code has been sent to your email.")
                return redirect(url_for("exam_register", exam_id=exam_id))

            code = request.form.get("otp", "").strip()
            otp_err = verify_session_otp("stu_reg_otp", code)
            if otp_err is not None:
                return render_template(
                    "register_verify.html", email=pending["email"], exam_id=exam_id, error=otp_err
                )

            # ---- OTP verified: finalize the registration --------------
            email_val = pending.get("email", "")
            phone_val = pending.get("phone", "")
            custom_vals = pending.get("custom_vals") or {}
            time_limit = int(pending.get("time_limit", 30) or 30)
            ratio = int(pending.get("ratio", 0) or 0)
            max_cap = int(pending.get("max_cap") or MAX_SUBMISSIONS)

            # Candidate Re-entry & Resume Gate:
            # If this is a resume for an active / re-opened session (status == 'in_progress' or 'started'),
            # authorize the candidate without creating duplicate rows or consuming daily capacity.
            is_resume = pending.get("is_resume", False)
            resume_session_id = pending.get("resume_session_id")
            dup = _find_student_duplicate(exam_id, email_val, phone_val)
            if not resume_session_id and dup:
                dup_session = dup.session or db.session.get(Session, dup.session_id)
                if dup_session and dup_session.status in ("in_progress", "started"):
                    is_resume = True
                    resume_session_id = dup_session.id

            if is_resume and resume_session_id:
                target_sess = db.session.get(Session, resume_session_id)
                if target_sess and target_sess.status in ("in_progress", "started"):
                    session[f"auth_{target_sess.id}"] = True
                    session[f"auth_time_{target_sess.id}"] = datetime.now(timezone.utc).isoformat()
                    session.pop("stu_reg_step", None)
                    session.pop("stu_reg_pending", None)
                    session.pop("stu_reg_otp", None)
                    audit("session_resumed", exam_id=exam_id, session_id=target_sess.id, email=email_val, phone=phone_val, ip=get_remote_address())
                    return redirect(url_for("exam", session_id=target_sess.id))

            # Per-exam uniqueness is re-checked at finalization (after the OTP
            # round-trip): if the same email/phone was registered for THIS exam
            # link in another browser while the code was pending, the OTP is
            # burned and the registration is rejected. Registrations on OTHER
            # exam links never affect this check.
            if dup is not None:
                audit(
                    "registration_failed",
                    exam_id=exam_id,
                    email=email_val,
                    phone=phone_val,
                    reason="duplicate_student",
                    step="verify",
                    ip=get_remote_address(),
                )
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                err = (
                    "This email address or phone number is already registered for this exam link. "
                    "Use the exam link you were given."
                )
                if request.is_json:
                    return jsonify({"error": err}), 400
                return (
                    render_template(
                        "register.html",
                        error=err,
                        form={},
                        captcha=captcha_payload(),
                        **template_vars,
                    ),
                    400,
                )

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
                    # Host proctoring policy copied from the parent Exam so the
                    # server and the exam page read ONE consistent value.
                    "enable_proctoring": cfg.get("enable_proctoring", True),
                    "max_violations": cfg.get("max_violations", 3),
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
                name=pending.get("name", ""),
                email=email_val,
                phone=phone_val,
                custom_fields=custom_vals,
                registered_at=now.isoformat(),
                agreed_to_policy=True,
                agreed_at=now.isoformat(),
            )
            db.session.add(student)

            # Durably record this registration for the host's rolling 24-hour
            # cap (atomic with the Student insert, using the same timestamp).
            _record_daily_registration(ex.host_email, now.isoformat())

            # --- Authoritative daily-cap gate (re-checked at commit time) ---
            # Even if this browser started registering before the limit was hit,
            # we must NOT create a new student once the host has reached their
            # 24-hour cap (closes the check-then-insert race as tightly as the
            # synchronous app allows).
            if daily_registration_blocked(ex.host_email):
                db.session.rollback()
                audit("daily_registration_limit_reached", exam_id=exam_id, step="verify", host_email=ex.host_email, daily_limit=DAILY_REGISTRATION_LIMIT, window_hours=DAILY_REGISTRATION_WINDOW_HOURS, ip=get_remote_address())
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                err_msg = daily_limit_error()
                if request.is_json:
                    return jsonify({"error": err_msg}), 400
                return render_template("register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars), 400

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                audit("registration_race_lost", exam_id=exam_id, session_id=session_id, ip=get_remote_address())
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                return redirect(url_for("exam_register", exam_id=exam_id))

            audit("registration_success", exam_id=exam_id, session_id=session_id, name=pending.get("name", ""), email=email_val, ip=get_remote_address())
            if s.host_email:
                write_tests_conducted(s.host_email)

            # Authorize this browser so the exam page opens directly afterwards.
            session[f"auth_{session_id}"] = True
            session[f"auth_time_{session_id}"] = datetime.now(timezone.utc).isoformat()

            # Clear the pending OTP registration data.
            session.pop("stu_reg_step", None)
            session.pop("stu_reg_pending", None)
            session.pop("stu_reg_otp", None)

            return redirect(url_for("exam", session_id=session_id))

        # ---- Input step: process a fresh registration form ------------
        student_data = {}
        missing = []

        # --- Duplicate check: email OR phone must not already be registered
        # for THIS exam link (scoped per exam_id — the same student may
        # register with the same email/phone for any OTHER exam link).
        phone_val = request.form.get("phone", "").strip()
        email_val = request.form.get("email", "").strip().lower()
        dup = _find_student_duplicate(exam_id, email_val, phone_val)
        is_resume = False
        resume_session_id = None
        if dup:
            dup_session = dup.session or db.session.get(Session, dup.session_id)
            if dup_session and dup_session.status in ("in_progress", "started"):
                is_resume = True
                resume_session_id = dup_session.id
            else:
                if email_val and dup.email == email_val and phone_val and dup.phone == phone_val:
                    err = "This email address and phone number are already registered."
                elif email_val and dup.email == email_val:
                    err = "This email address is already registered. Please use a different one."
                else:
                    err = "This phone number is already registered. Please use a different one."
                audit("registration_failed", exam_id=exam_id, email=email_val, phone=phone_val, reason="duplicate_student", ip=get_remote_address())
                if request.is_json:
                    return jsonify({"error": err}), 400
                return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400

        # --- core fields (name / phone) ---------------------------------
        for field in required_fields:
            value = request.form.get(field, "").strip()
            if not value:
                missing.append(REG_FIELD_LABELS.get(field, field))
                if field == "name" and len(value) > 100:
                    err = "Full Name cannot exceed 100 characters."
                return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400
            student_data[field] = value

        # --- custom dynamic fields ---------------------------------------
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            value = request.form.get(slug, "").strip()
            if cf.get("required") and not value:
                missing.append(cf.get("name", slug))
            if len(value) > 35:
                err = f"{cf.get('name', slug)} cannot exceed 35 characters."
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

        bl_err = check_candidate_blacklist(client_ip, {**request.form.to_dict(), **student_data})
        if bl_err:
            audit("registration_blacklisted", exam_id=exam_id, error=bl_err, ip=client_ip)
            if request.is_json:
                return jsonify({"error": bl_err}), 403
            return (
                render_template(
                    "register.html", error=bl_err, form=request.form, captcha=captcha_payload(), **template_vars
                ),
                403,
            )

        # --- Build the custom_fields JSON blob ---
        custom_vals = {}
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            if slug in student_data:
                custom_vals[slug] = student_data[slug]

        # --- Store pending data and email the verification OTP ----------
        # Email + phone were already validated and de-duplicated above.
        session["stu_reg_pending"] = {
            "exam_id": exam_id,
            "name": student_data.get("name", ""),
            "email": email_val,
            "phone": phone_val,
            "custom_vals": custom_vals,
            "time_limit": time_limit,
            "ratio": ratio,
            "max_cap": max_cap,
            "is_resume": is_resume,
            "resume_session_id": resume_session_id,
        }
        _issue_session_otp("stu_reg_otp", email_val, is_email=True, channel="student")
        session["stu_reg_step"] = "verify"
        return redirect(url_for("exam_register", exam_id=exam_id))

    # --- GET / render based on current step ---
    if step == "verify":
        pending = session.get("stu_reg_pending")
        if not pending or pending.get("exam_id") != exam_id:
            session.pop("stu_reg_step", None)
            session.pop("stu_reg_pending", None)
            session.pop("stu_reg_otp", None)
            return redirect(url_for("exam_register", exam_id=exam_id))
        return render_template(
            "register_verify.html", email=pending["email"], exam_id=exam_id, error=None
        )

    return render_template(
        "register.html", error=None, form={}, captcha=captcha_payload(), **template_vars
    )

@app.route("/exam/<session_id>")
@limiter.exempt
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

    time_limit_minutes = int((s.config or {}).get("time_limit_minutes", 30) or 30)

    # The exam clock is NOT started on page load. Proctored exams show a brief
    # pre-exam start gate first; the student presses "Start Exam" and the
    # client then calls POST /exam/<session_id>/start, which sets
    # started_at / deadline / status and starts the official countdown. Once
    # started, the deadline is persisted server-side so a page refresh can
    # never reset or extend the timer.
    now = datetime.now(timezone.utc)
    if s.started_at is not None:
        deadline = datetime.fromisoformat(s.deadline)
        remaining_seconds = max(0, int((deadline - now).total_seconds()))
        deadline_unix = int(deadline.timestamp())
        server_now_unix = int(now.timestamp())
    else:
        # Not started yet: send the full limit as the remaining time and a
        # sentinel deadline_unix of 0 so the client knows to call /start instead
        # of starting the countdown.
        remaining_seconds = time_limit_minutes * 60
        deadline_unix = 0
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
    # Host-configured proctoring policy (from the parent Exam, or the legacy
    # global default for pre-exam attempts). When disabled, the client skips
    # the browser lockdown entirely and the student takes a normal exam.
    proctoring_enabled, max_violations = _exam_proctoring_policy(
        s.exam if s.exam_id else None
    )
    required_fields = [
        f
        for f in (cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS)
        if f in REG_FIELD_LABELS
    ]
    custom_registration_fields = cfg.get("custom_registration_fields") or []

    saved_answers = {str(a.position): a.response for a in s.answers}

    return render_template(
        "exam.html",
        session_id=session_id,
        exam_title=cfg.get("exam_title", ""),
        questions=public_questions,
        saved_answers=saved_answers,
        remaining_seconds=remaining_seconds,
        deadline_unix=deadline_unix,
        server_now_unix=server_now_unix,
        time_limit_minutes=time_limit_minutes,
        student=student_to_dict(s.student) if s.student else None,
        required_fields=required_fields,
        custom_registration_fields=custom_registration_fields,
        field_labels=REG_FIELD_LABELS,
        # Anti-cheating: server-authoritative violation tally is passed to the
        # client so a page refresh NEVER resets the student's violation count,
        # and a flagged attempt force-submits immediately on load. The
        # threshold is the host-configured per-exam max_violations, and the
        # whole suite is skipped client-side when proctoring is disabled.
        enable_proctoring=proctoring_enabled,
        max_violations=max_violations,
        violation_threshold=max_violations,
        violation_count=s.violation_count or 0,
        flagged=bool(s.flagged),
    )


@app.route("/exam/<session_id>/start", methods=["POST"])
@limiter.exempt
def start_exam(session_id):
    """Start the official exam countdown.

    Called by the client when the student presses "Start Exam" (proctored) or
    immediately on page load (unproctored). Starting the clock here — instead
    of on the first page load — means the time a student spends reading the
    pre-exam instructions is never charged against their exam time.
    Idempotent: a session whose clock is already running simply returns its
    existing deadline.
    """
    s = db.session.get(Session, session_id)
    if s is None:
        return jsonify({"error": "invalid session"}), 400
    if s.status == "completed":
        return jsonify({"error": "exam already completed"}), 400
    if not session.get(f"auth_{session_id}"):
        return jsonify({"error": "unauthorized"}), 403
    if s.student is None:
        return jsonify({"error": "session not registered"}), 400

    now = datetime.now(timezone.utc)
    if s.started_at is None:
        s.started_at = now.isoformat()
        s.deadline = (
            now
            + timedelta(minutes=int((s.config or {}).get("time_limit_minutes", 30) or 30))
        ).isoformat()
        s.status = "started"
        db.session.commit()
        audit("exam_started", session_id=session_id, ip=get_remote_address())

    deadline = datetime.fromisoformat(s.deadline)
    return jsonify(
        {
            "deadline_unix": int(deadline.timestamp()),
            "server_now_unix": int(now.timestamp()),
        }
    )


@app.route("/exam/<session_id>/result.pdf")
@limiter.exempt
def exam_result_pdf(session_id):
    """
    Download the student's RESULT PDF for a completed exam session.

    Backs the "⬇️ Download Result PDF" button in result.html (a plain
    <a href="/exam/<session_id>/result.pdf">). Uses the shared ReportLab
    pipeline (pdf_response -> _build_result_pdf) with a graceful HTML
    fallback, so the button returns a real PDF attachment instead of a 404.

    Security: unknown/never-started sessions are bounced back to the host
    dashboard (no PDF is ever produced for invalid ids), matching the exam
    page's behavior.
    """
    s = db.session.get(Session, session_id)
    if s is None:
        return redirect(url_for("host", error="session_not_found"))
    # Only a completed submission has a result to download. Anything else is
    # bounced back to the exam/result flow rather than leaking a partial PDF.
    if s.status != "completed":
        return redirect(url_for("exam", session_id=session_id))

    return pdf_response(
        "result_pdf.html",
        f"result_{session_id}.pdf",
        session=session_to_dict(s),
    )


@app.route("/exam/<session_id>/time")
@limiter.exempt
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
    # Per-exam proctoring threshold (max_violations) so the client's local
    # force-submit check matches the host's configured value.
    _proctoring_enabled, max_violations = _exam_proctoring_policy(
        s.exam if s.exam_id else None
    )
    return jsonify(
        {
            "server_now_unix": int(now.timestamp()),
            "deadline_unix": int(deadline.timestamp()),
            # Anti-cheating: if the attempt has been flagged for N+ violations
            # while the student was away, the client force-submits immediately
            # on the next poll instead of letting them keep working.
            "flagged": bool(s.flagged),
            "violation_count": s.violation_count or 0,
            "violation_threshold": max_violations,
        }
    )


@app.route("/exam/<session_id>/violation", methods=["POST"])
@limiter.exempt
def exam_violation(session_id):
    """
    Anti-cheating incident reporter (SERVER-AUTHORITATIVE).

    The exam page's lockdown JS POSTs here every time a security flag fires:
    tab switches / visibility loss, window blur (clicking to another app),
    fullscreen exit, blocked copy/paste, right-click (context menu), or a
    developer-tools shortcut attempt.

    The server is the single source of truth for the violation tally:
      * It appends one ExamViolation row per incident (type + timestamp).
      * It increments the session's violation_count in the same transaction.
      * When the count reaches the host-configured max_violations the session
        is flagged; the client receives `auto_submit: true` and triggers a
        forced final submission, and any later return to the exam page
        force-submits too.

    A page refresh or a tampered client that suppresses reporting can NEVER
    reset the counter, because it lives here on the server.

    For exams created with proctoring DISABLED this endpoint is a no-op (OK,
    no strike recorded, never flags) — the student takes the exam normally
    and the host's override is honored server-side.
    """
    s = db.session.get(Session, session_id)
    if s is None or s.status != "started":
        return jsonify({"error": "invalid session"}), 400

    # Host-configured proctoring policy. If the exam is NOT proctored, no
    # strikes are ever recorded or counted: a tampered/legacy client posting a
    # violation report gets a no-op OK (never the auto-submit trigger), and the
    # session can never be flagged. This mirrors the client-side behavior where
    # the whole anti-cheating module is skipped for unproctored exams.
    proctoring_enabled, max_violations = _exam_proctoring_policy(
        s.exam if s.exam_id else None
    )
    if not proctoring_enabled:
        return jsonify(
            {
                "ok": True,
                "proctoring": False,
                "count": s.violation_count or 0,
                "threshold": max_violations,
                "auto_submit": False,
                "flagged": False,
            }
        )

    payload = request.get_json(silent=True) or {}
    vtype = str(payload.get("type", "")).strip() or "unknown"
    detail = str(payload.get("detail", "")).strip()
    if len(vtype) > 32:
        vtype = vtype[:32]
    if len(detail) > 255:
        detail = detail[:255]

    # No webcam proof snapshots are captured or stored anymore — the platform
    # relies purely on browser-level security. Violations carry only metadata
    # (type + detail + client timestamp), so every incident is persisted with
    # an empty snapshot reference. If a legacy client still posts a snapshot it
    # is simply ignored (never validated or decoded).
    snapshot = None

    s.violation_count = (s.violation_count or 0) + 1
    count = s.violation_count
    if count >= max_violations:
        s.flagged = True
    db.session.add(
        ExamViolation(
            session_id=session_id,
            violation_type=vtype,
            count=count,
            detail=detail or None,
            snapshot=snapshot,
        )
    )
    db.session.commit()

    audit(
        "exam_violation",
        session_id=session_id,
        violation_type=vtype,
        count=count,
        detail=detail,
        snapshot=(bool(snapshot)),
        flagged=bool(s.flagged),
        ip=get_remote_address(),
    )

    auto_submit = count >= max_violations
    response = {
        "ok": True,
        "count": count,
        "threshold": max_violations,
        "auto_submit": auto_submit,
        "flagged": auto_submit,
    }
    if auto_submit:
        response["message"] = (
            "Your exam has been flagged for repeated security violations and "
            "will be submitted immediately."
        )
    return jsonify(response)


@app.route("/exam/<session_id>/save-answer", methods=["POST"])
@app.route("/exam/<session_id>/answer", methods=["POST"])
@limiter.exempt
def save_exam_answer(session_id):
    """Auto-save / draft-save answer endpoint for an active exam session.

    Performs an UPSERT (updates if an answer row already exists for this
    session & question position/ID; inserts only if new) to avoid duplicate rows.
    """
    s = db.session.get(Session, session_id)
    if s is None:
        return jsonify({"error": "invalid session"}), 404
    if s.status == "completed" or getattr(s, "flagged", False):
        return jsonify({"error": "Session is completed or locked"}), 403
    if not session.get(f"auth_{session_id}"):
        return jsonify({"error": "unauthorized"}), 403

    if s.deadline:
        try:
            deadline_dt = datetime.fromisoformat(s.deadline)
            now_utc = datetime.now(timezone.utc)
            if now_utc > deadline_dt + timedelta(seconds=180):
                return jsonify({"error": "The exam time has expired."}), 403
        except (TypeError, ValueError):
            pass

    if request.data and request.get_json(silent=True) is None:
        return jsonify({"error": "Malformed or non-UTF8 JSON payload"}), 400
    payload = request.get_json(silent=True) or {}
    payload = _sanitize_null_bytes(payload)
    session_questions = list(s.questions)
    pos_to_q = {q.position: q for q in session_questions}
    qid_to_q = {q.question_id: q for q in session_questions if q.question_id}

    items_to_save = []
    if "answers" in payload and isinstance(payload["answers"], dict):
        for key, val in payload["answers"].items():
            items_to_save.append((key, val))
    else:
        qid = payload.get("questionId") or payload.get("question_id")
        pos = payload.get("position")
        val = payload.get("answerText") if "answerText" in payload else (
            payload.get("answer") if "answer" in payload else payload.get("response")
        )
        if pos is not None:
            items_to_save.append((pos, val))
        elif qid is not None:
            items_to_save.append((qid, val))

    for key, val in items_to_save:
        target_q = None
        target_pos = None

        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            target_pos = int(key)
            target_q = pos_to_q.get(target_pos)
        elif key in qid_to_q:
            target_q = qid_to_q[key]
            target_pos = target_q.position

        if not target_q:
            continue

        qtype = target_q.type or "mcq"
        correct = None
        correct_index = None

        if qtype == "mcq":
            correct_index = target_q.correct_index
            try:
                selected = int(val) if val not in (None, "") else None
            except (TypeError, ValueError):
                selected = None
            response_val = selected
            if selected is not None and correct_index is not None:
                correct = (selected == correct_index)
            elif selected is not None:
                correct = False
        else:
            response_val = "" if val is None else str(val)

        # UPSERT: check if an answer row already exists for this (session_id, target_pos)
        ans_row = Answer.query.filter_by(
            session_id=session_id, position=target_pos
        ).first()

        if ans_row:
            ans_row.question_id = target_q.question_id
            ans_row.type = qtype
            ans_row.response = response_val
            ans_row.correct = correct
            ans_row.correct_index = correct_index
        else:
            try:
                with db.session.begin_nested():
                    db.session.add(
                        Answer(
                            session_id=session_id,
                            position=target_pos,
                            question_id=target_q.question_id,
                            type=qtype,
                            response=response_val,
                            correct=correct,
                            correct_index=correct_index,
                        )
                    )
            except IntegrityError:
                ans_row = Answer.query.filter_by(
                    session_id=session_id, position=target_pos
                ).first()
                if ans_row:
                    ans_row.question_id = target_q.question_id
                    ans_row.type = qtype
                    ans_row.response = response_val
                    ans_row.correct = correct
                    ans_row.correct_index = correct_index

    db.session.commit()
    return jsonify({
        "ok": True,
        "saved": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


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
        session[f"auth_{session_id}"] = True
        return redirect(url_for("exam", session_id=session_id))

    # Fields this session collects (set by the host when generating the link).
    cfg = s.config or {}
    base_required = cfg.get("required_fields") or DEFAULT_REQUIRED_FIELDS
    required_fields = [
        f
        for f in base_required
        if f in REG_FIELD_LABELS
    ]
    # email + phone are PERMANENT requirements — always collected & verified.
    for perm in PERMANENT_REQUIRED_FIELDS:
        if perm not in required_fields:
            required_fields.append(perm)
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

    # --- Daily per-host registration cap (strict, across ALL links) ---
    # A host may collect at most DAILY_REGISTRATION_LIMIT student
    # registrations in any rolling 24-hour window, summed across every exam
    # link they generated. Checked early for a clean message and re-enforced
    # atomically again at finalization (see below).
    if daily_registration_blocked(s.host_email):
        err_msg = daily_limit_error()
        audit(
            "daily_registration_limit_reached",
            session_id=session_id,
            step="register",
            host_email=s.host_email,
            daily_limit=DAILY_REGISTRATION_LIMIT,
            window_hours=DAILY_REGISTRATION_WINDOW_HOURS,
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

    step = session.get("stu_reg_step", "input")  # 'input' | 'verify'

    if request.method == "POST":
        # ---- OTP verify step: resend or confirm the emailed code ------
        # This branch ALWAYS returns, so the input-step handler below only
        # ever runs for a fresh registration (step == 'input').
        if step == "verify":
            action = request.form.get("action", "verify")
            pending = session.get("stu_reg_pending")
            if not pending or pending.get("session_id") != session_id:
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                return redirect(url_for("register", session_id=session_id))

            if action == "resend":
                _issue_session_otp("stu_reg_otp", pending["email"], is_email=True, channel="student")
                flash("A new verification code has been sent to your email.")
                return redirect(url_for("register", session_id=session_id))

            code = request.form.get("otp", "").strip()
            otp_err = verify_session_otp("stu_reg_otp", code)
            if otp_err is not None:
                return render_template(
                    "register_verify.html", email=pending["email"], session_id=session_id, error=otp_err
                )

            # ---- OTP verified: finalize the registration --------------
            email_val = pending.get("email", "")
            phone_val = pending.get("phone", "")
            custom_vals = pending.get("custom_vals") or {}
            reg_now = datetime.now(timezone.utc)
            student = Student(
                session_id=session_id,
                name=pending.get("name", ""),
                email=email_val,
                phone=phone_val,
                custom_fields=custom_vals,
                registered_at=reg_now.isoformat(),
                agreed_to_policy=True,
                agreed_at=reg_now.isoformat(),
            )
            db.session.add(student)
            if s.status == "pending":
                s.status = "registered"

            # Durably record this registration for the host's rolling 24-hour
            # cap (atomic with the Student insert, using the same timestamp).
            _record_daily_registration(s.host_email, reg_now.isoformat())

            # --- Authoritative daily-cap gate (re-checked at commit time) ---
            # Even if this browser started registering before the limit was hit,
            # we must NOT create a new student once the host has reached their
            # 24-hour cap.
            if daily_registration_blocked(s.host_email):
                db.session.rollback()
                audit(
                    "daily_registration_limit_reached",
                    session_id=session_id,
                    step="verify",
                    host_email=s.host_email,
                    daily_limit=DAILY_REGISTRATION_LIMIT,
                    window_hours=DAILY_REGISTRATION_WINDOW_HOURS,
                    ip=get_remote_address(),
                )
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                err_msg = daily_limit_error()
                if request.is_json:
                    return jsonify({"error": err_msg}), 400
                return render_template("register.html", error=err_msg, form={}, captcha=captcha_payload(), **template_vars), 400

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                audit("registration_race_lost", session_id=session_id, ip=get_remote_address())
                session.pop("stu_reg_step", None)
                session.pop("stu_reg_pending", None)
                session.pop("stu_reg_otp", None)
                return redirect(url_for("exam", session_id=session_id))

            audit("registration_success", session_id=session_id, name=pending.get("name", ""), email=email_val, ip=get_remote_address())
            session.pop("stu_reg_step", None)
            session.pop("stu_reg_pending", None)
            session.pop("stu_reg_otp", None)
            return redirect(url_for("exam", session_id=session_id))

        # ---- Input step: process a fresh registration form ------------
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


        # --- Duplicate check: email OR phone must not already be used on the
        # SAME exam link (scoped per exam_id, NOT across the whole DB — the
        # same student may register for other exam links with the same
        # credentials). Legacy sessions without a parent Exam skip the check.
        email_val = student_data.get("email", "").strip().lower()
        phone_val = student_data.get("phone", "")
        student_data["email"] = email_val
        dup = _find_student_duplicate(s.exam_id, email_val, phone_val)
        if dup:
            if email_val and dup.email == email_val and phone_val and dup.phone == phone_val:
                err = "This email address and phone number are already registered."
            elif email_val and dup.email == email_val:
                err = "This email address is already registered. Please use a different one."
            else:
                err = "This phone number is already registered. Please use a different one."
            audit("registration_failed", session_id=session_id, email=email_val, phone=phone_val, reason="duplicate_student", ip=get_remote_address())
            if request.is_json:
                return jsonify({"error": err}), 400
            return render_template("register.html", error=err, form=request.form, captcha=captcha_payload(), **template_vars), 400

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
        # Everything except name/email/phone/registered_at goes into custom_fields
        custom_vals = {}
        for cf in custom_fields:
            slug = cf.get("slug") or slugify(cf.get("name", ""))
            if slug in student_data:
                custom_vals[slug] = student_data[slug]

        # --- Store pending data and email the verification OTP ----------
        # Email + phone were already validated and de-duplicated above.
        session["stu_reg_pending"] = {
            "session_id": session_id,
            "name": student_data.get("name", ""),
            "email": email_val,
            "phone": phone_val,
            "custom_vals": custom_vals,
        }
        _issue_session_otp("stu_reg_otp", email_val, is_email=True, channel="student")
        session["stu_reg_step"] = "verify"
        return redirect(url_for("register", session_id=session_id))

    # --- GET / render based on current step ---
    if step == "verify":
        pending = session.get("stu_reg_pending")
        if not pending or pending.get("session_id") != session_id:
            session.pop("stu_reg_step", None)
            session.pop("stu_reg_pending", None)
            session.pop("stu_reg_otp", None)
            return redirect(url_for("register", session_id=session_id))
        return render_template(
            "register_verify.html", email=pending["email"], session_id=session_id, error=None
        )

    return render_template(
        "register.html", error=None, form={}, captcha=captcha_payload(), **template_vars
    )

def _submit_exam(session_id):
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
    if request.data and request.get_json(silent=True) is None:
        return jsonify({"error": "Malformed or non-UTF8 JSON payload"}), 400
    payload = request.get_json(silent=True) or {}
    payload = _sanitize_null_bytes(payload)
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

    # A submission request may arrive for a session whose clock was never
    # started (the camera-verification gate / POST /start never ran). This must
    # NEVER bounce back with a rejection that provokes client-side alert
    # cascades (the historical "The exam has not started yet." 400 + alert ->
    # blur -> strike -> resubmit loop). Instead, back-fill the missing clock
    # fields (started_at now, plus a deadline so the deadline-enforcement below
    # has a sane value, using the session's configured time limit) and let the
    # normal submission flow finish, grade and finalize the attempt. The final
    # commit — the same transaction that persists the answers and completed_at —
    # also persists these back-filled fields. If genuine work exists (real
    # answers and/or recorded violations) this is a lost-/start race; if the
    # payload is empty it is a stray pre-exam auto-submit that escaped the
    # client guard — either way the session finalizes cleanly instead of being
    # left in a half-open state.
    if s.started_at is None:
        now_utc = datetime.now(timezone.utc)
        s.started_at = now_utc.isoformat()
        if not s.deadline:
            s.deadline = (
                now_utc
                + timedelta(
                    minutes=int((s.config or {}).get("time_limit_minutes", 30) or 30)
                )
            ).isoformat()
        audit(
            "submit_started_retroactively",
            session_id=session_id,
            reason="started_at_backfilled_on_submit",
            ip=get_remote_address(),
        )

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
            # Generous 3-minute grace period (180s) to prevent edge-case rejections
            # caused by network latency, cellular reconnection, or large payload uploads.
            grace = timedelta(seconds=180)
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

    # Anti-cheating: persist WHY this attempt was submitted when it was
    # forced out early for repeated security violations, so the host can
    # see it in the audit trail / exam_violations log AND on the Session
    # Details page (the strict proctoring-audit gate requires this flag).
    if s.flagged:
        s.auto_submitted = True
        audit(
            "exam_submitted_flag",  # submitted because the attempt was flagged
            session_id=session_id,
            reason="security_violation_threshold",
            violation_count=s.violation_count or 0,
            auto_submitted=True,
            ip=get_remote_address(),
        )
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
    total = len(session_questions)

    # ---- GRADE & PERSIST THE SUBMISSION ------------------------------------
    # For each dealt question, save the submitted answer and auto-grade MCQ.
    # Essay / coding answers are saved verbatim for manual review by the host.
    for pos, q in enumerate(session_questions):
        qtype = q.type or "mcq"
        raw = answers.get(str(pos))
        response = raw
        correct = None
        correct_index = None

        if qtype == "mcq":
            correct_index = q.correct_index
            try:
                selected = int(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                selected = None
            response = selected
            if selected is not None and correct_index is not None:
                correct = selected == correct_index
                if correct:
                    score += 1
            else:
                correct = False
        else:
            # Essay / coding -> saved verbatim, reviewed manually by the host.
            auto_graded_only = False
            response = "" if raw is None else str(raw)

        # UPSERT: Update existing Answer row if already saved via auto-save,
        # otherwise insert a new Answer row to avoid duplicates.
        ans_row = Answer.query.filter_by(
            session_id=session_id, position=pos
        ).first()
        if ans_row:
            ans_row.question_id = q.question_id
            ans_row.type = qtype
            ans_row.response = response
            ans_row.correct = correct
            ans_row.correct_index = correct_index
        else:
            try:
                with db.session.begin_nested():
                    db.session.add(
                        Answer(
                            session_id=session_id,
                            position=pos,
                            question_id=q.question_id,
                            type=qtype,
                            response=response,
                            correct=correct,
                            correct_index=correct_index,
                        )
                    )
            except IntegrityError:
                ans_row = Answer.query.filter_by(
                    session_id=session_id, position=pos
                ).first()
                if ans_row:
                    ans_row.question_id = q.question_id
                    ans_row.type = qtype
                    ans_row.response = response
                    ans_row.correct = correct
                    ans_row.correct_index = correct_index

    s.completed_at = datetime.now(timezone.utc).isoformat()

    if auto_graded_only:
        # Fully auto-graded (all MCQ): persist a numeric score.
        s.score = score
        s.total_selected = total
        percent = round((score / total) * 100) if total else 0
        manual_review = False
    else:
        # Contains essay/coding answers -> await manual review by the host.
        s.score = None
        s.total_selected = None
        percent = None
        manual_review = True

    db.session.commit()
    audit(
        "exam_submitted",
        session_id=session_id,
        score=s.score,
        total=s.total_selected,
        auto_graded_only=auto_graded_only,
        ip=get_remote_address(),
    )
    if s.host_email:
        write_tests_conducted(s.host_email)

    return jsonify(
        {
            "ok": True,
            "score": s.score,
            "total": s.total_selected,
            "percent": percent,
            "manual_review": manual_review,
            "session_id": session_id,
        }
    )


@app.route("/exam/<session_id>/submit", methods=["POST"])
@limiter.exempt
def submit_exam(session_id):
    """
    Route wrapper for POST /exam/<session_id>/submit.

    Delegates to _submit_exam() inside a try/except so an unexpected error can
    NEVER escape as an HTML error page — the JavaScript client always receives
    a JSON response that JSON.parse() can handle, for both success and failure.
    """
    try:
        return _submit_exam(session_id)
    except Exception as exc:  # noqa: BLE001 - the client expects JSON, always.
        db.session.rollback()
        traceback.print_exc()
        audit(
            "submit_exception",
            session_id=session_id,
            error=str(exc),
            ip=get_remote_address(),
        )
        return jsonify({"error": "An unexpected error occurred. Please try again."}), 500


# ===========================================================================
# EMERGENCY OPERATIONS CONSOLE (/mdvelo)
# Mobile-optimized, dark-themed (#0b0f19, #1e293b cards).
# Protected by master password + persisted brute-force lockout (AdminLockout).
# Strictly NO email/SMS OTP verification.
# Divided into 4 clean sections:
#   1. Failover & OTP
#   2. Host Moderation
#   3. Live Exam Operations
#   4. Blacklist / Security
# ===========================================================================

MDVELO_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#0b0f19">
    <title>Emergency Operations Console</title>
    <style>
        :root {
            --bg: #0b0f19;
            --card-bg: #1e293b;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --danger: #ef4444;
            --danger-bg: rgba(239, 68, 68, 0.15);
            --danger-border: rgba(239, 68, 68, 0.4);
            --radius: 14px;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            width: 100%;
            max-width: 420px;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 34px 26px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.65);
        }
        .shield-icon {
            width: 58px;
            height: 58px;
            background: linear-gradient(135deg, rgba(59, 130, 246, 0.25), rgba(37, 99, 235, 0.08));
            border: 1px solid rgba(59, 130, 246, 0.4);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 0 auto 16px auto;
            color: var(--primary);
            font-size: 28px;
        }
        h1 {
            font-size: 21px;
            font-weight: 700;
            text-align: center;
            letter-spacing: 0.5px;
            margin-bottom: 6px;
        }
        .subtitle {
            font-size: 13px;
            color: var(--text-muted);
            text-align: center;
            margin-bottom: 24px;
        }
        .alert {
            padding: 14px 16px;
            border-radius: 10px;
            font-size: 14px;
            line-height: 1.45;
            margin-bottom: 20px;
        }
        .alert-danger {
            background: var(--danger-bg);
            border: 1px solid var(--danger-border);
            color: #fca5a5;
        }
        .alert-lockout {
            background: rgba(239, 68, 68, 0.22);
            border: 1px solid var(--danger);
            color: #fecaca;
            text-align: center;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input[type="password"] {
            width: 100%;
            height: 50px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 0 16px;
            color: #fff;
            font-size: 16px;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        input[type="password"]:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.25);
        }
        .btn-submit {
            width: 100%;
            height: 50px;
            background: var(--primary);
            border: none;
            border-radius: 10px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            transition: background 0.2s, transform 0.1s;
        }
        .btn-submit:active {
            transform: scale(0.98);
        }
        .btn-submit:hover {
            background: var(--primary-hover);
        }
        .footer-note {
            margin-top: 24px;
            font-size: 12px;
            color: var(--text-muted);
            text-align: center;
            line-height: 1.5;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="shield-icon">⚡</div>
            <h1>EMERGENCY OPS</h1>
            <div class="subtitle">Operational Master Key Authorization</div>

            {% if is_locked %}
                <div class="alert alert-lockout">
                    <strong style="display:block; font-size:15px; margin-bottom:4px;">🔒 Security Lockout Active</strong>
                    Maximum failed attempts reached. Access is locked for 30 minutes.
                    <div style="margin-top: 10px; font-weight:700; font-size: 18px; color:#fff;">
                        Remaining: {{ rem_minutes }} min
                    </div>
                </div>
            {% else %}
                {% if error %}
                    <div class="alert alert-danger">{{ error }}</div>
                {% endif %}

                <form method="POST" action="/mdvelo">
                    <input type="hidden" name="action" value="login">
                    <div class="form-group">
                        <label for="master_password">Master Password</label>
                        <input type="password" id="master_password" name="master_password" placeholder="Enter master key" required autofocus autocomplete="off">
                    </div>
                    <button type="submit" class="btn-submit">
                        <span>Authorize Console</span> →
                    </button>
                </form>
            {% endif %}

            <div class="footer-note">
                Strict master-key protocol. Strictly zero SMS/Email OTP.<br>
                3 incorrect attempts trigger an immediate 30-minute lock.
            </div>
        </div>
    </div>
</body>
</html>
"""

MDVELO_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#0b0f19">
    <title>MDVELO Emergency Ops Console</title>
    <style>
        :root {
            --bg: #0b0f19;
            --card-bg: #1e293b;
            --card-sub: #131f33;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --danger: #ef4444;
            --danger-hover: #dc2626;
            --danger-bg: rgba(239, 68, 68, 0.16);
            --warning: #f59e0b;
            --warning-hover: #d97706;
            --warning-bg: rgba(245, 158, 11, 0.16);
            --success: #10b981;
            --success-hover: #059669;
            --success-bg: rgba(16, 185, 129, 0.16);
            --radius: 12px;
        }
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            min-height: 100vh;
            padding-bottom: 40px;
        }
        header {
            background: #111827;
            border-bottom: 1px solid var(--border);
            padding: 14px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            position: sticky;
            top: 0;
            z-index: 100;
        }
        .header-brand {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .header-brand h1 {
            font-size: 16px;
            font-weight: 700;
            letter-spacing: 0.5px;
        }
        .session-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--success-bg);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #6ee7b7;
            padding: 4px 10px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 600;
        }
        .dot {
            width: 7px;
            height: 7px;
            background: #10b981;
            border-radius: 50%;
            display: inline-block;
        }
        .btn-logout {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-muted);
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-logout:hover {
            color: #fff;
            border-color: #64748b;
        }
        .content {
            max-width: 1000px;
            margin: 20px auto;
            padding: 0 16px;
            display: flex;
            flex-direction: column;
            gap: 22px;
        }
        .flash-messages {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .flash {
            padding: 14px 16px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 500;
            line-height: 1.4;
        }
        .flash-success {
            background: var(--success-bg);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #a7f3d0;
        }
        .flash-warning {
            background: var(--warning-bg);
            border: 1px solid rgba(245, 158, 11, 0.35);
            color: #fde68a;
        }
        .flash-danger {
            background: var(--danger-bg);
            border: 1px solid rgba(239, 68, 68, 0.35);
            color: #fecaca;
        }
        .flash-info {
            background: rgba(59, 130, 246, 0.16);
            border: 1px solid rgba(59, 130, 246, 0.35);
            color: #bfdbfe;
        }
        .card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 22px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.3);
        }
        .card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--border);
        }
        .section-tag {
            font-size: 12px;
            font-weight: 700;
            color: var(--primary);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 4px;
        }
        .card-title {
            font-size: 17px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .card-subtitle {
            font-size: 13px;
            color: var(--text-muted);
            margin-bottom: 18px;
            line-height: 1.45;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 4px 9px;
            border-radius: 6px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .badge-active, .badge-success {
            background: var(--success-bg);
            color: #6ee7b7;
            border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .badge-inactive, .badge-secondary {
            background: rgba(148, 163, 184, 0.16);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.3);
        }
        .badge-suspended, .badge-warning {
            background: var(--warning-bg);
            color: #fde68a;
            border: 1px solid rgba(245, 158, 11, 0.35);
        }
        .badge-banned, .badge-danger {
            background: var(--danger-bg);
            color: #fca5a5;
            border: 1px solid rgba(239, 68, 68, 0.35);
        }
        .controls-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
            gap: 16px;
        }
        .subcard {
            background: var(--card-sub);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 16px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            gap: 14px;
        }
        .subcard-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
        }
        .subcard-title {
            font-size: 14px;
            font-weight: 600;
            color: #fff;
        }
        .subcard-desc {
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.4;
        }
        .btn {
            min-height: 48px;
            padding: 0 16px;
            border-radius: 8px;
            border: none;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            text-decoration: none;
            transition: all 0.2s;
            width: 100%;
        }
        .btn:active {
            transform: scale(0.98);
        }
        .btn-primary {
            background: var(--primary);
            color: #fff;
        }
        .btn-primary:hover {
            background: var(--primary-hover);
        }
        .btn-danger {
            background: var(--danger);
            color: #fff;
        }
        .btn-danger:hover {
            background: var(--danger-hover);
        }
        .btn-warning {
            background: var(--warning);
            color: #111827;
        }
        .btn-warning:hover {
            background: var(--warning-hover);
        }
        .btn-success {
            background: var(--success);
            color: #fff;
        }
        .btn-success:hover {
            background: var(--success-hover);
        }
        .btn-outline {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-main);
        }
        .btn-sm {
            min-height: 38px;
            font-size: 12px;
            padding: 0 12px;
            width: auto;
        }
        input[type="text"], select {
            height: 48px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 0 12px;
            color: #fff;
            font-size: 15px;
            width: 100%;
        }
        input[type="text"]:focus, select:focus {
            outline: none;
            border-color: var(--primary);
        }
        .table-responsive {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            margin-top: 10px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }
        th {
            background: #0f172a;
            color: var(--text-muted);
            padding: 12px 14px;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 12px 14px;
            border-bottom: 1px solid var(--border);
            color: var(--text-main);
            vertical-align: middle;
        }
        tr:last-child td {
            border-bottom: none;
        }
        .actions-cell {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }
        .grid-form {
            display: grid;
            grid-template-columns: 140px 1fr 1fr auto;
            gap: 10px;
        }
        @media (max-width: 640px) {
            .grid-form {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <header>
        <div class="header-brand">
            <span style="font-size: 20px;">⚡</span>
            <h1>MDVELO OPS CONSOLE</h1>
        </div>
        <div style="display: flex; align-items: center; gap: 12px;">
            <div class="session-badge">
                <span class="dot"></span>
                <span>Session Active ({{ session_remaining_minutes }}m)</span>
            </div>
            <form method="POST" action="/mdvelo" style="margin: 0;">
                <input type="hidden" name="action" value="logout">
                <button type="submit" class="btn-logout">Exit</button>
            </form>
        </div>
    </header>

    <div class="content">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                <div class="flash-messages">
                    {% for category, message in messages %}
                        <div class="flash flash-{{ category }}">{{ message }}</div>
                    {% endfor %}
                </div>
            {% endif %}
        {% endwith %}

        <!-- ================================================================ -->
        <!-- SECTION 1: FAILOVER & OTP DISPATCH                               -->
        <!-- ================================================================ -->
        <div class="card">
            <div class="section-tag">Section 1</div>
            <div class="card-header">
                <div class="card-title">📡 Email & OTP Dispatch Failover</div>
            </div>
            <div class="card-subtitle">
                Emergency communications failover overrides. Bypass Brevo API directly to the Render OTP Agent webhook, activate universal 15-minute emergency OTP, or flush failed queues.
            </div>
            <div class="controls-grid">
                <!-- Force Agent Toggle -->
                <div class="subcard">
                    <div>
                        <div class="subcard-head">
                            <span class="subcard-title">Force Render Agent Webhook</span>
                            {% if force_agent_active %}
                                <span class="badge badge-active">Active</span>
                            {% else %}
                                <span class="badge badge-inactive">Inactive</span>
                            {% endif %}
                        </div>
                        <div class="subcard-desc" style="margin-top: 8px;">
                            Bypasses all Brevo API calls completely and pushes every host/candidate verification code directly to the Render OTP Agent webhook.
                        </div>
                    </div>
                    <form method="POST" action="/mdvelo">
                        <input type="hidden" name="action" value="toggle_force_agent_otp">
                        {% if force_agent_active %}
                            <button type="submit" class="btn btn-warning">Restore Standard Brevo API</button>
                        {% else %}
                            <button type="submit" class="btn btn-primary">⚡ Force Direct Render Webhook</button>
                        {% endif %}
                    </form>
                </div>

                <!-- Master Universal OTP -->
                <div class="subcard">
                    <div>
                        <div class="subcard-head">
                            <span class="subcard-title">Master Universal OTP (999888)</span>
                            {% if universal_otp_active %}
                                <span class="badge badge-active">{{ universal_otp_remaining_minutes }}m left</span>
                            {% else %}
                                <span class="badge badge-inactive">Inactive</span>
                            {% endif %}
                        </div>
                        <div class="subcard-desc" style="margin-top: 8px;">
                            {% if universal_otp_active %}
                                Code <strong>{{ universal_otp_code }}</strong> is currently accepted for all candidate and host verification checks (expires in {{ universal_otp_remaining_minutes }}m).
                            {% else %}
                                Enables a universal OTP "999888" accepted for 15 minutes by all candidate/host verification handlers in case email delivery fails completely.
                            {% endif %}
                        </div>
                    </div>
                    <form method="POST" action="/mdvelo">
                        <input type="hidden" name="action" value="toggle_universal_otp">
                        {% if universal_otp_active %}
                            <button type="submit" class="btn btn-danger">Deactivate Universal OTP</button>
                        {% else %}
                            <input type="hidden" name="universal_code" value="999888">
                            <button type="submit" class="btn btn-success">Activate Master OTP (15 Min)</button>
                        {% endif %}
                    </form>
                </div>

                <!-- Resend Queue Flush -->
                <div class="subcard">
                    <div>
                        <div class="subcard-head">
                            <span class="subcard-title">Resend Queue Flush</span>
                            {% if failed_otp_count > 0 %}
                                <span class="badge badge-danger">{{ failed_otp_count }} Failed</span>
                            {% else %}
                                <span class="badge badge-success">0 Failed</span>
                            {% endif %}
                        </div>
                        <div class="subcard-desc" style="margin-top: 8px;">
                            Queries all OTP logs recorded with status 'FAILED' in the last 30 minutes and re-dispatches them immediately via the Tier 3 Render OTP Agent webhook.
                        </div>
                    </div>
                    <form method="POST" action="/mdvelo">
                        <input type="hidden" name="action" value="resend_queue_flush">
                        <button type="submit" class="btn btn-primary">⚡ Flush Resend Queue (Tier 3)</button>
                    </form>
                </div>
            </div>
        </div>

        <!-- ================================================================ -->
        <!-- SECTION 2: HOST ACCOUNT MODERATION & BLOCKING                    -->
        <!-- ================================================================ -->
        <div class="card">
            <div class="section-tag">Section 2</div>
            <div class="card-header">
                <div class="card-title">👤 Host Account Moderation & Blocking</div>
                <span class="badge badge-secondary">{{ hosts|length }} Hosts</span>
            </div>
            <div class="card-subtitle">
                Account moderation controls. Suspended or banned host accounts cannot log in, generate exams, and their active exams are blocked from launching.
            </div>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Host Email</th>
                            <th>Status</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for h in hosts %}
                            <tr>
                                <td>
                                    <strong>{{ h.email }}</strong>
                                    {% if h.name %}
                                        <div style="font-size: 11px; color: var(--text-muted);">{{ h.name }}</div>
                                    {% endif %}
                                    {% if h.status == 'suspended' and h.suspended_until %}
                                        <div style="font-size: 11px; color: #fde68a;">Suspended until: {{ h.suspended_until.strftime('%Y-%m-%d %H:%M') }} UTC</div>
                                    {% endif %}
                                </td>
                                <td>
                                    {% if h.is_banned or h.status == 'banned' %}
                                        <span class="badge badge-banned">Banned</span>
                                    {% elif h.status == 'suspended' %}
                                        <span class="badge badge-suspended">Suspended</span>
                                    {% else %}
                                        <span class="badge badge-active">Active</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <div class="actions-cell">
                                        {% if h.status != 'active' or h.is_banned %}
                                            <form method="POST" action="/mdvelo" style="display:inline;">
                                                <input type="hidden" name="action" value="host_action">
                                                <input type="hidden" name="host_id" value="{{ h.id }}">
                                                <input type="hidden" name="action_type" value="restore">
                                                <button type="submit" class="btn btn-sm btn-success">Restore Active</button>
                                            </form>
                                        {% endif %}
                                        {% if not h.is_banned and h.status != 'banned' %}
                                            <form method="POST" action="/mdvelo" style="display:inline;">
                                                <input type="hidden" name="action" value="host_action">
                                                <input type="hidden" name="host_id" value="{{ h.id }}">
                                                <input type="hidden" name="action_type" value="suspend_24h">
                                                <button type="submit" class="btn btn-sm btn-warning">Suspend 24h</button>
                                            </form>
                                            <form method="POST" action="/mdvelo" style="display:inline;">
                                                <input type="hidden" name="action" value="host_action">
                                                <input type="hidden" name="host_id" value="{{ h.id }}">
                                                <input type="hidden" name="action_type" value="suspend_7d">
                                                <button type="submit" class="btn btn-sm btn-warning">Suspend 7d</button>
                                            </form>
                                            <form method="POST" action="/mdvelo" style="display:inline;" onsubmit="return confirm('PERMANENTLY BAN host ' + {{ h.email|tojson }} + '? All their active exams will be unpublished and attempts revoked.');">
                                                <input type="hidden" name="action" value="host_action">
                                                <input type="hidden" name="host_id" value="{{ h.id }}">
                                                <input type="hidden" name="action_type" value="ban">
                                                <button type="submit" class="btn btn-sm btn-danger">Permanent Ban</button>
                                            </form>
                                        {% endif %}
                                    </div>
                                </td>
                            </tr>
                        {% else %}
                            <tr>
                                <td colspan="3" style="text-align: center; color: var(--text-muted); padding: 20px;">No registered host accounts found.</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ================================================================ -->
        <!-- SECTION 3: LIVE EXAM & ATTEMPT CONTROLS                          -->
        <!-- ================================================================ -->
        <div class="card">
            <div class="section-tag">Section 3</div>
            <div class="card-header">
                <div class="card-title">📝 Live Exam & Attempt Controls</div>
            </div>
            <div class="card-subtitle">
                Manage ongoing exams. Force-submit active attempts currently in progress, or re-trigger / download bulk submission PDF/ZIP bundles.
            </div>

            <!-- Force Submit Input Form -->
            <form method="POST" action="/mdvelo" style="margin-bottom: 20px;" onsubmit="return confirm('Force submit all active in-progress attempts for this exam?');">
                <input type="hidden" name="action" value="force_submit_exam">
                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 240px;">
                        <input type="text" name="exam_id" placeholder="Enter Exam ID (e.g. e08e9c0c...)" required>
                    </div>
                    <button type="submit" class="btn btn-warning" style="width: auto; padding: 0 24px;">⚡ Force-Submit Active Attempts</button>
                </div>
            </form>

            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Exam ID / Title</th>
                            <th>Organizer</th>
                            <th>Submissions / Attempts</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for ex in recent_exams %}
                            <tr>
                                <td>
                                    <strong>{{ ex.config.get('exam_title', 'Untitled') }}</strong>
                                    <div style="font-family: monospace; font-size: 11px; color: var(--text-muted);">{{ ex.id }}</div>
                                </td>
                                <td>
                                    <div style="font-size: 12px;">{{ ex.host_email }}</div>
                                </td>
                                <td>
                                    <span class="badge badge-secondary">{{ ex.sessions|length }} total</span>
                                    {% set active_cnt = ex.sessions|selectattr("status", "ne", "completed")|list|length %}
                                    {% if active_cnt > 0 %}
                                        <span class="badge badge-warning">{{ active_cnt }} active</span>
                                    {% endif %}
                                </td>
                                <td>
                                    <div class="actions-cell">
                                        <form method="POST" action="/mdvelo" style="display:inline;" onsubmit="return confirm('Force submit all in-progress attempts for exam: ' + {{ ex.id|tojson }} + '?');">
                                            <input type="hidden" name="action" value="force_submit_exam">
                                            <input type="hidden" name="exam_id" value="{{ ex.id }}">
                                            <button type="submit" class="btn btn-sm btn-warning">Force Submit</button>
                                        </form>
                                        <a href="/mdvelo/export/{{ ex.id }}" class="btn btn-sm btn-primary" style="display:inline-flex;">Download ZIP</a>
                                    </div>
                                </td>
                            </tr>
                        {% else %}
                            <tr>
                                <td colspan="4" style="text-align: center; color: var(--text-muted); padding: 20px;">No exams found.</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ================================================================ -->
        <!-- SECTION 4: SECURITY & ACCESS BLACKLISTS                          -->
        <!-- ================================================================ -->
        <div class="card">
            <div class="section-tag">Section 4</div>
            <div class="card-header">
                <div class="card-title">🛡️ Security & Access Blacklists</div>
            </div>
            <div class="card-subtitle">
                Restricts malicious roll numbers or client IP addresses, and provides instant candidate session token revocation.
            </div>

            <div class="controls-grid" style="margin-bottom: 20px;">
                <!-- Instant Candidate Session Purge -->
                <div class="subcard" style="grid-column: 1 / -1;">
                    <div class="subcard-head">
                        <span class="subcard-title">Instant Session Purge</span>
                        <span class="badge badge-danger">Revocation</span>
                    </div>
                    <div class="subcard-desc">
                        Revokes and clears active candidate attempt session tokens by Roll Number or Candidate Email. Candidates currently taking tests will be booted immediately. Enter "ALL" to revoke all sessions globally.
                    </div>
                    <form method="POST" action="/mdvelo" style="margin-top: 8px;" onsubmit="return confirm('Execute candidate session purge for the entered identifier?');">
                        <input type="hidden" name="action" value="instant_session_purge">
                        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                            <div style="flex: 1; min-width: 240px;">
                                <input type="text" name="identifier" placeholder="Roll Number or Candidate Email (or 'ALL')" required>
                            </div>
                            <button type="submit" class="btn btn-danger" style="width: auto; padding: 0 22px;">⚡ Revoke Session</button>
                        </div>
                    </form>
                    <div style="font-size: 11px; color: var(--text-muted); margin-top: 6px;">
                        Global purge timestamp: <strong>{{ last_session_purge or 'None' }}</strong>
                    </div>
                </div>
            </div>

            <!-- Add Blacklist Form -->
            <form method="POST" action="/mdvelo" style="margin-bottom: 16px;">
                <input type="hidden" name="action" value="add_blacklist">
                <div class="grid-form">
                    <select name="entry_type">
                        <option value="roll">Roll Number</option>
                        <option value="ip">IP Address</option>
                    </select>
                    <input type="text" name="value" placeholder="Identifier (e.g. 21CS04 or 192.168.1.5)" required>
                    <input type="text" name="reason" placeholder="Reason (e.g. Cheating attempt)">
                    <button type="submit" class="btn btn-primary btn-sm" style="height: 48px; padding: 0 20px;">Add to Blacklist</button>
                </div>
            </form>

            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Type</th>
                            <th>Value</th>
                            <th>Reason</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in blacklist_entries %}
                            <tr>
                                <td>
                                    <span class="badge badge-secondary">{{ item.get_kind()|upper if item.get_kind else (item.type|upper) }}</span>
                                </td>
                                <td><strong>{{ item.value }}</strong></td>
                                <td style="color: var(--text-muted);">{{ item.reason or 'N/A' }}</td>
                                <td>
                                    <form method="POST" action="/mdvelo" style="display:inline;">
                                        <input type="hidden" name="action" value="remove_blacklist">
                                        <input type="hidden" name="entry_id" value="{{ item.id }}">
                                        <button type="submit" class="btn btn-sm btn-danger">Remove</button>
                                    </form>
                                </td>
                            </tr>
                        {% else %}
                            <tr>
                                <td colspan="4" style="text-align: center; color: var(--text-muted); padding: 20px;">No active blacklist entries.</td>
                            </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

    </div>
</body>
</html>
"""


@app.route("/mdvelo", methods=["GET", "POST"])
@csrf.exempt
@limiter.exempt
def mdvelo():
    """
    Hidden, mobile-optimized emergency administration console.
    Protected by master password with persisted brute-force lockout (AdminLockout).
    Strictly NO email/SMS OTP verification.
    """
    ops_master_password = os.environ.get("OPS_MASTER_PASS", "mdhaseeb2407")

    # Check brute-force lockout
    is_locked, rem_minutes = check_admin_lockout()
    if is_locked:
        return (
            render_template_string(
                MDVELO_LOGIN_HTML,
                is_locked=True,
                rem_minutes=rem_minutes,
                error=None,
            ),
            429,
        )

    # Handle Master Password Login POST
    if request.method == "POST" and not is_ops_authenticated():
        action = request.form.get("action", "login")
        if action == "login":
            entered_password = request.form.get("master_password", "")
            if hmac.compare_digest(entered_password.strip(), ops_master_password.strip()):
                record_successful_admin_login()
                session["ops_authenticated"] = True
                session["ops_auth_time"] = datetime.now(timezone.utc).isoformat()
                flash("Operations Console authorized.", "success")
                audit("admin_auth_success", ip=get_remote_address())
                return redirect(url_for("mdvelo"))
            else:
                is_locked_now, rem_attempts = record_failed_admin_attempt()
                if is_locked_now:
                    audit("admin_lockout_triggered", ip=get_remote_address())
                    return (
                        render_template_string(
                            MDVELO_LOGIN_HTML,
                            is_locked=True,
                            rem_minutes=30,
                            error=None,
                        ),
                        429,
                    )
                audit("admin_auth_failed", remaining_attempts=rem_attempts, ip=get_remote_address())
                return (
                    render_template_string(
                        MDVELO_LOGIN_HTML,
                        is_locked=False,
                        rem_minutes=0,
                        error=f"Incorrect master key. {rem_attempts} attempt(s) remaining before 30-minute lockout.",
                    ),
                    401,
                )

    # Render login screen if unauthenticated
    if not is_ops_authenticated():
        return render_template_string(
            MDVELO_LOGIN_HTML,
            is_locked=False,
            rem_minutes=0,
            error=None,
        )

    # Calculate remaining session time (valid for 60m)
    session_rem_min = 60
    auth_time_str = session.get("ops_auth_time")
    if auth_time_str:
        try:
            at = datetime.fromisoformat(auth_time_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - at).total_seconds()
            session_rem_min = max(1, int((3600 - elapsed + 59) // 60))
        except Exception:
            session_rem_min = 60

    # Handle Authenticated Emergency Actions
    if request.method == "POST":
        action = request.form.get("action")

        if action == "logout":
            session.pop("ops_authenticated", None)
            session.pop("ops_auth_time", None)
            flash("Operations session closed.", "info")
            audit("admin_logout", ip=get_remote_address())
            return redirect(url_for("mdvelo"))

        elif action == "toggle_force_agent_otp":
            cur_state = is_force_agent_otp_enabled()
            new_state = "false" if cur_state else "true"
            set_system_setting("force_agent_otp", new_state)
            audit("admin_toggle_force_agent_otp", enabled=(new_state == "true"), ip=get_remote_address())
            flash(
                f"Direct Render OTP Agent webhook override is now {'ENABLED (Brevo bypassed)' if new_state == 'true' else 'DISABLED (Brevo restored)'}.",
                "success" if new_state == "true" else "warning",
            )
            return redirect(url_for("mdvelo"))

        elif action == "toggle_universal_otp":
            is_active, _, _ = is_master_universal_otp_active()
            if is_active:
                set_system_setting("master_otp_active", "false")
                set_system_setting("master_universal_otp", "false", expires_at=None)
                audit("admin_disable_universal_otp", ip=get_remote_address())
                flash("Master Universal OTP has been DEACTIVATED.", "warning")
            else:
                code = request.form.get("universal_code", "999888").strip() or "999888"
                exp = datetime.now(timezone.utc) + timedelta(minutes=15)
                set_system_setting("master_universal_otp_code", code)
                set_system_setting("master_otp_active", "true")
                set_system_setting("master_otp_expires", exp.isoformat())
                set_system_setting("master_universal_otp", "true", expires_at=exp)
                audit("admin_enable_universal_otp", code=code, expires_at=exp.isoformat(), ip=get_remote_address())
                flash(f"Master Universal OTP '{code}' is now ACTIVE for 15 minutes.", "success")
            return redirect(url_for("mdvelo"))

        elif action == "resend_queue_flush":
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
            failed_logs = OtpLog.query.filter(
                OtpLog.status == "FAILED",
                OtpLog.created_at >= cutoff
            ).all()

            resent_count = 0
            for flog in failed_logs:
                ok = _trigger_tier3_webhook(recipient_email=flog.recipient_email, otp_code=flog.otp_code)
                if ok:
                    flog.status = "RESENT"
                    resent_count += 1
            db.session.commit()
            audit("admin_resend_queue_flushed", count=resent_count, ip=get_remote_address())
            flash(f"Resend Queue Flush: Re-dispatched {resent_count} failed OTP(s) via Tier 3 agent.", "success" if resent_count > 0 else "info")
            return redirect(url_for("mdvelo"))

        elif action == "host_action":
            host_id = request.form.get("host_id")
            act_type = request.form.get("action_type")
            host_user = db.session.get(HostUser, host_id) if host_id else None
            if not host_user:
                flash("Host account not found.", "danger")
            else:
                now_utc = datetime.now(timezone.utc)
                if act_type == "suspend_24h":
                    host_user.status = "suspended"
                    host_user.suspended_until = now_utc + timedelta(hours=24)
                    host_user.is_banned = False
                    db.session.commit()
                    audit("admin_host_suspended_24h", host_email=host_user.email, ip=get_remote_address())
                    flash(f"Host '{host_user.email}' suspended for 24 hours.", "warning")
                elif act_type == "suspend_7d":
                    host_user.status = "suspended"
                    host_user.suspended_until = now_utc + timedelta(days=7)
                    host_user.is_banned = False
                    db.session.commit()
                    audit("admin_host_suspended_7d", host_email=host_user.email, ip=get_remote_address())
                    flash(f"Host '{host_user.email}' suspended for 7 days.", "warning")
                elif act_type == "ban":
                    host_user.status = "banned"
                    host_user.is_banned = True
                    host_user.suspended_until = None
                    # Unpublish all active exams
                    exams = Exam.query.filter_by(host_email=host_user.email).all()
                    for ex in exams:
                        cfg = dict(ex.config or {})
                        cfg["is_published"] = False
                        cfg["status"] = "unpublished"
                        cfg["archived"] = True
                        ex.config = cfg
                    # Revoke active sessions
                    active_sess = Session.query.filter(
                        Session.host_email == host_user.email,
                        Session.status.notin_(["completed", "SUBMITTED"])
                    ).all()
                    now_iso = now_utc.isoformat()
                    for asess in active_sess:
                        asess.status = "SUBMITTED"
                        asess.completed_at = now_iso
                        asess.auto_submitted = True
                    db.session.commit()
                    audit("admin_host_banned", host_email=host_user.email, ip=get_remote_address())
                    flash(f"Host '{host_user.email}' PERMANENTLY BANNED. Exams unpublished and active sessions revoked.", "danger")
                elif act_type == "restore":
                    host_user.status = "active"
                    host_user.is_banned = False
                    host_user.suspended_until = None
                    db.session.commit()
                    audit("admin_host_restored", host_email=host_user.email, ip=get_remote_address())
                    flash(f"Host '{host_user.email}' status restored to ACTIVE.", "success")
            return redirect(url_for("mdvelo"))

        elif action == "force_submit_exam":
            target_exam_id = request.form.get("exam_id", "").strip()
            if not target_exam_id:
                flash("Exam ID is required to force-submit attempts.", "danger")
            else:
                now_utc = datetime.now(timezone.utc)
                now_iso = now_utc.isoformat()
                count = 0
                for st in ['IN_PROGRESS', 'in_progress', 'started', 'registered']:
                    c = ExamAttempt.query.filter_by(exam_id=target_exam_id, status=st).update(
                        {
                            'status': 'SUBMITTED',
                            'submitted_at': now_utc,
                            'completed_at': now_iso,
                            'auto_submitted': True
                        },
                        synchronize_session='fetch'
                    )
                    count += c
                db.session.commit()
                audit("admin_force_submit_exam", exam_id=target_exam_id, count=count, ip=get_remote_address())
                flash(f"Force-submitted {count} active attempt(s) for exam '{target_exam_id}'.", "success")
            return redirect(url_for("mdvelo"))

        elif action == "retrigger_bulk_export":
            target_exam_id = request.form.get("exam_id", "").strip()
            if not target_exam_id:
                flash("Exam ID is required for bulk export.", "danger")
            else:
                ex = db.session.get(Exam, target_exam_id)
                if not ex:
                    flash(f"Exam '{target_exam_id}' not found.", "danger")
                else:
                    sub_count = Session.query.filter_by(exam_id=target_exam_id).filter(Session.student.has()).count()
                    audit("admin_retrigger_bulk_export", exam_id=target_exam_id, count=sub_count, ip=get_remote_address())
                    flash(f"Bulk export re-triggered for Exam '{target_exam_id}' ({sub_count} submissions ready).", "success")
            return redirect(url_for("mdvelo"))

        elif action == "add_blacklist":
            b_type = request.form.get("entry_type") or request.form.get("type") or "roll"
            b_type = "roll" if "roll" in b_type.lower() else "ip"
            b_value = request.form.get("value", "").strip()
            b_reason = request.form.get("reason", "").strip() or "Restricted via Emergency Console"
            if not b_value:
                flash("Value is required to add a blacklist entry.", "danger")
            else:
                exists = db.session.query(BlacklistEntry).filter(
                    db.or_(
                        BlacklistEntry.entry_type == b_type,
                        BlacklistEntry.type == b_type,
                        BlacklistEntry.type == ("roll_number" if b_type == "roll" else "ip")
                    ),
                    db.func.lower(BlacklistEntry.value) == b_value.lower()
                ).first()
                if exists:
                    flash(f"'{b_value}' is already blacklisted under {b_type.upper()}.", "warning")
                else:
                    new_entry = BlacklistEntry(
                        entry_type=b_type,
                        type="roll_number" if b_type == "roll" else "ip",
                        value=b_value,
                        reason=b_reason,
                        created_at=datetime.now(timezone.utc).isoformat()
                    )
                    db.session.add(new_entry)
                    db.session.commit()
                    audit("admin_blacklist_added", type=b_type, value=b_value, ip=get_remote_address())
                    flash(f"Added {b_type.upper()} restriction for '{b_value}'.", "success")
            return redirect(url_for("mdvelo"))

        elif action == "remove_blacklist":
            entry_id = request.form.get("entry_id")
            entry = db.session.get(BlacklistEntry, entry_id) if entry_id else None
            if entry:
                audit("admin_blacklist_removed", type=entry.entry_type or entry.type, value=entry.value, ip=get_remote_address())
                db.session.delete(entry)
                db.session.commit()
                flash("Blacklist entry removed.", "info")
            return redirect(url_for("mdvelo"))

        elif action == "instant_session_purge":
            target_identifier = request.form.get("identifier", "").strip()
            now_utc = datetime.now(timezone.utc)
            now_iso = now_utc.isoformat()
            if not target_identifier or target_identifier.upper() == "ALL":
                set_system_setting("candidate_session_purged_at", now_iso)
                active_sess = Session.query.filter(Session.status.notin_(["completed", "SUBMITTED"])).all()
                for asess in active_sess:
                    asess.status = "SUBMITTED"
                    asess.completed_at = now_iso
                    asess.auto_submitted = True
                db.session.commit()
                audit("admin_session_purge_global", purged_at=now_iso, ip=get_remote_address())
                flash("⚡ GLOBAL CANDIDATE SESSION PURGE EXECUTED. All active candidate sessions have been terminated.", "danger")
            else:
                count = purge_candidate_identifier(target_identifier)
                audit("admin_session_purge_targeted", identifier=target_identifier, affected=count, ip=get_remote_address())
                flash(f"⚡ Candidate session purge executed for '{target_identifier}'. Revoked {count} active attempt session(s).", "danger")
            return redirect(url_for("mdvelo"))

    # GET Dashboard data
    force_agent_active = is_force_agent_otp_enabled()
    univ_active, univ_code, univ_rem = is_master_universal_otp_active()
    cutoff_30m = datetime.now(timezone.utc) - timedelta(minutes=30)
    failed_otp_count = OtpLog.query.filter(OtpLog.status == "FAILED", OtpLog.created_at >= cutoff_30m).count()
    last_session_purge = get_system_setting("candidate_session_purged_at")
    hosts = HostUser.query.order_by(HostUser.id.desc()).limit(100).all()
    blacklist_entries = BlacklistEntry.query.order_by(BlacklistEntry.id.desc()).all()
    recent_exams = Exam.query.order_by(Exam.created_at.desc()).limit(30).all()

    return render_template_string(
        MDVELO_DASHBOARD_HTML,
        session_remaining_minutes=session_rem_min,
        force_agent_active=force_agent_active,
        universal_otp_active=univ_active,
        universal_otp_code=univ_code,
        universal_otp_remaining_minutes=univ_rem,
        failed_otp_count=failed_otp_count,
        last_session_purge=last_session_purge,
        hosts=hosts,
        blacklist_entries=blacklist_entries,
        recent_exams=recent_exams,
    )


@app.route("/mdvelo/export/<exam_id>")
@csrf.exempt
@limiter.exempt
def mdvelo_export_zip(exam_id):
    """Direct bulk submission export from the emergency operations console."""
    if not is_ops_authenticated():
        flash("Please authenticate to access exports.", "danger")
        return redirect(url_for("mdvelo"))
    ex = db.session.get(Exam, exam_id)
    if not ex:
        flash("Exam not found.", "danger")
        return redirect(url_for("mdvelo"))
    all_sessions = (
        Session.query.filter_by(exam_id=exam_id)
        .order_by(Session.created_at.asc())
        .all()
    )
    sessions = [s for s in all_sessions if s.student is not None]
    if not sessions:
        flash(f"No candidate submissions found for exam '{exam_id}'.", "warning")
        return redirect(url_for("mdvelo"))

    root_folder = f"Exam_{exam_id[:8]}_Submissions"
    zip_buf = io.BytesIO()
    used_filenames = set()

    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for idx, s in enumerate(sessions, start=1):
            data = session_to_dict(s)
            st = data.get("student") or {}
            raw_name = (st.get("name") or "Candidate").strip()
            clean_name = sanitize_filename(raw_name)
            raw_id = (
                st.get("roll_number")
                or st.get("roll_no")
                or st.get("registration_number")
                or st.get("reg_no")
                or st.get("phone")
                or s.id[:8]
            )
            clean_id = sanitize_filename(str(raw_id).strip())
            base_fname = f"{idx:02d}_{clean_name}_{clean_id}"
            fname = f"{base_fname}.pdf"
            counter = 1
            while fname in used_filenames:
                fname = f"{base_fname}_{counter}.pdf"
                counter += 1
            used_filenames.add(fname)
            try:
                pdf_bytes = _pdf_bytes(
                    "details_pdf.html",
                    session=data,
                    custom_registration_fields=data.get("custom_registration_fields") or [],
                )
                if pdf_bytes is not None:
                    archive.writestr(f"{root_folder}/{fname}", pdf_bytes)
                else:
                    pdf_fallback = _build_result_pdf(data)
                    archive.writestr(f"{root_folder}/{fname}", pdf_fallback)
            except Exception as pdf_exc:
                print(f"[Export ERROR] {pdf_exc}")

    zip_buf.seek(0)
    audit("admin_export_downloaded", exam_id=exam_id, submissions=len(sessions), ip=get_remote_address())
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{root_folder}.zip",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)

