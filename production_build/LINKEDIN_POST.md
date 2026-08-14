# LinkedIn Post — VeloTest Launch Announcement

> **How to post:** Attach the **VeloTest trailer video** along with this text (LinkedIn
> allows one video + a document/file). If your company or personal page supports a
> featured-media carousel, the trailer can be uploaded as the primary visual. The
> draft below is written to be read *alongside* the trailer, so it opens with a hook
> that pairs with the visuals.

---

## ✍️ Post Draft (copy & paste)

**🎬 From idea to first full-stack deployment — meet VeloTest!**

Earlier this year I set out to build something I could be proud to ship end-to-end: a
secure, scalable online exam platform. Today I'm thrilled to share that **VeloTest**
is ready for its upcoming deployment. 🚀

Built from scratch as my first full-stack web application, VeloTest lets educators
create a live question bank, generate a single shareable exam link, and grade
hundreds of students automatically — all on a security-first foundation.

**What makes VeloTest stand out:**

🔍 **Algorithmic grading** — Multiple-choice answers are auto-graded server-side in a
single atomic transaction, so scores are instant, accurate, and race-proof — even when
100+ students submit at the exact same moment. Essay & coding questions are neatly
flagged for manual review.

🔐 **Secure host & student workflows** — Email + CAPTCHA registration, rate-limited
login, timed OTP password resets, per-host data isolation, CSRF protection, hardened
cookies, and strict security headers on every response. Candidates pass a verified
registration gate and a tamper-proof server-side countdown, and **correct answers
never reach the browser** — grading always stays on the server.

⚙️ **Dynamic architecture** — A relational SQLite + SQLAlchemy schema where every exam
is a reusable entry point and *each student gets their own isolated attempt*. Question
banks are snapshotted per exam, questions and MCQ options are randomized per candidate,
and custom registration fields are generated dynamically at runtime.

**The stack that powers it:**
🐍 Python + Flask · 🗄️ SQLite + SQLAlchemy · 🛡️ Flask-WTF (CSRF) · ⏱️ Flask-Limiter ·
🧩 Marshmallow validation · 📄 ReportLab PDFs · 🎨 Bootstrap + Jinja2

This project taught me the full lifecycle — from relational schema design and
concurrency-safe transactions, to hardening a web app for production. I can't wait to
share the live link and walk through it in the trailer above. 🎥

If you build, teach, or run assessments — I'd love your thoughts. Drop a question or
a 💬 below, and follow along for the deployment announcement!

#VeloTest #FullStack #WebDevelopment #Flask #Python #SQLAlchemy #EdTech #MachineLearning
#SoftwareEngineering #100DaysOfCode #DevCommunity

---

## 🧭 Quick tips for posting

- **First line hook:** The trailer opens with the exam-in-action visuals, so the bold
  hook line right after "Earlier this year…" ties the video to the milestone.
- **Keep it skimmable:** The emoji-led bullets mirror what a recruiter scans for —
  features, security, tech stack.
- **Attach the trailer as the main media** (not an external link), since native video
  gets the highest reach on LinkedIn.
- **Replace placeholder words** (e.g., "upcoming deployment", "live link") with the real
  URL/date when you ship.

---

## ✂️ Shorter alternative (for a replies/companion comment)

> Building my first full-stack app from the DB up — **VeloTest** 🎓
> Flask + SQLite/SQLAlchemy, algorithmic grading, and security-first host/student
> flows. Trailer 🎥 above — the live deployment is coming soon!
> #VeloTest #Flask #Python #SQLAlchemy #EdTech
