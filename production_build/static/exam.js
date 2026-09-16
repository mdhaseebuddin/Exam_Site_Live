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
  // otherwise surface a spurious "Submission failed" alert even though the
  // exam was saved.
  let submitting = false;
  let submitted = false;

  // ---- Anti-cheating / browser-lockdown state -------------------------------
  // Host-controlled proctoring policy (from the Exam's settings, injected as
  // `enableProctoring` / `maxViolations`). When proctoring is DISABLED the
  // student takes the exam normally: no camera gate, no tab/shortcut/DevTools
  // lockdown, no face monitoring, and no strike counting — the server also
  // treats any violation reports as a no-op, so this flag must mirror that.
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
  const cameraGateOverlay = document.getElementById("cameraGateOverlay");
  const cameraPreview = document.getElementById("cameraPreview");
  const cameraGateStatus = document.getElementById("cameraGateStatus");
  const cameraGateError = document.getElementById("cameraGateError");
  const cameraRetryBtn = document.getElementById("cameraRetryBtn");
  const cameraStartBtn = document.getElementById("cameraStartBtn");

  function hideViolationOverlays() {
    if (violationOverlay) violationOverlay.style.display = "none";
    if (fullscreenBlockOverlay) fullscreenBlockOverlay.style.display = "none";
    if (forceSubmitOverlay) forceSubmitOverlay.style.display = "none";
  }

  // Tears down EVERYTHING media/proctoring related the moment a submission is
  // triggered, so the webcam hardware light turns off immediately and nothing
  // keeps polling the camera, DevTools detection or the server afterwards.
  // Idempotent — safe to call from both the manual submit handler and the
  // forced (violation) auto-submit path, in any order.
  function teardownExamMedia() {
    // 1) Stop every active media track (turns the camera light off now, not
    //    after the fetch) and detach the stream from every <video> element.
    stopCaptureStream();
    if (cameraPreview) {
      cameraPreview.style.display = "none";
      cameraPreview.srcObject = null;
    }
    if (faceMonitorVideo) {
      faceMonitorVideo.srcObject = null;
    }
    // 2) Clear the countdown and every proctoring poll interval.
    if (timerInterval) {
      window.clearInterval(timerInterval);
      timerInterval = null;
    }
    if (serverSyncInterval) {
      window.clearInterval(serverSyncInterval);
      serverSyncInterval = null;
    }
    if (faceMonitorInterval) {
      window.clearInterval(faceMonitorInterval);
      faceMonitorInterval = null;
    }
    if (devtoolsInterval) {
      window.clearInterval(devtoolsInterval);
      devtoolsInterval = null;
    }
    // 3) Hide the camera preview and every lingering proctoring overlay so
    //    only the "Thank You" result screen remains visible.
    hideViolationOverlays();
    if (cameraGateOverlay) cameraGateOverlay.style.display = "none";
  }

  // ---------------------------- Camera gate -------------------------------
  // The exam stays BLOCKED until a live webcam stream is verified. If the
  // student denies permission, no camera is found, the camera is in use, or
  // the stream never delivers real frames, a blocking warning is displayed
  // and the Start Exam button stays disabled. The stream acquired here is
  // kept alive for the whole exam and reused for violation proof snapshots.
  const CAMERA_CONNECT_TIMEOUT_MS = 10000;
  let cameraVerified = false;
  let examStarted = false;

  function setCameraGateState(statusText, errorText) {
    if (cameraGateStatus) cameraGateStatus.textContent = statusText;
    if (cameraGateError) {
      cameraGateError.textContent = errorText || "";
      cameraGateError.style.display = errorText ? "block" : "none";
    }
  }

  function cameraErrorMessage(err) {
    const name = (err && err.name) || "";
    switch (name) {
      case "NotAllowedError":
      case "PermissionDeniedError":
        return "Camera permission was denied. Click the camera icon in your browser\u2019s address bar, choose \u201cAllow\u201d, then press Try Again.";
      case "NotFoundError":
      case "DevicesNotFoundError":
        return "No camera was detected on this device. Connect a working webcam and press Try Again.";
      case "NotReadableError":
      case "TrackStartError":
        return "Your camera could not be started \u2014 it may already be in use by another application. Close other apps using the camera and press Try Again.";
      case "OverconstrainedError":
        return "Your camera cannot provide a usable video format. Press Try Again or try a different camera.";
      case "AbortError":
        return "The camera check was interrupted. Press Try Again to retry.";
      case "SecurityError":
        return "Camera access was blocked by the browser. This exam must be opened over HTTPS (or localhost) to use webcam proctoring.";
      default:
        return "Unable to connect to your camera. Press Try Again to attempt the check once more.";
    }
  }

  // Performs the camera-verification check. Runs on every load (including
  // refreshes) so a student can never skip it.
  function verifyCamera() {
    setCameraGateState("Requesting camera access\u2026", "");
    if (cameraStartBtn) cameraStartBtn.disabled = true;
    if (cameraRetryBtn) cameraRetryBtn.style.display = "none";
    if (cameraPreview) {
      cameraPreview.style.display = "none";
      cameraPreview.srcObject = null;
    }
    cameraVerified = false;

    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia
    ) {
      setCameraGateState(
        "\u26a0\ufe0f Camera check failed \u2014 the exam is blocked.",
        "This browser does not support camera access on this page. Camera access requires a secure (HTTPS) connection \u2014 enable it and press Try Again."
      );
      if (cameraRetryBtn) cameraRetryBtn.style.display = "block";
      return;
    }

    let verified = false;
    const gateTimer = window.setTimeout(function () {
      if (verified) return;
      window.clearTimeout(gateTimer);
      cameraVerified = false;
      stopCaptureStream();
      if (cameraPreview) {
        cameraPreview.style.display = "none";
        cameraPreview.srcObject = null;
      }
      setCameraGateState(
        "\u26a0\ufe0f Camera check failed \u2014 no live video stream.",
        "The video stream failed to start within the time limit. Make sure the camera is not covered or blocked, then press Try Again."
      );
      if (cameraRetryBtn) cameraRetryBtn.style.display = "block";
    }, CAMERA_CONNECT_TIMEOUT_MS);

    // Runs only when the video element has real frames available — a camera
    // that reports a stream but delivers no pixels still fails the check.
    function onLive() {
      if (verified) return;
      verified = true;
      window.clearTimeout(gateTimer);
      cameraVerified = true;
      if (cameraRetryBtn) cameraRetryBtn.style.display = "none";
      if (cameraStartBtn) cameraStartBtn.disabled = false;
      setCameraGateState(
        "\u2705 Camera connected \u2014 your live video feed is active. Click \u201cStart Exam\u201d to begin.",
        ""
      );
    }

    try {
      navigator.mediaDevices.getUserMedia(
        { video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false }
      )
        .then(function (stream) {
          if (verified) return;
          captureStream = stream;
          // Preview element when present (student sees their own live feed);
          // otherwise a detached element still proves frames are flowing.
          const videoEl = cameraPreview || document.createElement("video");
          videoEl.muted = true;
          videoEl.playsInline = true;
          videoEl.srcObject = stream;
          if (cameraPreview) cameraPreview.style.display = "block";
          // "loadeddata" fires only after actual video frames are available,
          // so a camera that reports a stream but delivers no pixels fails.
          videoEl.addEventListener("loadeddata", onLive, { once: true });
          videoEl.play().then(function () {}).catch(function () {});
        })
        .catch(function (err) {
          if (verified) return;
          window.clearTimeout(gateTimer);
          cameraVerified = false;
          stopCaptureStream();
          if (cameraPreview) {
            cameraPreview.style.display = "none";
            cameraPreview.srcObject = null;
          }
          setCameraGateState(
            "\u26a0\ufe0f Camera check failed \u2014 the exam is blocked.",
            cameraErrorMessage(err)
          );
          if (cameraRetryBtn) cameraRetryBtn.style.display = "block";
        });
    } catch (err) {
      if (verified) return;
      window.clearTimeout(gateTimer);
      cameraVerified = false;
      stopCaptureStream();
      setCameraGateState(
        "\u26a0\ufe0f Camera check failed \u2014 the exam is blocked.",
        cameraErrorMessage(err)
      );
      if (cameraRetryBtn) cameraRetryBtn.style.display = "block";
    }
  }

  // Re-acquires the webcam AFTER a FAILED submission attempt (best-effort and
  // silent): permission was already granted at the camera gate, so this only
  // repopulates `captureStream` and resumes the face monitor — no gate UI is
  // shown and the camera preview stays hidden. Degrades gracefully when the
  // camera is unavailable (the exam can still be retried and submitted).
  function acquireCameraStream() {
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices ||
      !navigator.mediaDevices.getUserMedia
    ) {
      return;
    }
    try {
      navigator.mediaDevices.getUserMedia(
        { video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false }
      )
        .then(function (stream) {
          captureStream = stream;
          startFaceMonitor(); // stream is live again -> resume face checks
        })
        .catch(function () {
          captureStream = null; // camera busy / denied -> monitor stays off
        });
    } catch (err) {
      captureStream = null;
    }
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
  // Proctoring-only: unproctored exams never force fullscreen.
  if (PROCTORING_ENABLED) {
    document.addEventListener("click", function () {
      if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
    }, true);
    document.addEventListener("keydown", function () {
      if (!submitting && !submitted && !redirectScheduled) requestFullscreen();
    }, true);
  }

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

  function initCameraGate() {
    if (cameraGateOverlay) cameraGateOverlay.style.display = "flex";
    // Lock every exam control until a live stream is verified.
    if (submitBtn) submitBtn.disabled = true;
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;
    verifyCamera();
  }

  function hideCameraGate() {
    if (cameraGateOverlay) cameraGateOverlay.style.display = "none";
  }

  if (cameraRetryBtn) {
    cameraRetryBtn.addEventListener("click", function () {
      verifyCamera();
    });
  }

  if (cameraStartBtn) {
    cameraStartBtn.addEventListener("click", function () {
      if (cameraVerified && !examStarted) beginExam();
    });
  }

  // ---------------------------- Exam start -------------------------------
  // Called from the camera gate after a live stream is verified. Establishes
  // the server-anchored deadline (so time at the camera gate never counts
  // against the student) and then launches the exam.
  function beginExam() {
    // Unproctored exams have no camera gate: the init path calls this directly
    // and the clock starts right away. In proctored exams the Start Exam
    // button may only call this after a live camera stream was verified.
    if (examStarted) return;
    if (PROCTORING_ENABLED && !cameraVerified) return;
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
      setCameraGateState(
        "\u26a0\ufe0f Could not start the exam.",
        "The exam server could not be reached, so the clock could not be started. Check your connection and press Start Exam to try again."
      );
      if (cameraGateOverlay) cameraGateOverlay.style.display = "flex";
      if (cameraStartBtn) cameraStartBtn.disabled = false;
    } else {
      // Unproctored exams have no gate UI to retry from, so silently retry the
      // start once the network recovers — the student keeps a normal exam page.
      window.setTimeout(beginExam, 2000);
    }
  }

  function launchExam() {
    hideCameraGate();
    if (submitBtn) submitBtn.disabled = false;
    if (prevBtn) prevBtn.disabled = false;
    if (nextBtn) nextBtn.disabled = false;
    renderQuestion(0);

    // Proctoring suite — skipped entirely for unproctored exams so the
    // student takes the exam normally (no fullscreen enforcement, no DevTools
    // probing, no face-absence monitoring).
    if (PROCTORING_ENABLED) {
      startLockdown(); // enforce fullscreen + block re-entry to non-fullscreen mode
      startDevToolsDetection(); // monitor window-size / debugger probes for devtools
      startFaceMonitor(); // continuous out-of-frame / face-absence monitoring
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

  // -------------------------- Face-presence monitor -----------------------
  // Continuous lightweight webcam analysis with NO external ML library: every
  // second a small frame of the LIVE camera feed (the stream acquired at the
  // camera gate is kept running for the whole exam) is analyzed for:
  //   1. Skin-tone pixels in the central region (YCrCb heuristic that works
  //      across skin tones), and
  //   2. Inter-frame motion (pixel deltas between consecutive frames), so a
  //      present-but-still student is never falsely flagged.
  // When BOTH are absent for a sustained window (~5s), the student is treated
  // as out-of-frame / lens covered. A `face_not_detected` strike is then
  // reported through the SAME pipeline as tab-switching — server-authoritative
  // count, the on-screen security warning modal, and a hard auto-submit once
  // the threshold is reached.
  const FACE_MONITOR_INTERVAL_MS = 1000;
  const FACE_MONITOR_MISSES_TO_FLAG = 5;    // ~5s of sustained absence
  const FACE_SKIN_THRESHOLD = 0.08;         // >= 8% of central pixels skin-toned
  const FACE_MOTION_THRESHOLD = 0.012;      // avg normalized channel delta
  let faceMonitorInterval = null;
  let faceMonitorVideo = null;
  let faceMonitorCanvas = null;
  let faceMonitorCtx = null;
  let faceMonitorStreak = 0;
  let lastFaceFrameData = null;
  // Lets a reporter (e.g. the face monitor) override the generic modal text so
  // students see a specific warning ("Face not detected in camera view").
  let proctorAlertMessage = "";

  // Sends a face-absence strike through the standard violation pipeline.
  function reportFaceAbsence() {
    proctorAlertMessage =
      "Security Warning: Face not detected in camera view. " +
      "Please keep your face inside the camera frame at all times.";
    reportViolation(
      "face_not_detected",
      "Face not detected in camera view (student out of frame / camera covered)"
    );
  }

  // Starts the monitor when the exam actually begins (after the camera gate).
  function startFaceMonitor() {
    if (faceMonitorInterval) return;
    if (!captureStream || !captureStream.active) return; // no live camera -> skip
    faceMonitorVideo = document.createElement("video");
    faceMonitorVideo.muted = true;
    faceMonitorVideo.playsInline = true;
    faceMonitorVideo.srcObject = captureStream;
    faceMonitorVideo.play().catch(function () {});
    faceMonitorCanvas = document.createElement("canvas");
    faceMonitorCanvas.width = 160;
    faceMonitorCanvas.height = 120;
    faceMonitorCtx = faceMonitorCanvas.getContext("2d");
    faceMonitorStreak = 0;
    lastFaceFrameData = null;
    faceMonitorInterval = window.setInterval(checkFacePresence, FACE_MONITOR_INTERVAL_MS);
  }

  // One analysis tick: reads the camera frame, checks for skin + motion, and
  // escalates a sustained absence into a strike.
  function checkFacePresence() {
    if (submitting || submitted || redirectScheduled) return;
    if (!faceMonitorVideo || !faceMonitorCtx) return;
    if (!faceMonitorVideo.videoWidth || faceMonitorVideo.readyState < 2) return;

    let frameData;
    try {
      faceMonitorCtx.drawImage(
        faceMonitorVideo,
        0, 0, faceMonitorCanvas.width, faceMonitorCanvas.height
      );
      frameData = faceMonitorCtx.getImageData(
        0, 0, faceMonitorCanvas.width, faceMonitorCanvas.height
      ).data;
    } catch (err) {
      return; // frame not ready / stream ended — do not guess
    }

    const w = faceMonitorCanvas.width;
    const h = faceMonitorCanvas.height;
    // Restrict analysis to the central ~70% — a face in frame is centered.
    const x0 = Math.floor(w * 0.15);
    const y0 = Math.floor(h * 0.15);
    const x1 = Math.floor(w * 0.85);
    const y1 = Math.floor(h * 0.85);

    let skinCount = 0;
    let total = 0;
    let motionSum = 0;
    let diffPixels = 0;
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const idx = (y * w + x) * 4;
        const r = frameData[idx];
        const g = frameData[idx + 1];
        const b = frameData[idx + 2];
        // BT.601 YCbCr skin-tone heuristic.
        const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
        const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
        if (cr >= 133 && cr <= 173 && cb >= 77 && cb <= 127) skinCount++;
        total++;
        if (lastFaceFrameData !== null) {
          motionSum +=
            Math.abs(r - lastFaceFrameData[idx]) +
            Math.abs(g - lastFaceFrameData[idx + 1]) +
            Math.abs(b - lastFaceFrameData[idx + 2]);
          diffPixels++;
        }
      }
    }
    if (diffPixels > 0) lastFaceFrameData = frameData;
    const avgMotion = diffPixels > 0 ? motionSum / (diffPixels * 3 * 255) : 0;
    const hasSkin = total > 0 && skinCount / total >= FACE_SKIN_THRESHOLD;
    const hasMotion = avgMotion >= FACE_MOTION_THRESHOLD;

    faceMonitorStreak = hasSkin || hasMotion ? 0 : faceMonitorStreak + 1;
    if (faceMonitorStreak >= FACE_MONITOR_MISSES_TO_FLAG) {
      faceMonitorStreak = 0; // each sustained absence can accrue one strike
      reportFaceAbsence();
    }
  }

  function captureSnapshot(done) {
    let finished = false; // idempotency guard: two finish(null) paths exist
    let ownsStream = false; // true when a throwaway stream was acquired here

    function finish(url) {
      if (finished) return;
      finished = true;
      // Only stop a stream we acquired for this snapshot — the camera-gate
      // stream stays alive for the whole exam and is reused for later frames.
      if (ownsStream) stopCaptureStream();
      done(url);
    }

    function drawFrame(stream) {
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
    }

    // Reuse the live camera-gate stream when it is still active, so the proof
    // frame is instant and the browser is never re-prompted for permission.
    if (captureStream && captureStream.active && captureStream.getVideoTracks().length) {
      drawFrame(captureStream);
      return;
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
      ownsStream = true;
      navigator.mediaDevices.getUserMedia(
        { video: { width: 320, height: 240 }, audio: false }
      )
        .then(function (stream) {
          captureStream = stream;
          drawFrame(stream);
        })
        .catch(function () { finish(null); });
    } catch (err) {
      finish(null);
    }
  }

  function reportViolation(type, detail) {
    // Unproctored exams have no strike counting at all (the server also
    // ignores any reports), so nothing to report here.
    if (!PROCTORING_ENABLED) return;
    // Once a submission is underway, stop counting incidents.
    if (submitting || submitted || redirectScheduled) return;

    // Rate-limit: blur + visibilitychange + fullscreen-exit from the SAME
    // physical switch fire within milliseconds and MUST count as ONE strike.
    // The first event claims the cooldown window; anything inside it is
    // dropped before the snapshot/fetch runs, so the server never even sees
    // (or counts) a duplicate.
    const nowMs = performance.now();
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
    if ((data && data.auto_submit) ||
        (PROCTORING_ENABLED && violationCount >= MAX_VIOLATIONS)) {
      forceSubmitExam();
      return;
    }
    showWarningModal(violationCount, proctorAlertMessage);
    proctorAlertMessage = "";
  }

  // -------------------------- Warning modal ------------------------------
  function showWarningModal(count, message) {
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
        message ||
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
    // Stop the webcam + clear every proctoring interval IMMEDIATELY, before
    // any fetch — the camera light must go off the moment a forced submit is
    // triggered (the submission either succeeds, or the flagged page redirect
    // and force-submits on reload; either way the camera is already off).
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
      (ctrl && e.shiftKey && (key === "i" || key === "j" || key === "c" || key === "e" || key === "k")) ||
      (ctrl && (key === "u" || key === "s" || key === "p")) ||
      (e.key === "F5" || (ctrl && key === "r"));
    const isClipboardKey = ctrl && (key === "c" || key === "x" || key === "v");
    const isSelectAllKey = ctrl && key === "a";
    const isFullscreenToggle = e.key === "F11";

    if (isDevtoolsKey || isClipboardKey || isSelectAllKey || isFullscreenToggle) {
      e.preventDefault();
      e.stopPropagation();
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

  // Begins the detection loop exactly when the exam starts (after the camera
  // gate) and is a no-op thereafter, so no strikes are possible pre-exam.
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
  // the DevTools + face monitors AFTER a FAILED submission attempt, so the
  // student keeps a correctly-timed, fully-monitored exam while they retry.
  // The webcam is re-acquired silently (permission was granted at the gate).
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
      startDevToolsDetection(); // no camera needed for this one
      if (captureStream && captureStream.active) {
        startFaceMonitor();
      } else {
        acquireCameraStream(); // repopulates captureStream, then restarts the monitor
      }
    }
  }

  async function submitExam(auto) {
    // Never submit more than once (manual + auto, or repeated auto triggers).
    if (submitted || submitting) return;
    if (!auto) {
      const ok = window.confirm("Are you sure you want to submit your test?");
      if (!ok) return;
    }
    // The submission is now TRIGGERED: immediately stop every active media
    // track (the webcam light goes off NOW, not after the fetch completes),
    // clear the countdown and all proctoring poll intervals, and hide the
    // camera preview + overlays. Idempotent, so a forced auto-submit that
    // reaches this same handler tears nothing down twice.
    teardownExamMedia();
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
        else {
          window.alert("Submission failed: " + err.message);
          // Restore the countdown + proctoring monitors so an interrupted
          // submission never leaves the student with a frozen timer or an
          // unmonitored (camera-off) exam while they retry.
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
  // PROCTORED exams NEVER auto-start on load: a blocking camera-verification
  // gate runs first, and the timer/questions stay locked until a live video
  // stream is verified and the student clicks "Start Exam". The only
  // exceptions are server-side terminal states (already flagged, or deadline
  // already passed), which must be honored immediately regardless of camera
  // availability.
  //
  // UNPROCTORED exams skip the gate entirely: no camera check runs, no
  // lockdown is installed, and the clock starts as soon as the page loads so
  // the student takes the exam like a normal test.
  if (cfg.flagged || (PROCTORING_ENABLED && violationCount >= MAX_VIOLATIONS)) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    forceSubmitExam();
  } else if (cfg.deadlineUnix && computeRemaining() <= 0) {
    timerEl.textContent = "00:00";
    timerEl.classList.add("time-up");
    submitExam(true); // deadline already passed -> submit immediately
  } else if (PROCTORING_ENABLED) {
    initCameraGate();
  } else {
    beginExam(); // no camera gate — start the clock right away
  }
})();

