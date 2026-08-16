import os, sys, tempfile, re

OUT = os.path.join(os.getcwd(), "_verify_out.txt")
def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")

# ---- Brevo SDK stub (programmable) ----
tmp = tempfile.mkdtemp()
sys.path.insert(0, tmp)
STUB = """
import re
class Configuration:
    def __init__(self):
        self.api_key = {}
class ApiClient:
    def __init__(self, conf):
        self.configuration = conf
class TransactionalEmailsApi:
    send_handler = None  # test installs a handler
    def __init__(self, client):
        self.client = client
        self.api_key = client.configuration.api_key.get("api-key")
    def send_transac_email(self, smtp):
        if TransactionalEmailsApi.send_handler is not None:
            TransactionalEmailsApi.send_handler(self, smtp)
class SendSmtpEmail:
    def __init__(self, **kw):
        self.kw = kw
        self.sender = kw.get("sender")
        self.to = kw.get("to")
"""
open(os.path.join(tmp, "sib_api_v3_sdk.py"), "w").write(STUB)

os.environ["FLASK_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DB_PATH"] = os.path.join(tmp, "test.db")

import sib_api_v3_sdk
import app as A

A.app.config["WTF_CSRF_ENABLED"] = False
A.init_db()

# Track emails sent through the real _send_student_otp / _send_otp_email paths.
emails_sent = []
host_emails_sent = []

def student_send_handler(api, smtp):
    to = (smtp.to or [{}])[0].get("email", "?")
    body = smtp.kw.get("text_content", "") or ""
    m = re.search(r"(\d{6})", body)
    emails_sent.append({"to": to, "code": m.group(1) if m else None, "key": api.api_key})

def fail_key1_succeed_key2(api, smtp):
    student_send_handler(api, smtp)
    if api.api_key == "KEY_1":
        raise RuntimeError("simulated key-1 failure")

sib_api_v3_sdk.TransactionalEmailsApi.send_handler = student_send_handler
A._send_otp_email = lambda email, code: (host_emails_sent.append(email), True)[1]

### UNIT: _send_student_otp key selection / failover / no-keys ###
for k in ("BREVO_STUDENT_API_KEY_1", "BREVO_STUDENT_API_KEY_2"):
    os.environ.pop(k, None)

# (a) no student keys -> returns False, nothing emailed
with A.app.test_request_context():
    rc = A._send_student_otp("no@keys.com", "123456")
assert rc is False, "expected False when no student keys"
emails_sent.clear()

# (b) failover: KEY_1 fails, KEY_2 succeeds
os.environ["BREVO_STUDENT_API_KEY_1"] = "KEY_1"
os.environ["BREVO_STUDENT_API_KEY_2"] = "KEY_2"
sib_api_v3_sdk.TransactionalEmailsApi.send_handler = fail_key1_succeed_key2
with A.app.test_request_context():
    rc = A._send_student_otp("failover@test.com", "654321")
assert rc is True, "failover should succeed via KEY_2"
assert emails_sent and emails_sent[-1]["to"] == "failover@test.com" and emails_sent[-1]["key"] == "KEY_2", emails_sent
log("UNIT_OK: no-keys fallback -> console; failover KEY_1->KEY_2 works; code emailed to student")

# (c) first key succeeds
sib_api_v3_sdk.TransactionalEmailsApi.send_handler = student_send_handler
emails_sent.clear()
with A.app.test_request_context():
    rc = A._send_student_otp("firstkey@test.com", "111111")
assert rc is True and emails_sent and emails_sent[-1]["key"] == "KEY_1", emails_sent
log("UNIT_OK: BREVO_STUDENT_API_KEY_1 used when healthy")

# Keep student keys set so the real OTP sender runs during the E2E flow.

### E2E setup ###
with A.app.app_context():
    ex = A.Exam(
        id="TESTEXAM001", host_email="host@example.com",
        config={"exam_title": "Test", "required_fields": ["name", "email", "phone"],
                "time_limit_minutes": 30, "ratio": 1},
    )
    A.db.session.add(ex)
    dup_sess = A.Session(id="sessdup00000001", exam_id="TESTEXAM001",
                         host_email="host@example.com", status="registered",
                         config={}, created_at="x")
    A.db.session.add(dup_sess)
    A.db.session.flush()
    dup_student = A.Student(session_id="sessdup00000001", name="Dup",
                            email="dup@example.com", phone="1111111111",
                            registered_at="x", agreed_to_policy=True)
    A.db.session.add(dup_student)
    A.db.session.commit()

client = A.app.test_client()

def get_captcha():
    with client.session_transaction() as s:
        return s.get("captcha_answer")
# 1) Form renders name/email/phone
r = client.get("/exam/register/TESTEXAM001")
assert r.status_code == 200, r.status_code
html = r.get_data(as_text=True)
assert 'name="email"' in html and 'name="phone"' in html and 'name="name"' in html
log("STEP1_OK: form renders name/email/phone")

# 2) Duplicate email rejected on input (against pre-existing student)
r = client.post("/exam/register/TESTEXAM001", data={
    "name": "Alice", "email": "dup@example.com", "phone": "2222222222",
    "captcha": get_captcha(), "agree": "1",
})
assert r.status_code == 400, (r.status_code, r.get_data(as_text=True)[:300])
log("STEP2_OK: duplicate email (existing DB student) rejected")

# 3) Duplicate phone rejected on input (against pre-existing student)
r = client.post("/exam/register/TESTEXAM001", data={
    "name": "Alice", "email": "alice@example.com", "phone": "1111111111",
    "captcha": get_captcha(), "agree": "1",
})
assert r.status_code == 400, (r.status_code, r.get_data(as_text=True)[:300])
log("STEP3_OK: duplicate phone (existing DB student) rejected")

# 4) Valid input -> real _send_student_otp emails the code, redirect to verify
emails_sent.clear()
client.get("/exam/register/TESTEXAM001")
r = client.post("/exam/register/TESTEXAM001", data={
    "name": "Alice", "email": "alice@example.com", "phone": "2222222222",
    "captcha": get_captcha(), "agree": "1",
}, follow_redirects=False)
assert r.status_code == 302, r.status_code
assert "/exam/register/TESTEXAM001" in r.headers.get("Location", "")
with client.session_transaction() as s:
    assert s.get("stu_reg_step") == "verify"
assert emails_sent and emails_sent[-1]["to"] == "alice@example.com", emails_sent
code = emails_sent[-1]["code"]
assert code and len(code) == 6
assert host_emails_sent == [], host_emails_sent
log(f"STEP4_OK: student OTP emailed to {emails_sent[-1]['to']} (channel=student, not host)")

# 5) Wrong OTP rejected
r = client.post("/exam/register/TESTEXAM001", data={"action": "verify", "otp": "000000"})
assert "Invalid verification code" in r.get_data(as_text=True)
log("STEP5_OK: wrong OTP rejected")

# 6) Correct OTP from the actually-sent email -> registration finalized
r = client.post("/exam/register/TESTEXAM001", data={"action": "verify", "otp": code},
                follow_redirects=False)
assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:300])
loc = r.headers.get("Location", "")
assert "/exam/" in loc and "register" not in loc
with A.app.app_context():
    stu = A.Student.query.filter_by(email="alice@example.com").first()
    assert stu is not None and stu.phone == "2222222222" and stu.session_id is not None
log("STEP6_OK: correct OTP -> student persisted with email+phone")

# 7) Duplicate email blocked across DB after OTP registration
r = client.post("/exam/register/TESTEXAM001", data={
    "name": "Bob", "email": "alice@example.com", "phone": "3333333333",
    "captcha": get_captcha(), "agree": "1",
})
assert r.status_code == 400, (r.status_code, r.get_data(as_text=True)[:300])
log("STEP7_OK: duplicate email blocked (across DB, past OTP)")

# 8) Duplicate phone blocked across DB after OTP registration
r = client.post("/exam/register/TESTEXAM001", data={
    "name": "Carol", "email": "carol@example.com", "phone": "2222222222",
    "captcha": get_captcha(), "agree": "1",
})
assert r.status_code == 400, (r.status_code, r.get_data(as_text=True)[:300])
log("STEP8_OK: duplicate phone blocked (across DB, past OTP)")

log("ALL_TESTS_PASSED")
print("ALL_TESTS_PASSED")