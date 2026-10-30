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
  // Handle for the 30s server-time re-sync poll (started together with the
  // countdown) so it can be cleared alongside every other interval the moment
  // the exam is submitted.
  let serverSyncInterval = null;
  // Server-anchored timeline (absolute UTC Unix timestamps). The countdown is
  // ALWAYS derived from (deadline - serverNow), so the user's system clock and
  // page refreshes can never extend the exam.
  let deadlineUnix = cfg.deadlineUnix;
  let serverNowUnix = cfg.serverNowUnix;
  const answers = {}; // { question_index: selected_option_index }
  // Submission guards: prevent a duplicate auto-submit (timer tick or server
  // sync re-triggering submitExam) from sending a second POST once time runs
  // out. The backend rejects the second as "already submitted", which would
  // otherwise show a spurious "Submission failed" alert even though the
  // exam was saved.
  let submitting = false;
  let submitted = false;

  // ---- Anti-cheating / browser-lockdown state -------------------------------
  // Host-controlled proctoring policy (from the Exam's settings, injected as
  // `enableProctoring` / `maxViolations`). When proctoring is DISABLED the
  // student takes the exam normally: no fullscreen lockdown, no
  // tab/shortcut/DevTools monitoring, and no strike counting — the server
  // also treats any violation reports as a no-op, so this flag must mirror
  // that.
  const PROCTORING_ENABLED = cfg.enableProctoring !== false;
  // The violation threshold and current tally are SERVER-authoritative: the
  // page receives the persisted count (cfg.violationCount) so a page refresh
  // can never reset a student's violation tally. The server re-anchors it via
  // the /time and /violation endpoints. The threshold is the HOST-configured
  // per-exam max_violations (e.g. 3 or 5), not a hardcoded constant.
  const MAX_VIOLATIONS = cfg.maxViolations || cfg.violationThreshold || 3;
  let violationCount = cfg.violationCount || 0;
  // True once (a) the server confirms the threshold, (b) the local tally hits
  // it, or (c) the server reports the attempt flagged. The exam is then
  // force-submitted and the student is redirected to the results page.
  let forcedSubmitRequested = false;
  let redirectScheduled = false;
  // Two-step inline confirmation for the manual Finish button — replaces the
  // native confirm() dialog (which steals window focus and fires a synthetic
  // blur that would be counted as a focus-loss strike).
  let submitConfirmArmed = false;
  // Debounce / rate-limit for strikes: a single physical action — switching
  // tabs, losing focus, exiting fullscreen — fires blur + visibilitychange +
  // fullscreenchange almost simultaneously. The FIRST event claims a
  // 5-second cooldown window; every other event arriving inside that window
  // is ignored, so one action can never consume multiple strikes. Uses
  // performance.now() (a monotonic clock) so changing the system clock can
  // never bypass the cooldown.
  let lastViolationReportAt = 0;
  const VIOLATION_COOLDOWN_MS = 5000;
  let focusLost = false;
  // True while the strict warning modal is on screen. While set, every
  // background proctoring loop (DevTools detector, countdown
  // and server-sync timers) is paused and every further violation report is
  // dropped, so strikes can never stack behind an un-acknowledged warning.
  // Only the student's click on "Continue Exam" clears it and resumes.
  let warningModalOpen = false;
  // The most recently reported violation, remembered until the modal renders
  // so it can show the exact rule that was tripped ("Reason: ...").
  let pendingViolationType = "";
  let pendingViolationDetail = "";

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
  const violationStrikeBadge = document.getElementById("violationStrikeBadge");
  const violationModalReason = document.getElementById("violationModalReason");
  const violationAckBtn = document.getElementById("violationAckBtn");
  const fullscreenBlockOverlay = document.getElementById("fullscreenBlockOverlay");
  const fullscreenRetryBtn = document.getElementById("fullscreenRetryBtn");
  const forceSubmitOverlay = document.getElementById("forceSubmitOverlay");
  const forceSubmitText = document.getElementById("forceSubmitText");
  const startGateOverlay = document.getElementById("startGateOverlay");
  const startExamBtn = document.getElementById("startExamBtn");

  function hideViolationOverlays() {
    // Any path that hides the overlays (submission, forced submit) also ends
    // the interaction lock so no stale flag lingers.
    warningModalOpen = false;
    if (violationOverlay) violationOverlay.style.display = "none";
    if (fullscreenBlockOverlay) fullscreenBlockOverlay.style.display = "none";
    if (forceSubmitOverlay) forceSubmitOverlay.style.display = "none";
  }

  // Tears down EVERYTHING proctoring-related the moment a submission is
  // triggered, so no monitor, countdown or overlay keeps running afterwards.
  // Idempotent — safe to call from both the manual submit handler and the
  // forced (violation) auto-submit path, in any order.
  function teardownExamMedia() {
    // 1) Clear the countdown and every proctoring poll interval.
    if (timerInterval) {
      window.clearInterval(timerInterval);
      timerInterval = null;
    }
    if (serverSyncInterval) {
      window.clearInterval(serverSyncInterval);
      serverSyncInterval = null;
    }
    if (devtoolsInterval) {
      window.clearInterval(devtoolsInterval);
      devtoolsInterval = null;
    }
    // 2) Hide every lingering proctoring overlay so only the exam itself
    //    (and later the "Thank You" result screen) remains visible.
    hideViolationOverlays();
    hideSubmitNotice();
    if (startGateOverlay) startGateOverlay.style.display = "none";
  }

  // Pre-exam start-gate state. The exam waits for the student to press
  // "Start Exam" before the clock starts; the platform relies purely on
  // browser-level security.
  let examStarted = false;

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
  // Proctoring-only: unproctored exams never force fullscreen.
  if (PROCTORING_ENABLED) {
    document.addEventListener("click", function () {
      // Fullscreen enforcement only applies during the ACTIVE exam, never
      // while the student is still on the pre-exam setup screen.
      if (!examStarted) return;
      if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
    }, true);
    document.addEventListener("keydown", function () {
      if (!examStarted) return;
      if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
    }, true);
  }

  function onFullscreenChange() {
    // Entering fullscreen on the pre-exam start gate begins the exam right
    // away (e.g. the student used the browser's fullscreen control).
    // Exiting fullscreen on the pre-exam setup screen is never a violation.
    if (!examStarted) {
      if (isFullscreen() && !submitting && !submitted && !redirectScheduled &&
          startGateOverlay && startGateOverlay.style.display === "flex") {
        beginExam();
      }
      return;
    }
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
  if (PROCTORING_ENABLED) {
    document.addEventListener("fullscreenchange", onFullscreenChange);
    document.addEventListener("webkitfullscreenchange", onFullscreenChange);
    document.addEventListener("mozfullscreenchange", onFullscreenChange);
    document.addEventListener("MSFullscreenChange", onFullscreenChange);
  }

  if (fullscreenRetryBtn) {
    fullscreenRetryBtn.addEventListener("click", function () {
      requestFullscreen(); // runs inside a user gesture -> always allowed
    });
  }

  function initPreExamGate() {
    // Lock every exam control until "Start Exam" is pressed. No media-device
    // access is requested — the exam relies purely on browser-level security.
    if (submitBtn) submitBtn.disabled = true;
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;
    if (startGateOverlay) startGateOverlay.style.display = "flex";
  }

  function hidePreExamGate() {
    if (startGateOverlay) startGateOverlay.style.display = "none";
  }

  if (startExamBtn) {
    startExamBtn.addEventListener("click", function () {
      requestFullscreen(); // runs inside a user gesture -> always allowed
      if (!examStarted) beginExam(); // clock starts now
    });
  }

  // ---------------------------- Exam start -------------------------------
  // Called from the "Start Exam" button (proctored) or directly from the
  // init path (unproctored). Establishes the server-anchored deadline and
  // launches the exam without requesting any media-device permission.
  function beginExam() {
    // Unproctored exams reach this from the init path; proctored exams reach
    // it from the "Start Exam" button (or entering fullscreen).
    if (examStarted) return;
    examStarted = true;
    requestStartClock();
  }

  function requestStartClock() {
    // If the server hasn't started this session's clock yet, POST /start so
    // the deadline is anchored at the moment the student actually begins, NOT
    // at the moment the page loaded.
    if (!cfg.deadlineUnix) {
      fetch("/exam/" + cfg.sessionId + "/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": cfg.csrfToken
        },
        body: JSON.stringify({})
      })
        .then(function (res) {
          return res.json().catch(function () { return {}; });
        })
        .then(function (data) {
          if (data && data.deadline_unix != null && data.server_now_unix != null) {
            deadlineUnix = data.deadline_unix;
            serverNowUnix = data.server_now_unix;
            launchExam();
          } else {
            startClockFailed();
          }
        })
        .catch(function () {
          startClockFailed();
        });
    } else {
      launchExam();
    }
  }

  function startClockFailed() {
    examStarted = false;
    if (PROCTORING_ENABLED) {
      // Re-open the pre-exam gate so the student can press Start Exam
      // again once the connection recovers.
      if (startGateOverlay) startGateOverlay.style.display = "flex";
      if (startExamBtn) startExamBtn.disabled = false;
    } else {
      // Unproctored exams have no gate UI to retry from, so silently retry
      // the start once the network recovers — the student keeps a normal
      // exam page.
      window.setTimeout(beginExam, 2000);
    }
  }

  function launchExam() {
    hidePreExamGate();
    if (submitBtn) submitBtn.disabled = false;
    if (prevBtn) prevBtn.disabled = false;
    if (nextBtn) nextBtn.disabled = false;
    renderQuestion(0);

    // Proctoring suite — skipped entirely for unproctored exams so the
    // student takes the exam normally (no fullscreen enforcement, no DevTools
    // probing, no tab/blur/right-click lockdown).
    if (PROCTORING_ENABLED) {
      startLockdown(); // enforce fullscreen + block re-entry to non-fullscreen mode
      startDevToolsDetection(); // monitor window-size / debugger probes for devtools
    }

    // Anti-cheating: if this attempt was already flagged server-side (final
    // violation landed while the student was away, or after a page refresh),
    // force-submit immediately and redirect to the results page. This only
    // applies to proctored exams — an unproctored attempt can never be flagged.
    if (cfg.flagged || (PROCTORING_ENABLED && violationCount >= MAX_VIOLATIONS)) {
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
  }

  // -------------------------- Violation reporting ------------------------
  // Strikes are reported as pure metadata (type + detail + client timestamp).
  // The platform relies purely on browser-level security; the server records
  // every incident with no image attachment.

  function reportViolation(type, detail) {
    // Unproctored exams have no strike counting at all (the server also
    // ignores any reports), so nothing to report here.
    if (!PROCTORING_ENABLED) return;
    // Proctoring only governs the ACTIVE exam (once "Start Exam" was clicked).
    // While the student still sits on the pre-exam setup screen, a window
    // blur, tab switch or fullscreen change is an ordinary browser action and
    // MUST NOT accrue a strike. Reporting violations there is what fed the
    // infinite alert -> blur -> strike -> resubmit loop.
    if (!examStarted) return;
    // Once a submission is underway, stop counting incidents.
    if (submitting || submitted || redirectScheduled) return;

    // While the strict warning modal is on screen, ALL further reports are
    // dropped (the background monitors are paused too), so strikes can never
    // stack behind an un-acknowledged warning.
    if (warningModalOpen) return;

    // Rate-limit: blur + visibilitychange + fullscreen-exit from the SAME
    // physical switch fire within milliseconds and MUST count as ONE strike.
    // The first event claims the cooldown window; anything inside it is
    // dropped before the report/fetch runs, so the server never even sees
    // (or counts) a duplicate.
    const nowMs = performance.now();
    if (nowMs - lastViolationReportAt < VIOLATION_COOLDOWN_MS) return;
    lastViolationReportAt = nowMs;
    // Record exactly what tripped today's strike so the modal can name the
    // precise reason as soon as the server confirms the new count.
    pendingViolationType = type;
    pendingViolationDetail = detail || "";

    // Report immediately — metadata only (type + detail + client timestamp).
    // No image frame is captured.
    postViolation(type, detail);
  }

  function postViolation(type, detail) {
    const body = { type: type, detail: detail || "" };

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
    if ((data && data.auto_submit) ||
        (PROCTORING_ENABLED && violationCount >= MAX_VIOLATIONS)) {
      forceSubmitExam();
      return;
    }
    showWarningModal(violationCount, "");
  }

  // -------------------------- Warning modal ------------------------------
  // Human-readable labels mapped from the server-side violation type codes so
  // the modal always names the exact rule that was tripped ("Reason: Tab
  // switched away", "Reason: Developer tools were detected", ...).
  const VIOLATION_REASONS = {
    tab_switch: "Tab switched away from the exam page",
    focus_loss: "Exam window lost focus to another window or app",
    fullscreen_exit: "Exited full-screen mode (Esc / F11 / gesture)",
    devtools_detected: "Developer tools were detected",
    devtools_shortcut: "Developer-tools / save / print / refresh shortcut was blocked",
    contextmenu: "Right-click / inspect-element attempt was blocked",
    copy_paste: "Clipboard operation (copy / cut / paste) was blocked"
  };

  function violationReasonLabel(type, detail) {
    const mapped = VIOLATION_REASONS[type];
    return mapped || detail || "A security violation was detected";
  }

  function showWarningModal(count, message) {
    if (submitting || submitted) return;
    if (!violationOverlay) return;
    const remaining = Math.max(0, MAX_VIOLATIONS - count);
    // 1) Clear strike display: "Strike 1 of 3", "Strike 2 of 5", ...
    if (violationStrikeBadge) {
      violationStrikeBadge.textContent =
        "Strike " + count + " of " + MAX_VIOLATIONS;
    }
    if (violationModalCount) {
      violationModalCount.textContent =
        remaining > 0
          ? "Your exam will be submitted automatically after " + remaining + " more."
          : "Maximum strikes reached — your exam is being submitted.";
    }
    // 2) Specific violation type: "Reason: Tab switched away".
    if (violationModalReason) {
      violationModalReason.textContent =
        "Reason: " + violationReasonLabel(pendingViolationType, pendingViolationDetail);
    }
    if (violationModalText) {
      violationModalText.textContent =
        message ||
        "Warning: Leaving the exam tab is prohibited and will be reported. " +
        "Do not switch tabs, minimize this window, open other programs, " +
        "right-click, or open developer tools while the exam is active.";
    }
    pendingViolationType = "";
    pendingViolationDetail = "";

    // 3) Hard screen lock: the fixed high-z-index backdrop (CSS) covers the
    // whole viewport and the capture-phase keydown lock below swallows every
    // keystroke, so the student can neither click nor type past the warning.
    warningModalOpen = true;
    pauseProctoringMonitors();
    if (document.activeElement && typeof document.activeElement.blur === "function") {
      document.activeElement.blur(); // drop focus from any exam control
    }
    if (violationAckBtn && typeof violationAckBtn.focus === "function") {
      // Enter/Space on "Continue Exam" is the only key input allowed while
      // the modal is open (see the capture-phase keydown lock below).
      violationAckBtn.focus();
    }
    violationOverlay.style.display = "flex";
    requestFullscreen();
  }

  // 4) Pause monitoring the MOMENT the modal opens: every background interval
  // (DevTools detector, the 1s countdown tick and the 30s server re-sync
  // poll) is cleared so no new strike can accumulate while the student reads
  // the warning. Each monitor is simply restarted when the student resumes.
  function pauseProctoringMonitors() {
    if (devtoolsInterval) {
      window.clearInterval(devtoolsInterval);
      devtoolsInterval = null;
    }
    if (timerInterval) {
      window.clearInterval(timerInterval);
      timerInterval = null;
    }
    if (serverSyncInterval) {
      window.clearInterval(serverSyncInterval);
      serverSyncInterval = null;
    }
  }

  // 5) Resume on "OK": restarts the countdown/server-sync and the DevTools
  // detector, and re-anchors the server clock. The strike cooldown is reset
  // so the very next action after the acknowledgement is counted fairly.
  function resumeProctoringMonitors() {
    warningModalOpen = false;
    if (submitting || submitted || redirectScheduled) return;
    lastViolationReportAt = 0;
    focusLost = false;
    startTimer(); // restarts the 1s countdown tick + the 30s server-time sync
    if (PROCTORING_ENABLED) {
      startDevToolsDetection();
    }
    syncFromServer(); // re-anchor deadline / strike tally / flagged state now
  }

  if (violationAckBtn) {
    violationAckBtn.addEventListener("click", function () {
      // The ONLY way out of the warning: resume monitoring, unblock the
      // screen (hide the modal), then re-assert fullscreen from inside this
      // click (a real user gesture, so the browser always allows it).
      resumeProctoringMonitors();
      if (violationOverlay) violationOverlay.style.display = "none";
      requestFullscreen();
    });
  }

  // -------------------------- Forced final submission --------------------
  function scheduleResultRedirect() {
    if (redirectScheduled) return;
    redirectScheduled = true;
    // Tiny delay lets the client render the score before the server-side
    // result page replaces the exam page.
    window.setTimeout(function () {
      window.location.href = "/exam/" + cfg.sessionId;
    }, 200);
  }

  function forceSubmitExam() {
    if (submitted || redirectScheduled) return;
    // A forced submission (strike threshold / server flag) only ever applies
    // to a session whose clock is already running, so record that locally to
    // satisfy submitExam()'s examStarted guard on the page-load path too.
    examStarted = true;
    forcedSubmitRequested = true;
    // Clear every proctoring interval IMMEDIATELY, before any fetch, so
    // nothing keeps polling DevTools detection or the server once a forced
    // submit is triggered (the submission either succeeds, or the flagged
    // page redirect and force-submits on reload).
    teardownExamMedia();
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
  // Tab-switch / focus-loss / clipboard / right-click / keyboard-shortcut
  // lockdown is part of the PROCTORING suite — never installed for unproctored
  // exams, where the student browses and switches tabs normally.
  if (PROCTORING_ENABLED) {
  function onVisibilityChange() {
    if (!examStarted) return;
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
    // No strikes can accrue while the student is still on the pre-exam
    // start screen (this blur-on-dismiss was the trigger for the loop).
    if (!examStarted) return;
    if (submitting || submitted) return;
    // `blur` fires both when a tab is switched AND when the user clicks into
    // another application while the tab stays visible. Report it either way.
    if (!focusLost) {
      focusLost = true;
      reportViolation("focus_loss", "Window lost focus (switched app / tab)");
    }
  });

  window.addEventListener("focus", function () {
    if (!examStarted) return;
    focusLost = false;
    if (!submitting && !submitted) requestFullscreen();
  });

  // -------------------------- Clipboard / input lockdown -----------------
  // Right-click -> no context menu, no "inspect element".
  document.addEventListener("contextmenu", function (e) {
    e.preventDefault();
    // Block the menu even on the pre-exam screen, but never report a strike
    // until the exam has actually started.
    if (!examStarted) return false;
    if (!submitting && !submitted) {
      reportViolation("contextmenu", "Right-click / inspect attempt blocked");
    }
    return false;
  });

  // Native copy / cut / paste events.
  ["copy", "cut", "paste"].forEach(function (evtName) {
    document.addEventListener(evtName, function (e) {
      e.preventDefault();
      // Until the exam starts, clipboard use is ordinary browsing — block the
      // operation but never turn it into a security strike.
      if (!examStarted) return;
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
      (ctrl && e.shiftKey && (key === "i" || key === "j" || key === "c" || key === "e" || key === "k")) ||
      (ctrl && (key === "u" || key === "s" || key === "p")) ||
      (e.key === "F5" || (ctrl && key === "r"));
    const isClipboardKey = ctrl && (key === "c" || key === "x" || key === "v");
    const isSelectAllKey = ctrl && key === "a";
    const isFullscreenToggle = e.key === "F11";

    if (isDevtoolsKey || isClipboardKey || isSelectAllKey || isFullscreenToggle) {
      e.preventDefault();
      e.stopPropagation();
      // Block the shortcut keys even on the pre-exam screen, but never report
      // a violation until the exam actually started.
      if (!examStarted) return false;
      if (!submitting && !submitted && !e.repeat) {
        const what = isDevtoolsKey
          ? "Developer tools / save / print / refresh shortcut"
          : isFullscreenToggle
            ? "Fullscreen toggle shortcut"
            : isSelectAllKey
              ? "Text selection (Select All) shortcut"
              : "Clipboard shortcut";
        reportViolation("devtools_shortcut", what + " blocked: " + e.key);
      }
      return false;
    }
    return true;
  });
  // 3) Hard input lock while the warning modal is on screen: a capture-phase
  // keydown swallows EVERY keystroke before it can reach the exam (radio
  // inputs, textareas, the Prev / Next / Finish buttons, shortcuts), so the
  // modal blocks typing and answering exactly as the backdrop blocks clicks.
  // The only exception is activating "Continue Exam" itself with Enter/Space
  // while it is focused (it is auto-focused when the modal opens, and Tab is
  // blocked so focus can never drift to the exam controls).
  document.addEventListener("keydown", function (e) {
    if (!warningModalOpen) return;
    const isAckActivation =
      (e.key === "Enter" || e.key === " ") &&
      document.activeElement === violationAckBtn;
    if (isAckActivation) return; // allow keyboard activation of "Continue"
    e.preventDefault();
    e.stopPropagation();
  }, true);
  } // end of PROCTORING_ENABLED lockdown listeners

  // -------------------------- DevTools detection --------------------------
  // Two independent heuristics catch the browser's developer tools while the
  // exam is active. Either one firing reports a `devtools_detected` strike
  // through the normal violation pipeline (server-authoritative count,
  // on-screen warning modal, and a hard auto-submit at the threshold):
  //   1. Window-size discrepancy: docked devtools shrink innerWidth/Height
  //      while outerWidth/Height stay the same, so the outer-inner delta grows
  //      by ~100px+. A normal window resize keeps that delta roughly constant.
  //   2. `debugger` probe: with devtools attached and breakpoints active, a
  //      `debugger;` statement pauses execution, making the measured round-trip
  //      time far exceed the sub-millisecond cost when devtools is closed.
  const DEVTOOLS_POLL_MS = 2000;
  const DEVTOOLS_DELTA_THRESHOLD = 100;
  const DEVTOOLS_DEBUGGER_THRESHOLD_MS = 120;
  const DEVTOOLS_REARM_MS = 5000;
  let devtoolsInterval = null;
  let devtoolsOpen = false;
  let baselineOuterInnerDelta = null;

  function measureOuterInnerDelta() {
    let w = 0;
    let h = 0;
    if (typeof window.outerWidth === "number" && typeof window.innerWidth === "number") {
      w = Math.max(0, window.outerWidth - window.innerWidth);
    }
    if (typeof window.outerHeight === "number" && typeof window.innerHeight === "number") {
      h = Math.max(0, window.outerHeight - window.innerHeight);
    }
    return { w: w, h: h };
  }

  function debuggerProbe() {
    const start = performance.now();
    try {
      (function () { debugger; })();
    } catch (err) {
      // Some browsers treat the statement as a no-op — never crash on it.
    }
    return performance.now() - start;
  }

  function checkDevTools() {
    if (!examStarted) return;
    if (submitting || submitted || redirectScheduled) return;
    if (devtoolsOpen) return; // already struck for the current open session

    // 1) Window-size discrepancy (catches docked devtools).
    if (baselineOuterInnerDelta !== null) {
      const delta = measureOuterInnerDelta();
      const grew =
        delta.w - baselineOuterInnerDelta.w >= DEVTOOLS_DELTA_THRESHOLD ||
        delta.h - baselineOuterInnerDelta.h >= DEVTOOLS_DELTA_THRESHOLD;
      if (grew) {
        triggerDevToolsStrike();
        return;
      }
    }

    // 2) debugger probe (catches undocked / remote devtools); near-instant
    //    while devtools is closed.
    if (debuggerProbe() > DEVTOOLS_DEBUGGER_THRESHOLD_MS) {
      triggerDevToolsStrike();
    }
  }

  function triggerDevToolsStrike() {
    if (submitting || submitted || redirectScheduled) return;
    if (devtoolsOpen) return;
    devtoolsOpen = true;
    reportViolation(
      "devtools_detected",
      "Developer tools detected (window size discrepancy / debugger probe)"
    );
    // Re-arm after a few seconds so KEEPING devtools open accrues further
    // strikes (and eventually the hard auto-submit), while an incidental blip
    // is counted only once.
    window.setTimeout(function () {
      if (!submitting && !submitted && !redirectScheduled) devtoolsOpen = false;
    }, DEVTOOLS_REARM_MS);
  }

  // Begins the detection loop exactly when the exam starts (once the start
  // gate is passed) and is a no-op thereafter, so no strikes are possible pre-exam.
  function startDevToolsDetection() {
    if (devtoolsInterval) return;
    baselineOuterInnerDelta = measureOuterInnerDelta();
    devtoolsInterval = window.setInterval(checkDevTools, DEVTOOLS_POLL_MS);
  }

  // On first paint: enforce fullscreen best-effort and, if the browser
  // supports it but the user hasn't entered yet, block the page behind the
  // "Enter Fullscreen" button (a click is a user gesture, so it always works).
  function startLockdown() {
    requestFullscreen();
    if (supportsFullscreen()) {
      window.setTimeout(function () {
        if (!isFullscreen() && examStarted && !submitting && !submitted && !redirectScheduled && !document.hidden) {
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
    if (!examStarted) return;
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
        // or a lost network report can never reset it. Only meaningful while
        // proctoring is enabled (unproctored exams never accrue strikes).
        if (PROCTORING_ENABLED && data && typeof data.violation_count === "number") {
          violationCount = Math.max(violationCount, data.violation_count);
          if (violationCount >= MAX_VIOLATIONS && !submitted) {
            forceSubmitExam();
          }
        }
        // If the server flagged the attempt while the student was away, stop
        // the exam immediately. Also proctoring-only (a flagged attempt is the
        // terminal outcome of the strike threshold).
        if (PROCTORING_ENABLED && data && data.flagged && !submitted) {
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
    if (!examStarted) return;
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
    // attempt to pause the browser tab from gaining extra time). The handle is
    // kept so it can be cleared together with the countdown on submission.
    serverSyncInterval = setInterval(syncFromServer, 30000);
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
  // Rebuilds the countdown ticker + server-time sync and, for proctored exams,
  // the DevTools detector AFTER a FAILED submission attempt, so the student
  // keeps a correctly-timed, fully-monitored exam while they retry.
  function resumeExamAfterFailedSubmit() {
    if (submitted || redirectScheduled) return;

    if (!timerInterval) {
      timerInterval = setInterval(() => {
        serverNowUnix += 1;
        renderTimer();
      }, 1000);
    }
    if (!serverSyncInterval) {
      serverSyncInterval = setInterval(syncFromServer, 30000);
    }
    if (PROCTORING_ENABLED) {
      startDevToolsDetection();
    }
  }

  // ------------------------ Inline submission notice -----------------------
  // Replaces the blocking native alert() on submission failure. A fixed
  // position, focus-safe toast is used: it never takes window focus, and
  // dismissing it can never fire a synthetic blur event. (The old alert()
  // lost focus on close, tripping the window-blur listener -> a strike ->
  // another auto-submit -> another alert: the infinite loop we are fixing.)
  let submitNoticeEl = null;
  let submitNoticeHideTimer = null;

  function hideSubmitNotice() {
    if (submitNoticeHideTimer) {
      window.clearTimeout(submitNoticeHideTimer);
      submitNoticeHideTimer = null;
    }
    if (submitNoticeEl) submitNoticeEl.style.display = "none";
  }

  function showSubmitNotice(message) {
    if (!message) return;
    if (!submitNoticeEl) {
      submitNoticeEl = document.createElement("div");
      submitNoticeEl.id = "submitNotice";
      submitNoticeEl.setAttribute("role", "alert");
      submitNoticeEl.setAttribute("aria-live", "polite");
      submitNoticeEl.style.cssText =
        "position:fixed;top:12px;left:50%;transform:translateX(-50%);" +
        "z-index:99999;max-width:600px;box-sizing:border-box;" +
        "background:#fff3cd;color:#664d03;border:1px solid #c1a962;" +
        "border-left:6px solid #e6a700;border-radius:8px;" +
        "padding:12px 18px;box-shadow:0 4px 12px rgba(0,0,0,.18);" +
        "font-size:15px;line-height:1.45;text-align:left;";
      submitNoticeEl.addEventListener("click", hideSubmitNotice);
      document.body.appendChild(submitNoticeEl);
    }
    submitNoticeEl.textContent = message;
    submitNoticeEl.style.display = "block";
    // Auto-dismiss after 8 seconds so a stale notice never lingers.
    hideSubmitNotice();
    submitNoticeHideTimer = window.setTimeout(function () {
      if (submitNoticeEl) submitNoticeEl.style.display = "none";
      submitNoticeHideTimer = null;
    }, 8000);
  }

  async function submitExam(auto) {
    // Never submit more than once (manual + auto, or repeated auto triggers).
    if (submitted || submitting) return;
    // The auto-submit routine (countdown tick, server re-sync, forced submit)
    // MUST be inert while the student is still on the pre-exam
    // setup screen. A session whose clock was never started has no deadline to
    // expire, so submitting there bounced off the backend with "The exam
    // has not started yet." and fed the alert -> blur -> strike loop. Every
    // legitimate entry point sets examStarted = true first (beginExam from the
    // Start Exam button, or the page-load paths the server already considers a
    // started session).
    if (!examStarted) return;
    if (!auto) {
      // Two-step inline confirmation — NEVER a native confirm() dialog. Native
      // dialogs steal window focus; dismissing them fires a synthetic blur
      // event that would be counted as a focus-loss strike (the same failure
      // mode as the old alert() cascade). First click arms the button; a second
      // click within the 5-second window submits. Auto-submits bypass this.
      if (!submitConfirmArmed) {
        submitConfirmArmed = true;
        submitBtn.textContent = "Click again to confirm submission";
        submitBtn.classList.add("btn-warning", "text-dark");
        window.setTimeout(function () {
          submitConfirmArmed = false;
          if (!submitting && !submitted) {
            submitBtn.textContent = "Finish";
            submitBtn.classList.remove("btn-warning", "text-dark");
          }
        }, 5000);
        return;
      }
      submitConfirmArmed = false;
      submitBtn.classList.remove("btn-warning", "text-dark");
    }
    // The submission is now TRIGGERED: immediately clear the countdown and
    // all proctoring poll intervals and hide the proctoring overlays.
    // Idempotent, so a forced auto-submit that reaches this same handler
    // tears nothing down twice.
    teardownExamMedia();
    submitBtn.disabled = true;
    submitBtn.textContent = "Submitting...";
    submitting = true;
    // `data` is hoisted to function scope so the catch block below can read
    // the server's `not_started` flag and pick a non-blocking remediation.
    let data = {};

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
      hideSubmitNotice();
      exitFullscreenQuietly();
      showResult(data);
      // A forced submission (3rd security violation) lands the student on the
      // server-side results page.
      if (forcedSubmitRequested) scheduleResultRedirect();
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = "Finish";
      // Only show a failure notice if the exam has NOT already been saved.
      // If a transient network error happens during auto-submit, the next
      // server sync re-triggers the auto-submit automatically. For a forced
      // (violation) submission we never block behind a dialog — we redirect
      // and let the flagged exam page retry the submit.
      if (!submitted) {
        if (forcedSubmitRequested) {
          scheduleResultRedirect();
        } else if (data && data.not_started) {
          // The backend says this session's clock was never started — an
          // auto-submit escaped the examStarted guard above. Drop quietly back
          // to the pre-exam state: reset the flag, show a NON-BLOCKING inline
          // notice (never a native alert, which would steal focus and re-trigger
          // the window-blur strike loop) and re-open the pre-exam start gate
          // instead of resuming monitors on a session that is not running.
          examStarted = false;
          showSubmitNotice(
            "Your exam has not started yet. " +
            (PROCTORING_ENABLED
              ? "Please click \u201cStart Exam\u201d to begin."
              : "The exam session has not been started yet. Please refresh the page.")
          );
          if (PROCTORING_ENABLED) {
            initPreExamGate();
          } else {
            window.setTimeout(beginExam, 1500);
          }
        } else {
          // Inline, non-blocking notice replaces the old window.alert() so a
          // dismissal can never fire a synthetic window blur (which used to
          // accrue a strike and re-trigger the auto-submit -> alert loop).
          showSubmitNotice("Submission failed: " + err.message);
          // Restore the countdown + proctoring monitors so an interrupted
          // submission never leaves the student with a frozen timer or an
          // unmonitored exam while they retry.
          resumeExamAfterFailedSubmit();
        }
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
  // PROCTORED exams NEVER auto-start on load: a pre-exam start gate runs
  // first, and the timer/questions stay locked until the student clicks
  // "Start Exam". The only exceptions are server-side terminal states
  // (already flagged, or deadline already passed), which must be honored
  // immediately regardless of availability.
  //
  // UNPROCTORED exams skip the gate entirely: no lockdown is installed, and
  // the clock starts as soon as the page loads so
  // the student takes the exam like a normal test.
  if (cfg.flagged || (PROCTORING_ENABLED && violationCount >= MAX_VIOLATIONS)) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    forceSubmitExam();
  } else if (cfg.deadlineUnix && computeRemaining() <= 0) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    // This session IS started server-side (it has a deadline) but the clock
    // elapsed before the student got here. Mark it started locally so
    // submitExam()'s examStarted guard lets this legitimate submit through.
    examStarted = true;
    submitExam(true); // deadline already passed -> submit immediately
  } else if (PROCTORING_ENABLED) {
    initPreExamGate();
  } else {
    beginExam(); // no gate — start the clock right away
  }
})();

