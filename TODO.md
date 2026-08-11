# Exam Platform — Multi-Student Universal Portal Refactor

## Goal
Turn the shareable exam link into a universal registration portal. Each student
who opens it registers and gets their own isolated Session + timer, so 100+
students can take the same exam concurrently without overriding each other.

## Steps
- [x] 1. Fix pre-existing `host()` indentation bug in `app.py` (column-0 `host_user`).
- [x] 2. Rewrite `generate_session()` to create an `Exam` (portal) + full `exam_questions` snapshot; store exam config.
- [x] 3. Add `exam_register(exam_id)` route at `/exam/register/<exam_id>` — universal landing form that creates a fresh isolated per-student `Session`.
- [x] 4. Update `exam(session_id)` to redirect to the exam portal when a session has no student.
- [x] 5. Update `host()` to pass per-exam data for the dashboard.
- [x] 6. Update `download_all_zip` fallback render to the new `host()` signature.
- [x] 7. Update `templates/host.html` to iterate Exams (portal links) + per-student attempts.
- [x] 8. Update `templates/register.html` form action for the universal portal.
- [x] 9. Syntax-check with `python -m py_compile` and verify the app boots.

## Verification Results
- Python files compile OK (`COMPILE_OK`).
- App imports successfully; all routes registered including:
  - `/exam/register/<exam_id>` (universal portal)
  - `/host/exam/<exam_id>/attempts` (per-exam student attempts)
  - `/host/exam/<exam_id>/delete`
- End-to-end smoke test (via `test_client`) passed:
  1. Host registers (CSRF + CAPTCHA) → 302
  2. Add question → 302
  3. Generate exam → 302 (creates `Exam` with unique `exam_id`)
  4. Dashboard renders with "Generated Exams", `/exam/register/<id>` portal links, and "View Attempts"
  5. Student opens `/exam/register/<exam_id>` → 200
  6. Student registers (CSRF + CAPTCHA) → 302 → redirects to a unique `/exam/<session_id>`
  7. Exam page renders with the dealt question and injected `examConfig`

## New Architecture Summary
- **Exam** (new model) = the shareable universal entry point at `/exam/register/<exam_id>`.
- **ExamQuestion** (new model) = full immutable snapshot of the question bank for an Exam.
- **Session** now has an `exam_id` FK. Each registered student gets their OWN isolated `Session` (attempt) with its own dealt questions, timer, and answers — so 100+ students can take the exam concurrently without conflicting.
- **Host dashboard** lists Exams with attempt/completed counts and links to `/host/exam/<id>/attempts`.
- **Export** (`download_all_zip`) still collects every student submission across all of a host's sessions.
