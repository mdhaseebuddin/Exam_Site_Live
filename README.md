# Exam Platform

Flask + SQLAlchemy (SQLite) online examination platform with email-based OTP
password reset, student session OTPs, math CAPTCHA, and server-side PDF reports.

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

> **SQLite on cloud hosts:** on free-tier instances the filesystem is
> ephemeral, so `instance/exam.db` resets on redeploy. This is fine for a
> single running instance. `DATABASE_URL` is supported, but the app ships
> SQLite/WAL-specific SQL — SQLite is the default and recommended engine.
