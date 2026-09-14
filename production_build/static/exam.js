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
  // Submission guards: prevent a duplicate auto-submit (timer tick or server
  // sync re-triggering submitExam) from sending a second POST once time runs
  // out. The backend rejects the second as "already submitted", which would
  // otherwise surface a spurious "Submission failed" alert even though the
  // exam was saved.
  let submitting = false;
  let submitted = false;

  // ---- Anti-cheating / browser-lockdown state -------------------------------
  // The violation threshold and current tally are SERVER-authoritative: the
  // page receives the persisted count (cfg.violationCount) so a page refresh
  // can never reset a student's violation tally. The server re-anchors it via
  // the /time and /violation endpoints.
  const MAX_VIOLATIONS = cfg.violationThreshold || 3;
  let violationCount = cfg.violationCount || 0;
  // True once (a) the server confirms the threshold, (b) the local tally hits
  // it, or (c) the server reports the attempt flagged. The exam is then
  // force-submitted and the student is redirected to the results page.
  let forcedSubmitRequested = false;
  let redirectScheduled = false;
  // Debounce: a single physical "switch away" fires blur AND visibilitychange
  // (and possibly a fullscreen-exit) at once — those must count as ONE
  // incident, not three.
  let lastViolationReportAt = 0;
  const VIOLATION_COOLDOWN_MS = 1500;
  let focusLost = false;

  const timerEl = document.getElementById("timer");
  const questionText = document.getElementById("questionText");
  const optionsList = document.getElementById("optionsList");
  const questionCounter = document.getElementById("questionCounter");
  const progressText = document.getElementById("progressText");
  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");
  const submitBtn = document.getElementById("submitBtn");
  const resultBox = document.getElementById("resultBox");

  // ==== Anti-cheating: overlays & lockdown module ============================
  const violationOverlay = document.getElementById("violationModal");
  const violationModalText = document.getElementById("violationModalText");
  const violationModalCount = document.getElementById("violationModalCount");
  const violationAckBtn = document.getElementById("violationAckBtn");
  const fullscreenBlockOverlay = document.getElementById("fullscreenBlockOverlay");
  const fullscreenRetryBtn = document.getElementById("fullscreenRetryBtn");
  const forceSubmitOverlay = document.getElementById("forceSubmitOverlay");
  const forceSubmitText = document.getElementById("forceSubmitText");

  function hideViolationOverlays() {
    if (violationOverlay) violationOverlay.style.display = "none";
    if (fullscreenBlockOverlay) fullscreenBlockOverlay.style.display = "none";
    if (forceSubmitOverlay) forceSubmitOverlay.style.display = "none";
  }

  // -------------------------- Fullscreen enforcement ---------------------
  function supportsFullscreen() {
    return !!(document.documentElement.requestFullscreen ||
      document.documentElement.webkitRequestFullscreen ||
      document.documentElement.mozRequestFullScreen ||
      document.documentElement.msRequestFullscreen);
  }

  function isFullscreen() {
    return !!(document.fullscreenElement ||
      document.webkitFullscreenElement ||
      document.mozFullScreenElement ||
      document.msFullscreenElement);
  }

  function requestFullscreen() {
    if (!supportsFullscreen() || isFullscreen()) return;
    const el = document.documentElement;
    const req = el.requestFullscreen ||
      el.webkitRequestFullscreen ||
      el.mozRequestFullScreen ||
      el.msRequestFullscreen;
    try {
      req.call(el);
    } catch (err) {
      // Browsers require a user gesture — retried on the next click anyway.
    }
  }

  function exitFullscreenQuietly() {
    if (!supportsFullscreen() || !isFullscreen()) return;
    const doc = document;
    const fn = doc.exitFullscreen ||
      doc.webkitExitFullscreen ||
      doc.mozCancelFullScreen ||
      doc.msExitFullscreen;
    try {
      if (fn) fn.call(doc);
    } catch (err) {
      // best effort
    }
  }

  // Re-enter fullscreen on ANY user gesture (capture phase) while the exam is
  // active. This is the workhorse that makes exiting fullscreen hard: even if
  // Esc / F11 kicks the student out, the very next click puts them back in.
  document.addEventListener("click", function () {
    if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
  }, true);
  document.addEventListener("keydown", function () {
    if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
  }, true);

  function onFullscreenChange() {
    if (submitting || submitted || redirectScheduled) return;
    if (!isFullscreen()) {
      // Exiting fullscreen is a security flag. Report it and block the page
      // behind the "Enter Fullscreen" overlay (its button is a user gesture,
      // so re-entry always succeeds).
      reportViolation("fullscreen_exit", "Exited fullscreen mode (Esc / F11 / gesture)");
      if (fullscreenBlockOverlay) fullscreenBlockOverlay.style.display = "flex";
    } else if (fullscreenBlockOverlay) {
      fullscreenBlockOverlay.style.display = "none";
    }
  }
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  document.addEventListener("mozfullscreenchange", onFullscreenChange);
  document.addEventListener("MSFullscreenChange", onFullscreenChange);

  if (fullscreenRetryBtn) {
    fullscreenRetryBtn.addEventListener("click", function () {
      requestFullscreen(); // runs inside a user gesture -> always allowed
    });
  }

  // -------------------------- Violation reporting ------------------------
  // Best-effort proctoring proof capture: grabs one small webcam frame as a
  // JPEG data-URL at the moment a strike is recorded. Pure webcam (no external
  // library) so it works offline; degrades to `null` when the camera is
  // unavailable or the student denies permission — a snapshot-less strike is
  // still counted and timestamped server-side.
  let captureStream = null;

  function stopCaptureStream() {
    if (captureStream) {
      captureStream.getTracks().forEach(function (t) { t.stop(); });
      captureStream = null;
    }
  }

  function captureSnapshot(done) {
    let finished = false; // idempotency guard: two finish(null) paths exist
    function finish(url) {
      if (finished) return;
      finished = true;
      stopCaptureStream();
      done(url);
    }
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia
    ) {
      done(null);
      return;
    }
    try {
      navigator.mediaDevices.getUserMedia(
        { video: { width: 320, height: 240 }, audio: false }
      )
        .then(function (stream) {
          captureStream = stream;
          const video = document.createElement("video");
          video.muted = true;
          video.playsInline = true;
          video.srcObject = stream;
          // Watchdog so a hung camera never blocks the violation report.
          const failTimer = window.setTimeout(function () {
            window.clearTimeout(failTimer);
            finish(null);
          }, 1500);
          video.addEventListener("loadedmetadata", function () {
            video.play().catch(function () {});
            // Give the sensor a frame or two to deliver pixels, then draw.
            window.setTimeout(function () {
              window.clearTimeout(failTimer);
              try {
                const canvas = document.createElement("canvas");
                canvas.width = 320;
                canvas.height = 240;
                canvas.getContext("2d").drawImage(video, 0, 0, 320, 240);
                finish(canvas.toDataURL("image/jpeg", 0.4));
              } catch (err) {
                finish(null);
              }
            }, 350);
          });
        })
        .catch(function () { finish(null); });
    } catch (err) {
      finish(null);
    }
  }

  function reportViolation(type, detail) {
    // Once a submission is underway, stop counting incidents.
    if (submitting || submitted || redirectScheduled) return;

    // Debounce: blur + visibilitychange + fullscreen-exit from the SAME
    // physical switch count as a single incident.
    const nowMs = Date.now();
    if (nowMs - lastViolationReportAt < VIOLATION_COOLDOWN_MS) return;
    lastViolationReportAt = nowMs;

    // Capture a proof frame first (bounded by the watchdog), then report the
    // strike together with it so the host gets a chronological audited trail.
    captureSnapshot(function (snapshot) {
      if (submitting || submitted || redirectScheduled) return;
      postViolation(type, detail, snapshot);
    });
  }

  function postViolation(type, detail, snapshot) {
    const body = { type: type, detail: detail || "" };
    if (snapshot && snapshot.length <= 320000) body.snapshot = snapshot;

    fetch("/exam/" + cfg.sessionId + "/violation", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // CSRF token — required by Flask-WTF global CSRF protection for JSON.
        "X-CSRFToken": cfg.csrfToken
      },
      body: JSON.stringify(body)
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; });
      })
      .then(function (data) {
        if (data && typeof data.count === "number") {
          violationCount = Math.max(violationCount, data.count);
        } else {
          violationCount += 1;
        }
        handleViolationResult(data);
      })
      .catch(function () {
        // Network hiccup — keep the lockdown strict locally. The server stays
        // authoritative: the /time sync re-anchors the tally from the DB.
        violationCount += 1;
        handleViolationResult({});
      });
  }

  function handleViolationResult(data) {
    if (submitting || submitted) return;
    // Server confirmed the threshold (or we hit it locally) -> force submit.
    if ((data && data.auto_submit) || violationCount >= MAX_VIOLATIONS) {
      forceSubmitExam();
      return;
    }
    showWarningModal(violationCount);
  }

  // -------------------------- Warning modal ------------------------------
  function showWarningModal(count) {
    if (submitting || submitted) return;
    if (!violationOverlay) return;
    const remaining = Math.max(0, MAX_VIOLATIONS - count);
    if (violationModalCount) {
      violationModalCount.textContent =
        "Security incident " + count + " of " + MAX_VIOLATIONS +
        (remaining > 0
          ? " — your exam will be submitted automatically after " + remaining + " more."
          : " — your exam is being submitted.");
    }
    if (violationModalText) {
      violationModalText.textContent =
        "Warning: Leaving the exam tab is prohibited and will be reported. " +
        "Do not switch tabs, minimize this window, open other programs, " +
        "right-click, or open developer tools while the exam is active.";
    }
    violationOverlay.style.display = "flex";
    requestFullscreen();
  }

  if (violationAckBtn) {
    violationAckBtn.addEventListener("click", function () {
      if (violationOverlay) violationOverlay.style.display = "none";
      requestFullscreen();
    });
  }

  // -------------------------- Forced final submission --------------------
  function scheduleResultRedirect() {
    if (redirectScheduled) return;
    redirectScheduled = true;
    stopCaptureStream();
    // Tiny delay lets the client render the score before the server-side
    // result page replaces the exam page.
    window.setTimeout(function () {
      window.location.href = "/exam/" + cfg.sessionId;
    }, 200);
  }

  function forceSubmitExam() {
    if (submitted || redirectScheduled) return;
    forcedSubmitRequested = true;
    hideViolationOverlays();
    if (forceSubmitOverlay) {
      forceSubmitOverlay.style.display = "flex";
      if (forceSubmitText) {
        forceSubmitText.textContent =
          "Your exam has been flagged for repeated security violations and " +
          "is being submitted automatically.";
      }
    }
    if (submitting) return; // an auto-submit is already in flight -> it redirects
    submitExam(true);
  }

  // -------------------------- Tab / focus detection ----------------------
  function onVisibilityChange() {
    if (document.hidden) {
      focusLost = true;
      if (!submitting && !submitted) {
        reportViolation("tab_switch", "Tab switched away / page hidden");
      }
    } else {
      focusLost = false;
      if (!submitting && !submitted) requestFullscreen();
    }
  }

  document.addEventListener("visibilitychange", onVisibilityChange);

  window.addEventListener("blur", function () {
    if (submitting || submitted) return;
    // `blur` fires both when a tab is switched AND when the user clicks into
    // another application while the tab stays visible. Report it either way.
    if (!focusLost) {
      focusLost = true;
      reportViolation("focus_loss", "Window lost focus (switched app / tab)");
    }
  });

  window.addEventListener("focus", function () {
    focusLost = false;
    if (!submitting && !submitted) requestFullscreen();
  });

  // -------------------------- Clipboard / input lockdown -----------------
  // Right-click -> no context menu, no "inspect element".
  document.addEventListener("contextmenu", function (e) {
    e.preventDefault();
    if (!submitting && !submitted) {
      reportViolation("contextmenu", "Right-click / inspect attempt blocked");
    }
    return false;
  });

  // Native copy / cut / paste events.
  ["copy", "cut", "paste"].forEach(function (evtName) {
    document.addEventListener(evtName, function (e) {
      e.preventDefault();
      if (!submitting && !submitted) {
        reportViolation("copy_paste", "Clipboard operation blocked: " + evtName);
      }
    });
  });

  // Keyboard shortcuts: cut/copy/paste, view-source, save/print, devtools,
  // fullscreen toggles and refresh are all locked down while the exam runs.
  document.addEventListener("keydown", function (e) {
    const key = (e.key || "").toLowerCase();
    const ctrl = e.ctrlKey || e.metaKey;

    const isDevtoolsKey =
      e.key === "F12" ||
      (ctrl && e.shiftKey && (key === "i" || key === "j" || key === "c" || key === "k")) ||
      (ctrl && (key === "u" || key === "s" || key === "p")) ||
      (e.key === "F5" || (ctrl && key === "r"));
    const isClipboardKey = ctrl && (key === "c" || key === "x" || key === "v");
    const isFullscreenToggle = e.key === "F11";

    if (isDevtoolsKey || isClipboardKey || isFullscreenToggle) {
      e.preventDefault();
      e.stopPropagation();
      if (!submitting && !submitted && !e.repeat) {
        const what = isDevtoolsKey
          ? "Developer tools / save / print / refresh shortcut"
          : isFullscreenToggle
            ? "Fullscreen toggle shortcut"
            : "Clipboard shortcut";
        reportViolation("devtools_shortcut", what + " blocked: " + e.key);
      }
      return false;
    }
    return true;
  });

  // On first paint: enforce fullscreen best-effort and, if the browser
  // supports it but the user hasn't entered yet, block the page behind the
  // "Enter Fullscreen" button (a click is a user gesture, so it always works).
  function startLockdown() {
    requestFullscreen();
    if (supportsFullscreen()) {
      window.setTimeout(function () {
        if (!isFullscreen() && !submitting && !submitted && !redirectScheduled && !document.hidden) {
          if (fullscreenBlockOverlay) fullscreenBlockOverlay.style.display = "flex";
        }
      }, 900);
    }
  }

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
        // Re-anchor the violation tally from the server too — a page refresh
        // or a lost network report can never reset it.
        if (data && typeof data.violation_count === "number") {
          violationCount = Math.max(violationCount, data.violation_count);
          if (violationCount >= MAX_VIOLATIONS && !submitted) {
            forceSubmitExam();
          }
        }
        // If the server flagged the attempt while the student was away, stop
        // the exam immediately.
        if (data && data.flagged && !submitted) {
          forceSubmitExam();
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
    // Never submit more than once (manual + auto, or repeated auto triggers).
    if (submitted || submitting) return;
    if (!auto) {
      const ok = window.confirm("Are you sure you want to submit your test?");
      if (!ok) return;
    }
    clearInterval(timerInterval);
    timerInterval = null;
    submitBtn.disabled = true;
    submitBtn.textContent = "Submitting...";
    submitting = true;

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
      // Parse the response as JSON, but NEVER let a malformed / non-JSON body
      // (e.g. an HTML error page or a CSRF rejection) throw raw tokens back at
      // the student. If the body isn't valid JSON we fall back to an empty
      // object and rely on the HTTP status code for a friendly message.
      let data = {};
      try {
        data = await res.json();
      } catch (parseErr) {
        data = {};
      }

      const serverMessage = data && data.error ? data.error : null;
      if (!res.ok || serverMessage) {
        throw new Error(serverMessage || "Submission failed (HTTP " + res.status + ")");
      }
      submitted = true;
      hideViolationOverlays();
      exitFullscreenQuietly();
      showResult(data);
      // A forced submission (3rd security violation) lands the student on the
      // server-side results page.
      if (forcedSubmitRequested) scheduleResultRedirect();
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Finish";
      // Only surface a failure alert if the exam has NOT already been saved.
      // If a transient network error happens during auto-submit, the next
      // server sync re-triggers the auto-submit automatically. For a forced
      // (violation) submission we never block behind a dialog — we redirect
      // and let the flagged exam page retry the submit.
      if (!submitted) {
        if (forcedSubmitRequested) scheduleResultRedirect();
        else window.alert("Submission failed: " + err.message);
      }
    } finally {
      submitting = false;
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
  startLockdown(); // enforce fullscreen + block re-entry to non-fullscreen mode

  // Anti-cheating: if this attempt was already flagged server-side (3rd
  // violation landed while the student was away, or after a page refresh),
  // force-submit immediately and redirect to the results page.
  if (cfg.flagged || (violationCount >= MAX_VIOLATIONS)) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    forceSubmitExam();
  } else if (computeRemaining() <= 0) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    submitExam(true); // deadline already passed -> submit immediately
  } else {
    startTimer();
  }
})();

