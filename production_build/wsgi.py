"""
WSGI entrypoint for the Exam Platform.

Used by production WSGI servers:

    # Linux / macOS (multi-worker — the recommended production deployment)
    gunicorn --workers 4 --threads 2 --bind 0.0.0.0:8000 wsgi:app

    # Windows (gunicorn is Unix-only) — use waitress instead:
    waitress-serve --listen=0.0.0.0:8000 wsgi:app

See README.md -> "Deployment with Gunicorn" for the full guide.
"""

from app import app, init_db

# Initialize the database tables when the WSGI app starts.
# Under Gunicorn with multiple workers, each worker process calls this
# once on startup. SQLite's WAL mode + busy_timeout handles concurrent
# access safely.
init_db()

if __name__ == "__main__":
    app.run()

