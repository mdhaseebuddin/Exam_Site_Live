import os, sys, tempfile

OUT = os.path.join(os.getcwd(), "_host_auth_out.txt")
def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")

# ---- Brevo SDK stub (host OTP channel) ----
tmp = tempfile.mkdtemp()
sys.path.insert(0, tmp)
STUB = """\
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
        pass
class SendSmtpEmail:
    def __init__(self, **kw):
        self.kw = kw
        self.sender = kw.get("sender")
        self.to = kw.get("to")
"""
open(os.path.join(tmp, "sib_api_v3_sdk.py"), "w").write(STUB)

os.environ["FLASK_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DB_PATH"] = os.path.join(tmp, "auth.db")

import app as A

A.app.config["WTF_CSRF_ENABLED"] = False
A.init_db()

# Capture host OTP codes sent by _issue_session_otp via the host channel.
host_otp_codes = []
A._send_otp_email = lambda email, code: (host_otp_codes.append(code), True)[1]

client = A.app.test_client()

def get_captcha():
    with client.session_transaction() as s:
        return s.get("captcha_answer")

def register_host(name, email, password):
    client.get("/host/register")  # seed CAPTCHA
    r = client.post("/host/register", data={
        "name": name, "email": email, "password": password,
        "confirm": password, "captcha": get_captcha(), "agree": "1",
    }, follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:200])
    assert host_otp_codes, "OTP should have been issued"
    code = host_otp_codes[-1]
    r = client.post("/host/register", data={"action": "verify", "otp": code},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:200])
    return r

# ---- 1) Registration + hashing ----
register_host("Host One", "one@example.com", "secret1234")
with A.app.app_context():
    h = A.HostUser.query.filter_by(email="one@example.com").first()
    assert h is not None and A.verify_password("secret1234", h.password_hash), "hash mismatch"
log("STEP1_OK: host registration creates account; password hash verifies")

# log out
with client.session_transaction() as s:
    s.pop(A.SESSION_HOST_EMAIL, None)

# ---- 2) Valid credentials log in successfully (full flow) ----
host_otp_codes.clear()
client.get("/host/login")  # seed CAPTCHA
r = client.post("/host/login", data={
    "email": "one@example.com", "password": "secret1234",
    "captcha": get_captcha(), "agree": "1",
}, follow_redirects=False)
assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:200])
assert host_otp_codes, "login OTP should have been issued"
login_code = host_otp_codes[-1]
r = client.post("/host/login", data={"action": "verify", "otp": login_code},
                follow_redirects=False)
with client.session_transaction() as s:
    assert s.get(A.SESSION_HOST_EMAIL) == "one@example.com", "not logged in"
log("STEP2_OK: valid host credentials log in (no more 'invalid email or password')")

# log out
with client.session_transaction() as s:
    s.pop(A.SESSION_HOST_EMAIL, None)

# ---- 3) Duplicate registration (input step): flash + redirect to login ----
host_otp_codes.clear()
client.get("/host/register")
r = client.post("/host/register", data={
    "name": "Dupe", "email": "one@example.com", "password": "secret5678",
    "confirm": "secret5678", "captcha": get_captcha(), "agree": "1",
}, follow_redirects=False)
assert r.status_code == 302 and "/host/login" in r.headers.get("Location", ""), (r.status_code, r.headers.get("Location", ""))
assert host_otp_codes == [], "no OTP should be sent for a duplicate registration"
r = client.get("/host/login")
assert "Account already exists. Please log in." in r.get_data(as_text=True), r.get_data(as_text=True)[:300]
log("STEP3_OK: duplicate registration -> flash exact message + redirect to login")

# ---- 4) Duplicate registration (verify step): stale pending is blocked ----
with client.session_transaction() as s:
    s["reg_step"] = "verify"
    s["reg_pending"] = {"email": "one@example.com", "name": "Stale",
                        "password_hash": A.hash_password("stale-password")}
    s["reg_otp"] = {"code_hash": A.hash_password("654321"),
                    "generated_at": A.datetime.utcnow()}
r = client.post("/host/register", data={"action": "verify", "otp": "654321"},
                follow_redirects=False)
assert r.status_code == 302 and "/host/login" in r.headers.get("Location", ""), (r.status_code, r.get_data(as_text=True)[:200])
with A.app.app_context():
    cnt = A.HostUser.query.filter_by(email="one@example.com").count()
    assert cnt == 1, f"duplicate row created: {cnt}"
log("STEP4_OK: verify-step duplicate check prevents a second account; no 500")

# ---- 5) Wrong password is still rejected ----
client.get("/host/login")
r = client.post("/host/login", data={
    "email": "one@example.com", "password": "wrongpassword",
    "captcha": get_captcha(), "agree": "1",
}, follow_redirects=False)
assert r.status_code == 200 and "Invalid email or password." in r.get_data(as_text=True), (r.status_code, r.get_data(as_text=True)[:200])
log("STEP5_OK: wrong password still rejected with 'Invalid email or password.'")

log("ALL_HOST_AUTH_TESTS_PASSED")
print("ALL_HOST_AUTH_TESTS_PASSED")