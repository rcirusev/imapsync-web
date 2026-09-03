(() => {
  const form = document.getElementById("sync-form");
  const startBtn = document.getElementById("start-btn");
  const statusBadge = document.getElementById("status-badge");
  const consoleEl = document.getElementById("console");
  const progressIndeterminate = document.getElementById("progress-indeterminate");
  const statsSection = document.getElementById("stats-section");
  const themeToggle = document.getElementById("theme-toggle");
  const binaryBanner = document.getElementById("binary-banner");

  // ---- Test connection (login-only check, no imapsync run) ----
  const testConnectionBtn = document.getElementById("test-connection-btn");
  const connTestStatus = {
    1: document.getElementById("conn-test-status-1"),
    2: document.getElementById("conn-test-status-2"),
  };

  function setConnTestStatus(side, cls, text) {
    const el = connTestStatus[side];
    el.hidden = false;
    el.className = `conn-test-status ${cls}`;
    el.textContent = text;
  }

  testConnectionBtn.addEventListener("click", () => {
    setConnTestStatus(1, "pending", "Testing…");
    setConnTestStatus(2, "pending", "Testing…");
    testConnectionBtn.disabled = true;
    const originalLabel = testConnectionBtn.textContent;
    testConnectionBtn.textContent = "Testing…";

    fetch("/api/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF_HEADERS },
      body: JSON.stringify({
        host1: form.host1.value, port1: form.port1.value, ssl1: form.ssl1.checked,
        user1: form.user1.value, password1: form.password1.value,
        authuser1: form.authuser1.value,
        host2: form.host2.value, port2: form.port2.value, ssl2: form.ssl2.checked,
        user2: form.user2.value, password2: form.password2.value,
        authuser2: form.authuser2.value,
      }),
    })
      .then((r) => r.json())
      .then((results) => {
        for (const side of ["1", "2"]) {
          const r = results[side];
          if (!r) continue;
          setConnTestStatus(side, r.ok ? "ok" : "fail", r.ok ? "✓ OK" : `✗ ${r.message}`);
        }
      })
      .catch(() => {
        setConnTestStatus(1, "fail", "✗ Request failed");
        setConnTestStatus(2, "fail", "✗ Request failed");
      })
      .finally(() => {
        testConnectionBtn.disabled = false;
        testConnectionBtn.textContent = originalLabel;
      });
  });

  let consoleTouched = false;

  // Remembers the currently-running single-migration job across page
  // reloads / dropped connections, so we can reattach to it on load instead
  // of losing track of it. Cleared as soon as the job reaches a final state.
  const ACTIVE_JOB_KEY = "imapsync_active_job";
  let reconnectAttempts = 0;
  let reconnectTimer = null;

  function setStatus(kind, label) {
    statusBadge.className = "status-badge status-" + kind;
    statusBadge.textContent = label;
  }

  function appendLine(text) {
    if (!consoleTouched) {
      consoleEl.textContent = "";
      consoleTouched = true;
    }
    const div = document.createElement("div");
    div.textContent = text;
    consoleEl.appendChild(div);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  function fmtDuration(seconds) {
    if (seconds == null) return "–";
    if (seconds < 60) return seconds.toFixed(1) + "s";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
  }

  function fmtOrDash(v) {
    return v === null || v === undefined ? "–" : v;
  }

  // Formats a unix-seconds timestamp relative to now, e.g. "in 3h 12m" or
  // "5h ago" — used for schedules' next/last run, which change on their own
  // clock rather than needing a live countdown.
  function fmtRelative(unixSeconds) {
    if (unixSeconds === null || unixSeconds === undefined) return "–";
    const deltaMin = Math.round((unixSeconds * 1000 - Date.now()) / 60000);
    const future = deltaMin >= 0;
    const abs = Math.abs(deltaMin);
    const h = Math.floor(abs / 60);
    const m = abs % 60;
    const span = h > 0 ? `${h}h ${m}m` : `${m}m`;
    return future ? `in ${span}` : `${span} ago`;
  }

  // Minimal HTML-escaping for any user-controlled value (host/user/
  // authuser/batch name/etc.) before it is interpolated into an
  // innerHTML template. These values come straight from the migration
  // form or an uploaded CSV, so they must never be trusted as markup.
  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  // Sent on every state-changing request (anything but a plain GET).
  // It carries no secret value — its only purpose is that a cross-site
  // <form> submit or a "simple" cross-origin fetch/XHR cannot attach a
  // custom header without first triggering a CORS preflight, which this
  // app never answers with an Access-Control-Allow-* response. Combined
  // with the matching check in app.py, this stops a malicious page from
  // riding a logged-in operator's cached Basic Auth credentials into a
  // mutating endpoint (CSRF).
  const CSRF_HEADERS = { "X-Requested-With": "imapsync-web" };

  // ---- imapsync availability banner ----
  fetch("/api/imapsync-status")
    .then((r) => r.json())
    .then(({ available }) => {
      binaryBanner.hidden = available;
      startBtn.disabled = !available;
    })
    .catch(() => {});

  // ---- Master/admin account toggle (New migration tab) ----
  // When on, --userN stays the mailbox being migrated but --authuserN (a
  // separate admin/master login) is who actually authenticates — so the
  // password field switches to meaning "the admin account's password"
  // instead of that mailbox's own. Needs the mail server to support this
  // (Dovecot master user, Zimbra admin auth, etc) — see README.
  function wireMasterAccountToggle(side) {
    const checkbox = document.getElementById(`master-account-${side}`);
    const authuserRow = document.getElementById(`authuser${side}-row`);
    const authuserInput = authuserRow.querySelector("input");
    const passwordLabel = document.getElementById(`password${side}-label`);
    const passwordHint = document.getElementById(`password${side}-hint`);

    function apply() {
      const on = checkbox.checked;
      authuserRow.hidden = !on;
      if (!on) authuserInput.value = "";
      passwordLabel.textContent = on ? "Admin account password" : "Password";
      passwordHint.textContent = on
        ? "The master/admin account's password, sent once, never stored or logged — not this mailbox's own password."
        : "Sent once to start the migration, never stored or logged.";
    }
    checkbox.addEventListener("change", apply);
    apply();
    return apply;
  }
  const applyMasterAccount1 = wireMasterAccountToggle("1");
  const applyMasterAccount2 = wireMasterAccountToggle("2");

  // ---- Delta sync toggle (New migration tab) ----
  const scheduleEnabledCheckbox = document.getElementById("schedule-enabled");
  const scheduleIntervalRow = document.getElementById("schedule-interval-row");
  const scheduleIntervalInput = document.getElementById("schedule-interval");
  scheduleEnabledCheckbox.addEventListener("change", () => {
    scheduleIntervalRow.hidden = !scheduleEnabledCheckbox.checked;
  });

  // ---- New migration form ----
  form.addEventListener("submit", (evt) => {
    evt.preventDefault();

    const data = new FormData(form);
    const payload = {
      host1: data.get("host1"),
      user1: data.get("user1"),
      authuser1: data.get("authuser1") || "",
      password1: data.get("password1"),
      port1: data.get("port1"),
      ssl1: form.ssl1.checked,
      host2: data.get("host2"),
      user2: data.get("user2"),
      authuser2: data.get("authuser2") || "",
      password2: data.get("password2"),
      port2: data.get("port2"),
      ssl2: form.ssl2.checked,
      options: {
        dry: form.dry.checked,
        syncflags: form.syncflags.checked,
        delete2duplicates: form.delete2duplicates.checked,
        subscribeall: form.subscribeall.checked,
        exclude: data.get("exclude") || "",
      },
      schedule: {
        enabled: scheduleEnabledCheckbox.checked,
        interval_hours: parseFloat(scheduleIntervalInput.value) || 0,
      },
    };

    consoleTouched = false;
    consoleEl.textContent = "";
    statsSection.hidden = true;
    progressIndeterminate.hidden = false;
    setStatus("running", "Running…");
    startBtn.disabled = true;
    startBtn.textContent = "Migration running…";

    fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF_HEADERS },
      body: JSON.stringify(payload),
    })
      .then((res) => res.json().then((body) => ({ ok: res.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) throw new Error(body.error || "Failed to start");
        openStream(body.job_id);
      })
      .catch((err) => {
        appendLine("ERROR: " + err.message);
        setStatus("error", "Failed to start");
        progressIndeterminate.hidden = true;
        startBtn.disabled = false;
        startBtn.textContent = "Start migration";
      });
  });

  function openStream(jobId, isReconnect) {
    const source = new EventSource(`/api/stream/${jobId}`);
    localStorage.setItem(ACTIVE_JOB_KEY, jobId);

    if (isReconnect) {
      // Full replay is about to arrive from the server again (it re-sends
      // everything it has buffered for this job) — clear the console first
      // so lines aren't duplicated after a reconnect.
      consoleTouched = false;
      consoleEl.textContent = "";
    }

    source.addEventListener("log", (e) => {
      reconnectAttempts = 0;
      const { line } = JSON.parse(e.data);
      appendLine(line);
    });

    source.addEventListener("done", (e) => {
      const stats = JSON.parse(e.data);
      source.close();
      localStorage.removeItem(ACTIVE_JOB_KEY);
      progressIndeterminate.hidden = true;
      startBtn.disabled = false;
      startBtn.textContent = "Start migration";

      if (stats.status === "success") {
        setStatus("success", "Completed");
      } else if (stats.status === "interrupted") {
        // The job was still "running" when this server process last
        // restarted (crash, VM reboot, systemd restart) — it never got a
        // real result. Nothing was lost though: re-run the same accounts
        // (History tab → Resume) and imapsync will only transfer whatever
        // is still missing on the destination, not start over from zero.
        setStatus("error", "Interrupted");
      } else {
        setStatus("error", "Failed");
      }

      document.getElementById("stat-folders").textContent = fmtOrDash(stats.folders);
      document.getElementById("stat-messages").textContent = fmtOrDash(stats.messages);
      document.getElementById("stat-errors").textContent = fmtOrDash(stats.errors);
      document.getElementById("stat-duration").textContent = fmtDuration(stats.duration_s);
      statsSection.hidden = false;
    });

    source.onerror = () => {
      source.close();
      reconnectAttempts += 1;

      // Give up for real only after a couple of minutes of failed retries —
      // a dropped wifi connection or a quick service restart shouldn't be
      // able to tell the two apart from a permanent failure.
      if (reconnectAttempts > 30) {
        setStatus("error", "Connection lost");
        progressIndeterminate.hidden = true;
        startBtn.disabled = false;
        startBtn.textContent = "Start migration";
        localStorage.removeItem(ACTIVE_JOB_KEY);
        return;
      }

      setStatus("running", "Reconnecting…");
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => openStream(jobId, true), 3000);
    };
  }

  // ---- Reattach to a migration that was still running when this page (or
  // this browser tab/connection) was last open, e.g. after a reload, a
  // dropped connection, or the tab being closed and reopened. The server
  // itself doesn't need any of this — the job keeps running in a background
  // thread regardless of whether anyone is watching; this only restores the
  // live view client-side. ----
  (function reattachIfPending() {
    const pendingJobId = localStorage.getItem(ACTIVE_JOB_KEY);
    if (!pendingJobId) return;
    consoleTouched = false;
    consoleEl.textContent = "";
    statsSection.hidden = true;
    progressIndeterminate.hidden = false;
    setStatus("running", "Reconnecting…");
    startBtn.disabled = true;
    startBtn.textContent = "Migration running…";
    openStream(pendingJobId, true);
  })();

  // ---- Bulk migration (CSV) ----
  const bulkFile = document.getElementById("bulk-file");
  const bulkStartBtn = document.getElementById("bulk-start-btn");
  const bulkRowErrors = document.getElementById("bulk-row-errors");
  const bulkProgressCard = document.getElementById("bulk-progress-card");
  const bulkProgressLabel = document.getElementById("bulk-progress-label");
  const bulkConsole = document.getElementById("bulk-console");
  const bulkResultsCard = document.getElementById("bulk-results-card");
  const bulkResultsBody = document.getElementById("bulk-results-body");
  const bulkSummary = document.getElementById("bulk-summary");
  const bulkProgressIndeterminate = document.getElementById("bulk-progress-indeterminate");
  const bulkBatchNameInput = document.getElementById("bulk-batch-name");
  const bulkStopBtn = document.getElementById("bulk-stop-btn");
  const bulkScheduleEnabledCheckbox = document.getElementById("bulk-schedule-enabled");
  const bulkScheduleIntervalRow = document.getElementById("bulk-schedule-interval-row");
  const bulkScheduleIntervalInput = document.getElementById("bulk-schedule-interval");
  const bulkScheduleHint = document.getElementById("bulk-schedule-hint");

  bulkScheduleEnabledCheckbox.addEventListener("change", () => {
    bulkScheduleIntervalRow.hidden = !bulkScheduleEnabledCheckbox.checked;
    bulkScheduleHint.hidden = !bulkScheduleEnabledCheckbox.checked;
  });

  let bulkRowSource = null;
  let bulkReconnectAttempts = 0;
  let currentBulkBatchId = null;

  function requestStopBatch(batchId, onSettled) {
    fetch(`/api/bulk/${batchId}/stop`, { method: "POST", headers: CSRF_HEADERS })
      .then((r) => r.json())
      .then((body) => onSettled && onSettled(body.error || null))
      .catch(() => onSettled && onSettled("Failed to reach the server."));
  }

  bulkStopBtn.addEventListener("click", () => {
    if (!currentBulkBatchId) return;
    bulkStopBtn.disabled = true;
    bulkStopBtn.textContent = "Stopping…";
    requestStopBatch(currentBulkBatchId, (error) => {
      if (error) {
        bulkStopBtn.disabled = false;
        bulkStopBtn.textContent = "Stop batch";
      }
      // On success, leave the button disabled — the batch_done event
      // (once the in-flight row finishes) will reset the whole console.
    });
  });

  function bulkAppendLine(text) {
    const div = document.createElement("div");
    div.textContent = text;
    bulkConsole.appendChild(div);
    bulkConsole.scrollTop = bulkConsole.scrollHeight;
  }

  bulkStartBtn.addEventListener("click", () => {
    const file = bulkFile.files[0];
    if (!file) {
      bulkRowErrors.hidden = false;
      bulkRowErrors.textContent = "Choose a CSV file first.";
      return;
    }

    bulkRowErrors.hidden = true;
    bulkResultsCard.hidden = true;
    bulkResultsBody.innerHTML = "";
    bulkProgressCard.hidden = false;
    bulkProgressIndeterminate.hidden = false;
    bulkConsole.textContent = "";
    bulkProgressLabel.textContent = "Uploading…";
    bulkStartBtn.disabled = true;
    bulkStartBtn.textContent = "Running…";
    bulkStopBtn.disabled = false;
    bulkStopBtn.textContent = "Stop batch";

    const formData = new FormData();
    formData.append("file", file);
    formData.append("name", bulkBatchNameInput.value.trim());
    formData.append("schedule_enabled", bulkScheduleEnabledCheckbox.checked ? "true" : "false");
    formData.append("schedule_interval_hours", bulkScheduleIntervalInput.value || "0");

    fetch("/api/bulk/start", { method: "POST", headers: CSRF_HEADERS, body: formData })
      .then((res) => res.json().then((body) => ({ ok: res.ok, body })))
      .then(({ ok, body }) => {
        if (!ok) throw new Error(body.error || "Failed to start bulk migration");
        if (body.row_errors && body.row_errors.length) {
          bulkRowErrors.hidden = false;
          bulkRowErrors.innerHTML =
            `<strong>${body.row_errors.length} row(s) skipped:</strong><br>` +
            body.row_errors.map((e) => `Row ${e.row}: ${e.reason}`).join("<br>");
        }
        currentBulkBatchId = body.batch_id;
        openBulkStream(body.batch_id, body.total);
      })
      .catch((err) => {
        bulkProgressLabel.textContent = "Failed to start";
        bulkAppendLine("ERROR: " + err.message);
        bulkStartBtn.disabled = false;
        bulkStartBtn.textContent = "Start bulk migration";
      });
  });

  function openBulkStream(batchId, total, isReconnect) {
    const source = new EventSource(`/api/bulk/stream/${batchId}`);
    bulkResultsCard.hidden = false;

    if (isReconnect) {
      // The server replays every buffered row_start/row_done event on
      // reconnect — clear what's shown first so rows aren't duplicated.
      bulkResultsBody.innerHTML = "";
      bulkConsole.textContent = "";
    }

    source.addEventListener("row_start", (e) => {
      bulkReconnectAttempts = 0;
      const row = JSON.parse(e.data);
      bulkProgressLabel.textContent =
        `Row ${row.index}/${row.total} — ${row.user1}@${row.host1} → ${row.user2}@${row.host2}`;
      bulkConsole.textContent = "";
      if (bulkRowSource) bulkRowSource.close();
      bulkRowSource = new EventSource(`/api/stream/${row.job_id}`);
      bulkRowSource.addEventListener("log", (ev) => {
        bulkAppendLine(JSON.parse(ev.data).line);
      });
    });

    source.addEventListener("row_done", (e) => {
      bulkReconnectAttempts = 0;
      const row = JSON.parse(e.data);
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${row.index}/${row.total}</td>
        <td>${escapeHtml(row.user1)}@${escapeHtml(row.host1)}</td>
        <td>${escapeHtml(row.user2)}@${escapeHtml(row.host2)}</td>
        <td><span class="status-badge status-${row.status}">${STATUS_LABEL[row.status] || row.status}</span></td>
        <td>${fmtOrDash(row.messages)}</td>
        <td>${fmtOrDash(row.errors)}</td>
        <td>${fmtDuration(row.duration_s)}</td>
        <td><button class="btn btn-ghost btn-small" data-job="${row.job_id}">View log</button></td>
      `;
      bulkResultsBody.appendChild(tr);
      tr.querySelector("button[data-job]").addEventListener("click", () => openLogModal(row.job_id));
    });

    source.addEventListener("batch_done", (e) => {
      const summary = JSON.parse(e.data);
      source.close();
      if (bulkRowSource) { bulkRowSource.close(); bulkRowSource = null; }
      bulkProgressIndeterminate.hidden = true;
      bulkProgressLabel.textContent = summary.stopped
        ? "Bulk migration stopped"
        : "Bulk migration finished";
      bulkSummary.className = "status-badge " + (
        summary.stopped ? "status-stopped" : summary.error ? "status-error" : "status-success"
      );
      bulkSummary.textContent = `${summary.success}/${summary.total} succeeded` +
        (summary.error ? `, ${summary.error} failed` : "") +
        (summary.stopped ? " (stopped early)" : "");
      bulkStartBtn.disabled = false;
      bulkStartBtn.textContent = "Start bulk migration";
      bulkStopBtn.disabled = true;
      bulkStopBtn.textContent = "Stop batch";
      bulkFile.value = "";
      currentBulkBatchId = null;
    });

    source.onerror = () => {
      source.close();
      if (bulkRowSource) { bulkRowSource.close(); bulkRowSource = null; }
      bulkReconnectAttempts += 1;

      // A brief drop shouldn't lose the live view — retry for a while.
      // Even if this gives up for good, the batch itself keeps running on
      // the server regardless; its progress is always visible in the
      // History tab's "Bulk batches" table (which persists to disk after
      // every row, survives reloads, and doesn't depend on this stream).
      if (bulkReconnectAttempts > 10) {
        bulkProgressIndeterminate.hidden = true;
        bulkProgressLabel.textContent =
          "Connection lost — check the History tab's Bulk batches table for live progress.";
        bulkStartBtn.disabled = false;
        bulkStartBtn.textContent = "Start bulk migration";
        return;
      }

      bulkProgressLabel.textContent = "Reconnecting…";
      setTimeout(() => openBulkStream(batchId, total, true), 3000);
    };
  }

  // ---- Tabs ----
  const tabs = document.querySelectorAll(".tab");
  const panels = {
    new: document.getElementById("tab-new"),
    bulk: document.getElementById("tab-bulk"),
    history: document.getElementById("tab-history"),
  };
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      Object.values(panels).forEach((p) => (p.hidden = true));
      panels[tab.dataset.tab].hidden = false;
      historyTabVisible = tab.dataset.tab === "history";
      if (historyTabVisible) {
        loadHistory();
        loadBatches();
        loadSchedules();
      }
    });
  });

  // ---- History ----
  const historyBody = document.getElementById("history-body");
  const batchesBody = document.getElementById("batches-body");
  const schedulesBody = document.getElementById("schedules-body");
  const refreshHistoryBtn = document.getElementById("refresh-history");
  const refreshBatchesBtn = document.getElementById("refresh-batches");
  const refreshSchedulesBtn = document.getElementById("refresh-schedules");
  refreshHistoryBtn.addEventListener("click", withRefreshFeedback(refreshHistoryBtn, loadHistory));
  refreshBatchesBtn.addEventListener("click", withRefreshFeedback(refreshBatchesBtn, loadBatches));
  refreshSchedulesBtn.addEventListener("click", withRefreshFeedback(refreshSchedulesBtn, loadSchedules));

  const clearHistoryBtn = document.getElementById("clear-history");
  clearHistoryBtn.addEventListener("click", () => {
    const ok = confirm(
      "Clear history? This removes finished single migrations and bulk " +
      "batches (and their log files). Anything still running, and " +
      "anything an active delta sync still needs, is kept."
    );
    if (!ok) return;
    clearHistoryBtn.disabled = true;
    clearHistoryBtn.textContent = "Clearing…";
    fetch("/api/history/clear", { method: "POST", headers: CSRF_HEADERS })
      .then((r) => r.json())
      .then((result) => {
        loadHistory();
        loadBatches();
        loadSchedules();
        clearHistoryBtn.textContent = `Cleared ${result.jobs_deleted + result.batches_deleted}`;
        setTimeout(() => {
          clearHistoryBtn.textContent = "Clear history";
        }, 2000);
      })
      .catch(() => {
        clearHistoryBtn.textContent = "Clear history";
        alert("Failed to clear history — check the server log.");
      })
      .finally(() => {
        clearHistoryBtn.disabled = false;
      });
  });

  const STATUS_LABEL = {
    running: "Running", success: "Completed", error: "Failed", interrupted: "Interrupted",
  };
  const BATCH_STATUS_LABEL = {
    running: "Running", done: "Done", interrupted: "Interrupted", stopped: "Stopped",
  };

  // While the History tab is open and at least one job/batch is "running",
  // we re-fetch every few seconds so status/progress update on their own —
  // this is what lets you check how far a 100-mailbox bulk run has gotten
  // from the History tab, at any time, without keeping the Bulk tab open.
  let historyTabVisible = false;
  let historyPollTimer = null;
  let batchesPollTimer = null;

  // Briefly disables a refresh button and shows "Refreshing…" while its
  // fetch is in flight, then restores it — otherwise a refresh that finds
  // no new data looks identical to a refresh that silently did nothing.
  function withRefreshFeedback(button, loadFn) {
    return () => {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Refreshing…";
      Promise.resolve(loadFn()).finally(() => {
        button.disabled = false;
        button.textContent = original;
      });
    };
  }

  let allSchedules = [];

  function renderScheduleRow(sched) {
    const tr = document.createElement("tr");
    tr._sched = sched;
    tr.innerHTML = `
      <td>${escapeHtml(sched.label)}${sched.kind === "batch" ? ' <span class="bulk-tag">bulk</span>' : ""}</td>
      <td>${sched.interval_hours}h</td>
      <td>${fmtRelative(sched.last_run_at)}</td>
      <td>${fmtRelative(sched.next_run_at)}</td>
      <td class="history-actions">
        <button class="btn btn-ghost btn-small" data-edit-schedule="${sched.id}">Edit</button>
        <button class="btn btn-ghost btn-small" data-run-now="${sched.id}">Run now</button>
        <button class="btn btn-ghost btn-small" data-delete-schedule="${sched.id}">Delete</button>
      </td>
    `;
    return tr;
  }

  function renderScheduleEditRow(sched) {
    const tr = document.createElement("tr");
    tr._sched = sched;
    const passwordFields = sched.kind === "job"
      ? `
        <input type="password" class="filter-input" data-edit-password1
               placeholder="New source password (leave blank to keep)" autocomplete="off">
        <input type="password" class="filter-input" data-edit-password2
               placeholder="New destination password (leave blank to keep)" autocomplete="off">
      `
      : `<span class="field-hint">Passwords can't be edited for a batch — re-upload the CSV instead.</span>`;
    tr.innerHTML = `
      <td colspan="4">
        <div class="schedule-edit-form">
          <label class="field field-small">
            <span>Every (hours)</span>
            <input type="number" min="0.25" step="0.25" value="${sched.interval_hours}" data-edit-interval>
          </label>
          ${passwordFields}
          <span class="conn-test-status" data-edit-error hidden></span>
        </div>
      </td>
      <td class="history-actions">
        <button class="btn btn-primary btn-small" data-save-schedule="${sched.id}">Save</button>
        <button class="btn btn-ghost btn-small" data-cancel-schedule="${sched.id}">Cancel</button>
      </td>
    `;
    return tr;
  }

  function renderSchedules() {
    if (!allSchedules.length) {
      schedulesBody.innerHTML = `<tr><td colspan="5" class="history-empty">No scheduled delta syncs yet.</td></tr>`;
      return;
    }
    schedulesBody.innerHTML = "";
    allSchedules.forEach((sched) => schedulesBody.appendChild(renderScheduleRow(sched)));

    schedulesBody.querySelectorAll("button[data-run-now]").forEach((btn) => {
      btn.addEventListener("click", () => {
        btn.disabled = true;
        btn.textContent = "Running…";
        fetch(`/api/schedules/${btn.dataset.runNow}/run-now`, { method: "POST", headers: CSRF_HEADERS })
          .then(() => {
            loadSchedules();
            setTimeout(() => { loadHistory(); loadBatches(); }, 2000);
          });
      });
    });
    schedulesBody.querySelectorAll("button[data-delete-schedule]").forEach((btn) => {
      btn.addEventListener("click", () => {
        btn.disabled = true;
        btn.textContent = "Deleting…";
        fetch(`/api/schedules/${btn.dataset.deleteSchedule}`, { method: "DELETE", headers: CSRF_HEADERS })
          .then(() => loadSchedules());
      });
    });
    schedulesBody.querySelectorAll("button[data-edit-schedule]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tr = btn.closest("tr");
        tr.replaceWith(renderScheduleEditRow(tr._sched));
        wireScheduleEditRow(tr._sched.id);
      });
    });
  }

  function wireScheduleEditRow(scheduleId) {
    const tr = schedulesBody.querySelector(
      `button[data-save-schedule="${scheduleId}"]`
    ).closest("tr");
    const errorEl = tr.querySelector("[data-edit-error]");

    tr.querySelector("[data-cancel-schedule]").addEventListener("click", () => {
      renderSchedules();
    });

    tr.querySelector("[data-save-schedule]").addEventListener("click", () => {
      const intervalInput = tr.querySelector("[data-edit-interval]");
      const interval = parseFloat(intervalInput.value);
      if (!interval || interval < 0.25) {
        errorEl.hidden = false;
        errorEl.className = "conn-test-status fail";
        errorEl.textContent = "Minimum interval is 0.25h (15 minutes).";
        return;
      }
      const pw1El = tr.querySelector("[data-edit-password1]");
      const pw2El = tr.querySelector("[data-edit-password2]");
      const saveBtn = tr.querySelector("[data-save-schedule]");
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
      fetch(`/api/schedules/${scheduleId}/edit`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...CSRF_HEADERS },
        body: JSON.stringify({
          interval_hours: interval,
          password1: pw1El ? pw1El.value : "",
          password2: pw2El ? pw2El.value : "",
        }),
      })
        .then(async (r) => {
          const body = await r.json();
          if (!r.ok) throw new Error(body.error || "Failed to save.");
          loadSchedules();
        })
        .catch((err) => {
          errorEl.hidden = false;
          errorEl.className = "conn-test-status fail";
          errorEl.textContent = err.message;
          saveBtn.disabled = false;
          saveBtn.textContent = "Save";
        });
    });
  }

  function loadSchedules() {
    return fetch("/api/schedules")
      .then((r) => r.json())
      .then((schedules) => {
        allSchedules = schedules;
        renderSchedules();
      })
      .catch(() => {
        schedulesBody.innerHTML = `<tr><td colspan="5" class="history-empty">Failed to load schedules.</td></tr>`;
      });
  }

  // Last-fetched rows, kept around so the search box / status filter can
  // re-render instantly from memory instead of re-fetching on every
  // keystroke. Filtering never touches the polling/auto-refresh logic below.
  let allBatches = [];
  let allJobs = [];

  const batchesSearchInput = document.getElementById("batches-search");
  const batchesStatusFilter = document.getElementById("batches-status-filter");
  const historySearchInput = document.getElementById("history-search");
  const historyStatusFilter = document.getElementById("history-status-filter");

  function filterBatches(batches) {
    const q = batchesSearchInput.value.trim().toLowerCase();
    const status = batchesStatusFilter.value;
    return batches.filter((b) => {
      if (status !== "all" && b.status !== status) return false;
      if (!q) return true;
      const name = (b.name || `batch ${b.id.slice(0, 8)}`).toLowerCase();
      return name.includes(q);
    });
  }

  function filterJobs(jobs) {
    const q = historySearchInput.value.trim().toLowerCase();
    const status = historyStatusFilter.value;
    return jobs.filter((j) => {
      if (status !== "all" && j.status !== status) return false;
      if (!q) return true;
      const haystack = `${j.user1} ${j.host1} ${j.user2} ${j.host2}`.toLowerCase();
      return haystack.includes(q);
    });
  }

  function renderBatches() {
    const batches = filterBatches(allBatches);
    if (!allBatches.length) {
      batchesBody.innerHTML = `<tr><td colspan="7" class="history-empty">No bulk migrations yet.</td></tr>`;
      return;
    }
    if (!batches.length) {
      batchesBody.innerHTML = `<tr><td colspan="7" class="history-empty">No batches match your filter.</td></tr>`;
      return;
    }
    batchesBody.innerHTML = "";
    batches.forEach((batch) => {
      const tr = document.createElement("tr");
      const started = new Date(batch.created_at * 1000).toLocaleString();
      const badgeStatus = batch.status === "done" ? "success" : batch.status;
      const displayName = batch.name || `Batch ${batch.id.slice(0, 8)}`;
      tr.innerHTML = `
        <td>${started}</td>
        <td>${escapeHtml(displayName)}${batch.schedule_id ? ' <span class="bulk-tag">auto</span>' : ""}</td>
        <td>${batch.completed}/${batch.total}</td>
        <td>${batch.success}</td>
        <td>${batch.error}</td>
        <td><span class="status-badge status-${badgeStatus}">${BATCH_STATUS_LABEL[batch.status] || batch.status}</span></td>
        <td class="history-actions">
          <button class="btn btn-ghost btn-small" data-view-batch="${batch.id}">View rows</button>
          ${batch.status === "running" ? `<button class="btn btn-ghost btn-small" data-stop-batch="${batch.id}">Stop</button>` : ""}
          ${batch.error > 0 ? `<a class="btn btn-ghost btn-small" href="/api/batches/${batch.id}/failed.csv">Download failed CSV</a>` : ""}
        </td>
      `;
      tr._batch = batch;
      batchesBody.appendChild(tr);
    });

    batchesBody.querySelectorAll("button[data-view-batch]").forEach((btn) => {
      btn.addEventListener("click", () => openBatchModal(btn.closest("tr")._batch));
    });
    batchesBody.querySelectorAll("button[data-stop-batch]").forEach((btn) => {
      btn.addEventListener("click", () => {
        btn.disabled = true;
        btn.textContent = "Stopping…";
        requestStopBatch(btn.dataset.stopBatch, () => loadBatches());
      });
    });
  }

  function renderHistory() {
    const jobs = filterJobs(allJobs);
    if (!allJobs.length) {
      historyBody.innerHTML = `<tr><td colspan="8" class="history-empty">No migrations yet.</td></tr>`;
      return;
    }
    if (!jobs.length) {
      historyBody.innerHTML = `<tr><td colspan="8" class="history-empty">No migrations match your filter.</td></tr>`;
      return;
    }
    historyBody.innerHTML = "";
    jobs.forEach((job) => {
      const canResume = job.status === "error" || job.status === "interrupted";
      const tr = document.createElement("tr");
      const started = job.started_at
        ? new Date(job.started_at * 1000).toLocaleString()
        : new Date(job.created_at * 1000).toLocaleString();
      tr.innerHTML = `
        <td>${started}${job.batch_id ? ' <span class="bulk-tag">bulk</span>' : ""}${job.schedule_id ? ' <span class="bulk-tag">auto</span>' : ""}</td>
        <td>${escapeHtml(job.user1)}@${escapeHtml(job.host1)}${job.authuser1 ? ' <span class="bulk-tag" title="Authenticated via master account ' + escapeHtml(job.authuser1) + '">master</span>' : ""}</td>
        <td>${escapeHtml(job.user2)}@${escapeHtml(job.host2)}${job.authuser2 ? ' <span class="bulk-tag" title="Authenticated via master account ' + escapeHtml(job.authuser2) + '">master</span>' : ""}</td>
        <td><span class="status-badge status-${job.status}">${STATUS_LABEL[job.status] || job.status}</span></td>
        <td>${fmtOrDash(job.messages)}</td>
        <td>${fmtOrDash(job.errors)}</td>
        <td>${fmtDuration(job.duration_s)}</td>
        <td class="history-actions">
          <button class="btn btn-ghost btn-small" data-job="${job.id}">View log</button>
          ${canResume ? `<button class="btn btn-ghost btn-small" data-resume="${job.id}">Resume</button>` : ""}
        </td>
      `;
      tr._job = job;
      historyBody.appendChild(tr);
    });
    historyBody.querySelectorAll("button[data-job]").forEach((btn) => {
      btn.addEventListener("click", () => openLogModal(btn.dataset.job));
    });
    historyBody.querySelectorAll("button[data-resume]").forEach((btn) => {
      btn.addEventListener("click", () => resumeJob(btn.closest("tr")._job));
    });
  }

  batchesSearchInput.addEventListener("input", renderBatches);
  batchesStatusFilter.addEventListener("change", renderBatches);
  historySearchInput.addEventListener("input", renderHistory);
  historyStatusFilter.addEventListener("change", renderHistory);

  function loadBatches() {
    return fetch("/api/batches")
      .then((r) => r.json())
      .then((batches) => {
        clearTimeout(batchesPollTimer);
        allBatches = batches;
        renderBatches();
        const anyRunning = batches.some((b) => b.status === "running");
        if (anyRunning && historyTabVisible) {
          batchesPollTimer = setTimeout(loadBatches, 4000);
        }
      })
      .catch(() => {
        batchesBody.innerHTML = `<tr><td colspan="7" class="history-empty">Failed to load bulk batches.</td></tr>`;
      });
  }

  function loadHistory() {
    return fetch("/api/jobs")
      .then((r) => r.json())
      .then((jobs) => {
        clearTimeout(historyPollTimer);
        allJobs = jobs;
        renderHistory();
        const anyRunning = jobs.some((j) => j.status === "running");
        if (anyRunning && historyTabVisible) {
          historyPollTimer = setTimeout(loadHistory, 4000);
        }
      })
      .catch(() => {
        historyBody.innerHTML = `<tr><td colspan="8" class="history-empty">Failed to load history.</td></tr>`;
      });
  }

  // Re-launches the same host/port/SSL/user/options as a fresh migration —
  // this is how you "resume" an interrupted or failed job. imapsync itself
  // is incremental: it checks what's already on the destination and only
  // transfers what's missing, so re-running doesn't re-copy anything that
  // already made it across. Passwords are never stored, so those two
  // fields are deliberately left blank for you to re-enter.
  function resumeJob(job) {
    if (!job) return;
    let options = {};
    try {
      options = JSON.parse(job.options_json || "{}");
    } catch (e) {
      options = {};
    }

    form.host1.value = job.host1 || "";
    form.port1.value = job.port1 || "";
    form.ssl1.checked = !!job.ssl1;
    form.user1.value = job.user1 || "";
    document.getElementById("master-account-1").checked = !!job.authuser1;
    applyMasterAccount1();
    form.authuser1.value = job.authuser1 || "";
    form.password1.value = "";
    form.host2.value = job.host2 || "";
    form.port2.value = job.port2 || "";
    form.ssl2.checked = !!job.ssl2;
    form.user2.value = job.user2 || "";
    document.getElementById("master-account-2").checked = !!job.authuser2;
    applyMasterAccount2();
    form.authuser2.value = job.authuser2 || "";
    form.password2.value = "";
    form.dry.checked = !!options.dry;
    form.syncflags.checked = options.syncflags !== false;
    form.delete2duplicates.checked = !!options.delete2duplicates;
    form.subscribeall.checked = options.subscribeall !== false;
    form.exclude.value = options.exclude || "";

    tabs.forEach((t) => t.classList.remove("active"));
    document.querySelector('.tab[data-tab="new"]').classList.add("active");
    Object.values(panels).forEach((p) => (p.hidden = true));
    panels.new.hidden = false;

    setStatus("idle", "Idle");
    statsSection.hidden = true;
    consoleTouched = false;
    consoleEl.textContent =
      "Re-enter both passwords (never stored) and click “Start migration” to resume — " +
      "imapsync only transfers what's still missing on the destination, it won't re-copy anything already synced.";
    form.password1.focus();
  }

  // ---- Log modal ----
  const modal = document.getElementById("log-modal");
  const modalLog = document.getElementById("modal-log");
  let modalSource = null;

  function stopModalStream() {
    if (modalSource) {
      modalSource.close();
      modalSource = null;
    }
  }

  modal.querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", () => {
      modal.hidden = true;
      stopModalStream();
    })
  );

  function openLogModal(jobId) {
    stopModalStream();
    modal.hidden = false;
    modalLog.textContent = "Loading…";

    const showSavedLog = () => {
      fetch(`/api/jobs/${jobId}/log`)
        .then((r) => r.text())
        .then((text) => (modalLog.textContent = text || "(empty log)"));
    };

    fetch(`/api/jobs/${jobId}`)
      .then((r) => r.json())
      .then((job) => {
        if (!job || job.status !== "running") {
          showSavedLog();
          return;
        }
        // Job is still running — tail it live instead of showing a
        // point-in-time snapshot, so reopening this from History after a
        // reconnect behaves just like the New migration console does.
        modalLog.textContent = "";
        modalSource = new EventSource(`/api/stream/${jobId}`);
        modalSource.addEventListener("log", (e) => {
          modalLog.textContent += JSON.parse(e.data).line + "\n";
          modalLog.scrollTop = modalLog.scrollHeight;
        });
        modalSource.addEventListener("done", (e) => {
          const stats = JSON.parse(e.data);
          stopModalStream();
          modalLog.textContent += `\n--- finished: ${STATUS_LABEL[stats.status] || stats.status} ---`;
          loadHistory();
        });
        modalSource.onerror = stopModalStream;
      })
      .catch(showSavedLog);
  }

  // ---- Batch detail modal (rows for one bulk batch, like drilling into a
  // named Office 365 migration batch) ----
  const batchModal = document.getElementById("batch-modal");
  const batchModalTitle = document.getElementById("batch-modal-title");
  const batchModalBody = document.getElementById("batch-modal-body");

  batchModal.querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", () => (batchModal.hidden = true))
  );

  function openBatchModal(batch) {
    batchModal.hidden = false;
    batchModalTitle.textContent = batch.name || `Batch ${batch.id.slice(0, 8)}`;
    batchModalBody.innerHTML = `<tr><td colspan="7" class="history-empty">Loading…</td></tr>`;

    fetch(`/api/batches/${batch.id}/jobs`)
      .then((r) => r.json())
      .then((jobs) => {
        if (!jobs.length) {
          batchModalBody.innerHTML = `<tr><td colspan="7" class="history-empty">No rows recorded yet.</td></tr>`;
          return;
        }
        batchModalBody.innerHTML = "";
        jobs.forEach((job) => {
          const canResume = job.status === "error" || job.status === "interrupted";
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td>${escapeHtml(job.user1)}@${escapeHtml(job.host1)}</td>
            <td>${escapeHtml(job.user2)}@${escapeHtml(job.host2)}</td>
            <td><span class="status-badge status-${job.status}">${STATUS_LABEL[job.status] || job.status}</span></td>
            <td>${fmtOrDash(job.messages)}</td>
            <td>${fmtOrDash(job.errors)}</td>
            <td>${fmtDuration(job.duration_s)}</td>
            <td class="history-actions">
              <button class="btn btn-ghost btn-small" data-job="${job.id}">View log</button>
              ${canResume ? `<button class="btn btn-ghost btn-small" data-resume="${job.id}">Resume</button>` : ""}
            </td>
          `;
          tr._job = job;
          batchModalBody.appendChild(tr);
        });
        batchModalBody.querySelectorAll("button[data-job]").forEach((btn) => {
          btn.addEventListener("click", () => openLogModal(btn.dataset.job));
        });
        batchModalBody.querySelectorAll("button[data-resume]").forEach((btn) => {
          btn.addEventListener("click", () => {
            batchModal.hidden = true;
            resumeJob(btn.closest("tr")._job);
          });
        });
      })
      .catch(() => {
        batchModalBody.innerHTML = `<tr><td colspan="7" class="history-empty">Failed to load batch rows.</td></tr>`;
      });
  }

  // ---- Theme toggle ----
  const root = document.documentElement;
  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
  }
  const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(preferred);
  themeToggle.addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next);
  });
})();
