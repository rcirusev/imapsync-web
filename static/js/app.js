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

  // Renders "user@host" as two lines — the account name on top, the host
  // underneath in smaller/muted text — instead of one long "user@host"
  // string, so a long hostname doesn't dominate the row. `badgeHtml`, if
  // given, is appended after the account name (e.g. the "master" tag).
  function accountCell(user, host, badgeHtml) {
    return (
      '<div class="account-cell">' +
      `<span class="account-name">${escapeHtml(user)}</span>${badgeHtml || ""}` +
      `<span class="account-host">@${escapeHtml(host)}</span>` +
      "</div>"
    );
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
        <td class="nowrap">${row.index}/${row.total}</td>
        <td>${accountCell(row.user1, row.host1)}</td>
        <td>${accountCell(row.user2, row.host2)}</td>
        <td class="nowrap"><span class="status-badge status-${row.status}">${STATUS_LABEL[row.status] || row.status}</span></td>
        <td class="nowrap">${fmtOrDash(row.messages)}</td>
        <td class="nowrap">${fmtOrDash(row.errors)}</td>
        <td class="nowrap">${fmtDuration(row.duration_s)}</td>
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
    const accountsCol = sched.kind === "job"
      ? `<div class="account-pair">${accountCell(sched.user1, sched.host1)}` +
        `<span class="account-arrow">\u2192</span>${accountCell(sched.user2, sched.host2)}</div>`
      : `${escapeHtml(sched.label)} <span class="bulk-tag">bulk</span>`;
    tr.innerHTML = `
      <td>${accountsCol}</td>
      <td class="nowrap">${sched.interval_hours}h</td>
      <td class="nowrap">${fmtRelative(sched.last_run_at)}</td>
      <td class="nowrap">${fmtRelative(sched.next_run_at)}</td>
      <td class="history-actions">
        <button class="btn btn-ghost btn-small" data-edit-schedule="${sched.id}">Edit</button>
        <button class="btn btn-ghost btn-small" data-run-now="${sched.id}">Run now</button>
        <button class="btn btn-ghost btn-small" data-delete-schedule="${sched.id}">Delete</button>
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
      btn.addEventListener("click", () => openScheduleEditModal(btn.closest("tr")._sched));
    });
  }

  // ---- Schedule edit modal (replaces the old in-row edit form, which
  // stretched across the table and no longer lined up with the column
  // headers once passwords were involved) ----
  const scheduleEditModal = document.getElementById("schedule-edit-modal");
  const scheduleEditLabel = document.getElementById("schedule-edit-label");
  const scheduleEditInterval = document.getElementById("schedule-edit-interval");
  const scheduleEditPasswordFields = document.getElementById("schedule-edit-password-fields");
  const scheduleEditBatchHint = document.getElementById("schedule-edit-batch-hint");
  const scheduleEditPassword1 = document.getElementById("schedule-edit-password1");
  const scheduleEditPassword2 = document.getElementById("schedule-edit-password2");
  const scheduleEditError = document.getElementById("schedule-edit-error");
  const scheduleEditSaveBtn = document.getElementById("schedule-edit-save");
  let scheduleEditId = null;

  function closeScheduleEditModal() {
    scheduleEditModal.hidden = true;
    scheduleEditId = null;
  }

  scheduleEditModal.querySelectorAll("[data-close]").forEach((el) =>
    el.addEventListener("click", closeScheduleEditModal)
  );

  function openScheduleEditModal(sched) {
    scheduleEditId = sched.id;
    // textContent, not innerHTML — no escaping needed, and nothing here
    // can be interpreted as markup even if a host/user contains "<" etc.
    scheduleEditLabel.textContent = sched.label + (sched.kind === "batch" ? " (bulk batch)" : "");
    scheduleEditInterval.value = sched.interval_hours;
    scheduleEditPassword1.value = "";
    scheduleEditPassword2.value = "";
    scheduleEditError.hidden = true;
    const isBatch = sched.kind === "batch";
    scheduleEditPasswordFields.hidden = isBatch;
    scheduleEditBatchHint.hidden = !isBatch;
    scheduleEditSaveBtn.disabled = false;
    scheduleEditSaveBtn.textContent = "Save";
    scheduleEditModal.hidden = false;
  }

  scheduleEditSaveBtn.addEventListener("click", () => {
    const interval = parseFloat(scheduleEditInterval.value);
    if (!interval || interval < 0.25) {
      scheduleEditError.hidden = false;
      scheduleEditError.className = "conn-test-status fail";
      scheduleEditError.textContent = "Minimum interval is 0.25h (15 minutes).";
      return;
    }
    scheduleEditSaveBtn.disabled = true;
    scheduleEditSaveBtn.textContent = "Saving…";
    fetch(`/api/schedules/${scheduleEditId}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...CSRF_HEADERS },
      body: JSON.stringify({
        interval_hours: interval,
        password1: scheduleEditPassword1.value,
        password2: scheduleEditPassword2.value,
      }),
    })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body.error || "Failed to save.");
        closeScheduleEditModal();
        loadSchedules();
      })
      .catch((err) => {
        scheduleEditError.hidden = false;
        scheduleEditError.className = "conn-test-status fail";
        scheduleEditError.textContent = err.message;
        scheduleEditSaveBtn.disabled = false;
        scheduleEditSaveBtn.textContent = "Save";
      });
  });

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
        <td class="nowrap">${started}</td>
        <td>${escapeHtml(displayName)}${batch.schedule_id ? ' <span class="bulk-tag">auto</span>' : ""}</td>
        <td class="nowrap">${batch.completed}/${batch.total}</td>
        <td class="nowrap">${batch.success}</td>
        <td class="nowrap">${batch.error}</td>
        <td class="nowrap"><span class="status-badge status-${badgeStatus}">${BATCH_STATUS_LABEL[batch.status] || batch.status}</span></td>
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
        <td class="nowrap">${started}${job.batch_id ? ' <span class="bulk-tag">bulk</span>' : ""}${job.schedule_id ? ' <span class="bulk-tag">auto</span>' : ""}</td>
        <td>${accountCell(job.user1, job.host1, job.authuser1 ? ' <span class="bulk-tag" title="Authenticated via master account ' + escapeHtml(job.authuser1) + '">master</span>' : "")}</td>
        <td>${accountCell(job.user2, job.host2, job.authuser2 ? ' <span class="bulk-tag" title="Authenticated via master account ' + escapeHtml(job.authuser2) + '">master</span>' : "")}</td>
        <td class="nowrap"><span class="status-badge status-${job.status}">${STATUS_LABEL[job.status] || job.status}</span></td>
        <td class="nowrap">${fmtOrDash(job.messages)}</td>
        <td class="nowrap">${fmtOrDash(job.errors)}</td>
        <td class="nowrap">${fmtDuration(job.duration_s)}</td>
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
  const modalPanel = modal.querySelector(".modal-panel-log");
  let modalSource = null;

  // The log modal sizes itself from the log's own content — wide progress
  // lines get a wider box, a short finished-job log gets a small one —
  // capped by the monitor size via the CSS on .modal-panel-log. Dragging
  // the resize handle overrides that: the chosen size is remembered
  // (localStorage) and reused for every log afterwards instead of being
  // recomputed from content, until the person resizes it again.
  const LOG_MODAL_SIZE_KEY = "imapsyncweb.logModalSize";
  let modalProgrammaticResize = false;
  let modalLastFitAt = 0;

  function readSavedModalSize() {
    try {
      const raw = localStorage.getItem(LOG_MODAL_SIZE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.w === "number" && typeof parsed.h === "number") return parsed;
    } catch (_) {
      // localStorage unavailable (private browsing, etc.) or a corrupt
      // value — fall back to content-based sizing below.
    }
    return null;
  }

  function saveModalSize(w, h) {
    try {
      localStorage.setItem(LOG_MODAL_SIZE_KEY, JSON.stringify({ w, h }));
    } catch (_) {
      // Nothing to fall back to but the CSS default — not worth surfacing.
    }
  }

  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (modalProgrammaticResize) {
        // This callback fired because of our own style.width/height
        // assignment below, not a manual drag — ignore it once.
        modalProgrammaticResize = false;
        return;
      }
      if (modal.hidden) return;
      const rect = modalPanel.getBoundingClientRect();
      saveModalSize(Math.round(rect.width), Math.round(rect.height));
    }).observe(modalPanel);
  }

  // Offscreen element used only to measure how wide a line of the log
  // text renders at the modal's actual font, so the initial size fits
  // the content instead of guessing.
  const modalMeasurer = document.createElement("pre");
  modalMeasurer.style.cssText =
    "position:absolute; visibility:hidden; top:-9999px; left:-9999px; margin:0; " +
    "white-space:pre; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; " +
    "font-size:13px; line-height:1.5;";
  document.body.appendChild(modalMeasurer);

  function fitLogModalToContent(text) {
    const saved = readSavedModalSize();
    if (saved) {
      modalProgrammaticResize = true;
      modalPanel.style.width = saved.w + "px";
      modalPanel.style.height = saved.h + "px";
      return;
    }

    modalMeasurer.textContent = text || "";
    const lineCount = (text || "").split("\n").length;
    const lineHeightPx = 13 * 1.5; // matches .modal-panel-log .console
    const chromeHeight = 90; // modal header + panel/console padding, approx
    const chromePlusScrollbarWidth = 64; // panel padding + a little slack

    const targetWidth = Math.min(
      Math.max(modalMeasurer.scrollWidth + chromePlusScrollbarWidth, 480),
      Math.round(window.innerWidth * 0.96),
      1400
    );
    const targetHeight = Math.min(
      Math.max(lineCount * lineHeightPx + chromeHeight, 320),
      Math.round(window.innerHeight * 0.9)
    );

    modalProgrammaticResize = true;
    modalPanel.style.width = targetWidth + "px";
    modalPanel.style.height = targetHeight + "px";
  }

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
    // Unhiding the modal (display: none -> flex) fires its own resize
    // notification before any content has loaded — mark it programmatic
    // so it isn't mistaken for a manual drag before fitLogModalToContent
    // below gets a chance to run.
    modalProgrammaticResize = true;
    modal.hidden = false;
    modalLog.textContent = "Loading…";

    const showSavedLog = () => {
      fetch(`/api/jobs/${jobId}/log`)
        .then((r) => r.text())
        .then((text) => {
          modalLog.textContent = text || "(empty log)";
          fitLogModalToContent(modalLog.textContent);
        });
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
          // Refit as the log grows, throttled so a fast-scrolling progress
          // stream doesn't recompute layout on every single line.
          const now = Date.now();
          if (now - modalLastFitAt > 500) {
            modalLastFitAt = now;
            fitLogModalToContent(modalLog.textContent);
          }
        });
        modalSource.addEventListener("done", (e) => {
          const stats = JSON.parse(e.data);
          stopModalStream();
          modalLog.textContent += `\n--- finished: ${STATUS_LABEL[stats.status] || stats.status} ---`;
          fitLogModalToContent(modalLog.textContent);
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
            <td>${accountCell(job.user1, job.host1)}</td>
            <td>${accountCell(job.user2, job.host2)}</td>
            <td class="nowrap"><span class="status-badge status-${job.status}">${STATUS_LABEL[job.status] || job.status}</span></td>
            <td class="nowrap">${fmtOrDash(job.messages)}</td>
            <td class="nowrap">${fmtOrDash(job.errors)}</td>
            <td class="nowrap">${fmtDuration(job.duration_s)}</td>
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
