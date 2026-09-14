# VeloTest Exam Platform — Technology Stack

Here is the complete, comprehensive breakdown of every tool, library, platform, and integration we have used across our entire chat history for your VeloTest exam platform:

## 🌐 Core Application Framework & Languages

- **Python Flask**: The lightweight Python web framework handling your server routing, views, request contexts, and application lifecycle.
- **HTML5 / CSS3 / JavaScript**: Used for building your frontend user interfaces, dashboards, welcome page, and layout templates.

## 🗄️ Database & Data Layer

- **SQLAlchemy & Flask-SQLAlchemy**: The ORM managing database models (users, hosts, exams, test records, candidate registrations).
- **`ExamViolation` model + proctoring columns on `Session`**: every security strike (`violation_type`, 1-based `count`, `detail`, base64 `snapshot`, `created_at`) is stored per attempt and foreign-keyed to the session, while `Session.violation_count`, `Session.flagged`, and `Session.auto_submitted` persist the server-authoritative tally, the checkmate flag, and whether the submission was forced by the 3-strike auto-submit rule.
- **SQLite**: The built-in file-based database used for seamless local development and testing.
- **Neon**: Your serverless cloud PostgreSQL database handling production data storage so accounts and records persist safely across logins.
- **psycopg2-binary**: The PostgreSQL adapter library enabling Python/SQLAlchemy to communicate with your Neon cloud database.

## 🔒 Security, Validation & State Management

- **Werkzeug**: Provides secure, salted cryptographic password hashing (`generate_password_hash` / `check_password_hash`).
- **Flask-WTF / CSRF Protection**: Protects your application forms and endpoints against Cross-Site Request Forgery.
- **Secure Cookies**: Uses HttpOnly and SameSite cookie configurations to protect user session states.

## ✉️ Communication & Third-Party APIs

- **Brevo (formerly Sendinblue) (`sib-api-v3-sdk`)**: The transactional email API handling outbound verification codes and OTP emails for your user authentication system.
- **ReportLab**: The pure-Python library utilized to programmatically generate downloadable PDF reports and exam results.

## ☁️ DevOps, Deployment & Uptime Monitoring

- **Render**: The cloud hosting provider running your web service (`velotest.onrender.com`) on Python 3.11.
- **GitHub**: Your version control repository hosting your project codebase (`Exam_Site_Live`).
- **Visual Studio Code (VS Code)**: Your primary code editor and local terminal environment.
- **UptimeRobot**: The automated external uptime monitoring service pinging your site every 5 minutes with HTTP requests to prevent Render's free tier from spinning down due to inactivity.

## ⚖️ Legal & Compliance Pages

- **Updated Privacy Policy (`privacy.html`)**: Expanded data collection disclosures covering account info, exam logs, secure password storage, and data retention, cleanly linked directly in your website footer.