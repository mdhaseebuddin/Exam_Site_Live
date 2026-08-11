# Exam Platform — Production Build

This folder is a clean, self-contained production copy of the Exam Platform.
It contains only the files needed to run the application in production.

## Contents

```
production_build/
├── app.py            # Flask application (routes, logic, config)
├── models.py         # SQLAlchemy models
├── schemas.py        # Marshmallow input-validation schemas
├── wsgi.py           # WSGI entrypoint (gunicorn / waitress)
├── requirements.txt  # Python dependencies
├── .env.example      # Environment-variable template (copy to .env)
├── .gitignore        # Prevents committing secrets / runtime data
├── templates/        # Jinja2 HTML templates
├── static/           # CSS / JS assets
└── instance/         # SQLite database location (created at runtime)
```

> **PDF Reporting:** Result and session-detail PDFs are generated server-side
> with **ReportLab** — a pure-Python library — so no system binary (e.g.
> `wkhtmltopdf`) is required on the host. This keeps PDF export working on any
> cloud provider and avoids deployment-time dependency issues.

## Setup

1. **Create a Python virtual environment** and install dependencies:

   ```bash
   cd production_build
   python -m venv venv
   # Windows:
   venv\Scripts\activate
   # Linux / macOS:
   source venv/bin/activate

   pip install -r requirements.txt
   ```

   > **Note:** `gunicorn` is installed only on Linux/macOS; `waitress` is used
   > as the Windows fallback (already handled in `requirements.txt`).

2. **Configure environment variables:**

   ```bash
   cp .env.example .env
   # then edit .env with real values (especially SECRET_KEY and SMTP credentials)
   ```

   At minimum, set a strong `SECRET_KEY`. In production also set
   `FLASK_ENV="production"` and `SESSION_COOKIE_SECURE="true"`.

3. **Run the app with a production WSGI server.**

   Linux / macOS (multi-worker, recommended):

   ```bash
   gunicorn --workers 4 --threads 2 --bind 0.0.0.0:8000 wsgi:app
   ```

   Windows:

   ```bash
   waitress-serve --listen=0.0.0.0:8000 wsgi:app
   ```

The database tables are created automatically on startup by `wsgi.py`
(`init_db()`).

## Security notes

- The `.env` file and the `instance/`, `logs/`, and `data/` folders are
  git-ignored and never committed.
- Use a real, strong `SECRET_KEY` in production.
- Set `SESSION_COOKIE_SECURE="true"` when serving over HTTPS.
- Provide real `SMTP_*` credentials for working password-reset emails.
