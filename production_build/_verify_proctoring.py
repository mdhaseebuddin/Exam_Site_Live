"""Throwaway integration test for the per-exam proctoring feature.

Runs against a COPY of the production SQLite DB so the real data is never
mutated. Validates:
  1. init_db() migration adds Exam.enable_proctoring / Exam.max_violations.
  2. /exam/<session_id> renders proctored config (gate overlays present).
  3. /exam/<session_id> renders unproctored config (overlays absent).
  4. /exam/<session_id>/violation is a no-op for unproctored exams.
  5. /exam/<session_id>/violation honours the host's max_violations.
"""
import os
import shutil
import sys
import tempfile

BASE = r"C:\Users\Haseeb\OneDrive\Desktop\Exam_Site_Live\production_build"
SRC_DB = os.path.join(BASE, "instance", "exam.db")

tmp = tempfile.mkdtemp(prefix="proctortest_")
copy_db = os.path.join(tmp, "exam_test.db")
shutil.copy2(SRC_DB, copy_db)

os.environ["FLASK_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DB_PATH"] = copy_db
os.chdir(BASE)

# --- Stub the Brevo (sib_api_v3_sdk) SDK so the app imports without it
# installed; only the transactional-email flows use it, which this test never
# exercises. Same approach as the repo's _verify_*.py scripts.
STUB = """
class Configuration:
    def __init__(self):
        self.api_key = {}
class ApiClient:
    def __init__(self, conf):
        self.configuration = conf
class TransactionalEmailsApi:
    def __init__(self, client):
        self.client = client
    def send_transac_email(self, smtp):
        return None
class SendSmtpEmail:
    def __init__(self, **kw):
        self.kw = kw
"""
import types
_sdk = types.ModuleType("sib_api_v3_sdk")
for _name, _obj in {
    "Configuration": type("Configuration", (), {"__init__": lambda self: setattr(self, "api_key", {})}),
    "ApiClient": type("ApiClient", (), {"__init__": lambda self, conf: setattr(self, "configuration", conf)}),
    "TransactionalEmailsApi": type("TransactionalEmailsApi", (), {"__init__": lambda self, client: setattr(self, "client", client), "send_transac_email": lambda self, smtp: None}),
    "SendSmtpEmail": type("SendSmtpEmail", (), {"__init__": lambda self, **kw: setattr(self, "kw", kw)}),
}.items():
    setattr(_sdk, _name, _obj)
sys.modules["sib_api_v3_sdk"] = _sdk

import app as appmod
from app import app, db, init_db
from models import Exam, Session, Student

init_db()

# --- 1. verify the migration added both columns to the real (copied) table
with app.app_context():
    insp = db.inspect(db.engine)
    exam_cols = [c["name"] for c in insp.get_columns("exams")]
    assert "enable_proctoring" in exam_cols, exam_cols
    assert "max_violations" in exam_cols, exam_cols
    print("MIGRATION_OK columns:", [c for c in exam_cols if c in ("enable_proctoring", "max_violations")])

app.config["WTF_CSRF_ENABLED"] = False

client = app.test_client()

def make_attempt(exam_id, sess_id, proctoring, max_v, status="registered"):
    with app.app_context():
        ex = Exam(
            id=exam_id,
            host_email="host@example.com",
            enable_proctoring=proctoring,
            max_violations=max_v,
            config={
                "exam_title": "T-" + exam_id,
                "time_limit_minutes": 30,
                "ratio": 1,
                "max_capacity": 500,
                "custom_registration_fields": [],
                "required_fields": ["name", "phone"],
                "enable_proctoring": proctoring,
                "max_violations": max_v,
            },
            created_at="2026-01-01T00:00:00+00:00",
        )
        s = Session(
            id=sess_id,
            exam_id=exam_id,
            host_email="host@example.com",
            status=status,
            config={
                "exam_title": "T-" + exam_id,
                "time_limit_minutes": 30,
                "ratio": 1,
                "enable_proctoring": proctoring,
                "max_violations": max_v,
                "required_fields": ["name", "phone"],
            },
            created_at="2026-01-01T00:00:00+00:00",
        )
        st = Student(
            session_id=sess_id,
            name="Tester",
            email="tester@example.com",
            phone="123",
            custom_fields={},
            registered_at="2026-01-01T00:00:00+00:00",
            agreed_to_policy=True,
        )
        db.session.add_all([ex, s, st])
        db.session.commit()

    with client.session_transaction() as sxn:
        sxn[f"auth_{sess_id}"] = True

def fetch_exam_page(sess_id):
    resp = client.get("/exam/" + sess_id)
    return resp.status_code, resp.get_data(as_text=True)

# --- 2. proctored exam page (max_violations=5)
make_attempt("PROCTEST0001", "PROCSESS000001", True, 5)
code, html = fetch_exam_page("PROCSESS000001")
assert code == 200, code
assert '"enableProctoring": true' in html, "missing enableProctoring true"
assert '"maxViolations": 5' in html, "missing maxViolations 5"
assert '"violationThreshold": 5' in html, "missing violationThreshold 5"
assert 'id="cameraGateOverlay"' in html, "camera gate should render for proctored"
assert 'id="violationModal"' in html
print("PROCTORED_PAGE_OK")

# --- 3. unproctored exam page (max_violations=3 default)
make_attempt("UNPROTEST0002", "UNPROSESS0002", False, 3)
code, html = fetch_exam_page("UNPROSESS0002")
assert code == 200, code
assert '"enableProctoring": false' in html, "missing enableProctoring false"
assert '"maxViolations": 3' in html
assert 'id="cameraGateOverlay"' not in html, "camera gate must NOT render for unproctored"
assert 'id="violationModal"' not in html
assert 'id="fullscreenBlockOverlay"' not in html
assert 'id="forceSubmitOverlay"' not in html
print("UNPROCTORED_PAGE_OK")

# --- 4. violation endpoint no-op for unproctored exam
with app.app_context():
    s = db.session.get(Session, "UNPROSESS0002")
    s.status = "started"
    db.session.commit()
resp = client.post("/exam/UNPROSESS0002/violation", json={"type": "tab_switch", "detail": "x"})
data = resp.get_json()
assert resp.status_code == 200, resp.status_code
assert data.get("proctoring") is False, data
assert data.get("count") == 0, data
assert data.get("auto_submit") is False, data
with app.app_context():
    s = db.session.get(Session, "UNPROSESS0002")
    assert s.violation_count == 0, "unproctored session must never count strikes"
print("UNPROCTORED_VIOLATION_NOOP_OK")

# --- 5. violation endpoint honours max_violations=5 on a proctored exam
with app.app_context():
    s = db.session.get(Session, "PROCSESS000001")
    s.status = "started"
    db.session.commit()
for i in range(1, 6):
    resp = client.post("/exam/PROCSESS000001/violation", json={"type": "tab_switch", "detail": "x"})
    data = resp.get_json()
    assert data["count"] == i, data
    if i < 5:
        assert data["auto_submit"] is False, data
    else:
        assert data["auto_submit"] is True, data
print("PROCTORED_THRESHOLD_5_OK")

print("ALL_PROCTORING_TESTS_PASSED")

# --- 6. host.html renders with the new proctoring form controls
from flask import render_template
with app.test_request_context("/host"):
    host_html = render_template(
        "host.html",
        questions=[],
        exams=[],
        field_labels={},
        host=None,
    )
assert 'name="enable_proctoring"' in host_html, "missing enable_proctoring checkbox"
assert 'name="max_violations"' in host_html, "missing max_violations input"
assert 'webcam verification, tab-switch monitoring, shortcut blocking, and face-presence checks' in host_html, "missing proctoring note"
print("HOST_FORM_OK")
print("ALL_CHECKS_PASSED")
