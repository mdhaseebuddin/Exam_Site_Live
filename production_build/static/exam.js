/**
 * Exam Page logic — countdown timer, single-question navigation, submit.
 * Receives its configuration from window.__EXAM_CONFIG__ (injected by Flask).
 */
(function () {
  "use strict";

  const cfg = JSON.parse(document.getElementById("examConfig").textContent);
  const questions = cfg.questions;
  const total = cfg.totalQuestions;

let currentIndex = 0;
  let timerInterval = null;
  // Server-anchored timeline (absolute UTC Unix timestamps). The countdown is
  // ALWAYS derived from (deadline - serverNow), so the user's system clock and
  // page refreshes can never extend the exam.
  let deadlineUnix = cfg.deadlineUnix;
  let serverNowUnix = cfg.serverNowUnix;
  const answers = {}; // { question_index: selected_option_index }

  const timerEl = document.getElementById("timer");
  const questionText = document.getElementById("questionText");
  const optionsList = document.getElementById("optionsList");
  const questionCounter = document.getElementById("questionCounter");
  const progressText = document.getElementById("progressText");
  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");
  const submitBtn = document.getElementById("submitBtn");
  const resultBox = document.getElementById("resultBox");

  // ---------------------------- Timer ----------------------------
  function formatTime(secs) {
    const m = String(Math.floor(secs / 60)).padStart(2, "0");
    const s = String(secs % 60).padStart(2, "0");
    return m + ":" + s;
  }

// Compute the remaining seconds using ONLY the server-anchored timestamps.
  // The browser's Date.now() is never trusted for the countdown itself.
  function computeRemaining() {
    return Math.max(0, deadlineUnix - serverNowUnix);
  }

  function syncFromServer() {
    fetch("/exam/" + cfg.sessionId + "/time", { headers: { "Accept": "application/json" } })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (data && data.server_now_unix != null && data.deadline_unix != null) {
          // Re-anchor to the authoritative server clock.
          serverNowUnix = data.server_now_unix;
          deadlineUnix = data.deadline_unix;
          renderTimer();
        }
      })
      .catch(function () {
        // Network hiccup — ignore; the local countdown keeps running and the
        // next poll will re-sync. The server still enforces the deadline on
        // submit, so this cannot be exploited to gain time.
      });
  }

  function renderTimer() {
    const remaining = computeRemaining();
    timerEl.textContent = formatTime(remaining);
    if (remaining <= 0) {
      timerEl.classList.add("time-up");
      clearInterval(timerInterval);
      timerInterval = null;
      submitExam(true); // deadline passed -> auto-submit
    }
  }

  function startTimer() {
    renderTimer();
    // Tick every second; the displayed value is derived from the anchored
    // (deadline - serverNow) delta, so clock tampering cannot "stop" the timer.
    timerInterval = setInterval(() => {
      serverNowUnix += 1; // advance the server-anchored "now" by one second
      renderTimer();
    }, 1000);
    // Periodically re-sync the true server time (corrects drift and any
    // attempt to pause the browser tab from gaining extra time).
    setInterval(syncFromServer, 30000);
  }

  // ------------------------- Rendering ---------------------------
  function renderQuestion(index) {
    const q = questions[index];
    const qtype = q.type || "mcq";
    questionText.textContent = q.text;
    optionsList.innerHTML = "";

    if (qtype === "mcq") {
      (q.options || []).forEach((opt, oi) => {
        const label = document.createElement("label");
        label.className = "option-item d-flex align-items-center p-3 mb-2 border rounded";

        const input = document.createElement("input");
        input.type = "radio";
        input.name = "answer";
        input.value = oi;
        if (answers[index] === oi) input.checked = true;
        input.addEventListener("change", () => {
          answers[index] = oi;
        });

        const span = document.createElement("span");
        span.className = "ms-3";
        span.textContent = String.fromCharCode(65 + oi) + ". " + opt;

        label.appendChild(input);
        label.appendChild(span);
        optionsList.appendChild(label);
      });
    } else {
      // essay / coding -> free-text textarea
      const wrapper = document.createElement("div");
      wrapper.className = "mb-2";

      const hint = document.createElement("div");
      hint.className = "form-text mb-2";
      hint.textContent = qtype === "coding"
        ? "Write your code below."
        : "Write your answer below.";

      const textarea = document.createElement("textarea");
      textarea.className = "form-control";
      textarea.rows = qtype === "coding" ? 10 : 6;
      textarea.placeholder = qtype === "coding"
        ? "// your code here"
        : "Type your answer here...";
      textarea.style.fontFamily = qtype === "coding" ? "monospace" : "";
      textarea.value = answers[index] || "";
      textarea.addEventListener("input", () => {
        answers[index] = textarea.value;
      });

      wrapper.appendChild(hint);
      wrapper.appendChild(textarea);
      optionsList.appendChild(wrapper);
    }

    questionCounter.textContent = "Question " + (index + 1) + " of " + total;
    progressText.textContent = "Question " + (index + 1) + " of " + total;
    prevBtn.disabled = index === 0;
    nextBtn.textContent = index === total - 1 ? "Review" : "Next →";
  }

  prevBtn.addEventListener("click", () => {
    if (currentIndex > 0) {
      currentIndex -= 1;
      renderQuestion(currentIndex);
    }
  });

  nextBtn.addEventListener("click", () => {
    if (currentIndex < total - 1) {
      currentIndex += 1;
      renderQuestion(currentIndex);
    }
  });

  // ---------------------------- Submit ---------------------------
  async function submitExam(auto) {
    if (!auto) {
      const ok = window.confirm("Are you sure you want to submit your test?");
      if (!ok) return;
    }
    clearInterval(timerInterval);
    submitBtn.disabled = true;
    submitBtn.textContent = "Submitting...";

    try {
// Send the student's registration info AND their answers together.
      const res = await fetch("/exam/" + cfg.sessionId + "/submit", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // CSRF token — required by Flask-WTF global CSRF protection for
          // JSON POSTs (the token is sent in an HTTP header, not a form field).
          "X-CSRFToken": cfg.csrfToken
        },
        body: JSON.stringify({ student: cfg.student, answers: answers })
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || "Submission failed");
      showResult(data);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Finish";
      window.alert("Submission failed: " + err.message);
    }
  }

  function showResult(data) {
    document.querySelector(".exam-main").classList.add("d-none");
    document.querySelector(".exam-footer").classList.add("d-none");
    questionCounter.classList.add("d-none");
    submitBtn.classList.add("d-none");
    timerEl.classList.add("d-none");

    resultBox.style.display = "block";

    // If the exam contains essay/coding questions, there is no auto score —
    // the submission is saved and awaits manual review by the host.
    if (data.manual_review || data.percent === null || data.percent === undefined) {
      resultBox.innerHTML = `
        <div class="card text-center shadow-sm mt-4">
          <div class="card-body py-5">
            <h1 class="display-4">Thank You! Your exam has been submitted.</h1>
            <p class="lead">Your exam has been submitted successfully.</p>
            <p class="text-muted">Answers that require manual review have been saved for the examiner.</p>
          </div>
        </div>`;
    } else {
      const gradeColor = data.percent >= 60 ? "text-success" : "text-danger";
      resultBox.innerHTML = `
        <div class="card text-center shadow-sm mt-4">
          <div class="card-body py-5">
            <h1 class="display-3 ${gradeColor}">${data.percent}%</h1>
            <p class="lead">You scored <strong>${data.score}</strong> out of <strong>${data.total}</strong> questions.</p>
          </div>
        </div>`;
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  submitBtn.addEventListener("click", () => submitExam(false));

// --------------------------- Init ------------------------------
  renderQuestion(0);

  if (computeRemaining() <= 0) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    submitExam(true); // deadline already passed -> submit immediately
  } else {
    startTimer();
  }
})();

