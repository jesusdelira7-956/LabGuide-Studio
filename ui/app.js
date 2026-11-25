(() => {
  "use strict";

  const state = {
    preflight: null,
    officialGuide: null,
    sessions: { staging: [], saved: [], legacy: [] },
    currentSession: null,
    pollTimer: null,
    lastKnownMode: "idle",
    blurContext: {
      stepNum: null,
      target: "crop",
      img: null,
      boxes: [],
      drawing: false,
      startX: 0,
      startY: 0,
      currentBox: null,
    },
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function showToast(message, isError = false) {
    const region = $("#toast-region");
    const el = document.createElement("div");
    el.className = isError ? "toast error" : "toast";
    el.textContent = message;
    region.appendChild(el);
    setTimeout(() => el.remove(), 4200);
  }

  async function apiGet(url) {
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  async function apiPost(url, payload = {}) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  // Tabs (accessible ARIA + hidden="until-found" + beforematch)
  function selectTab(tabId, focusTab = false) {
    const tabs = $$('[role="tab"]');
    const panels = $$('[role="tabpanel"]');
    let targetTab = null;

    for (const tab of tabs) {
      const selected = tab.id === tabId;
      tab.ariaSelected = selected ? "true" : "false";
      tab.toggleAttribute("focusgroupstart", selected);
      if (selected) targetTab = tab;
    }

    for (const panel of panels) {
      const active = targetTab && panel.id === targetTab.dataset.panel;
      if (active) {
        panel.hidden = false;
        panel.tabIndex = 0;
      } else {
        panel.hidden = "until-found";
        panel.removeAttribute("tabindex");
      }
    }

    if (focusTab && targetTab) {
      targetTab.focus();
    }
  }

  function setupTabs() {
    const tabs = $$('[role="tab"]');
    for (const tab of tabs) {
      tab.addEventListener("click", () => selectTab(tab.id));
    }

    const tablist = $(".tablist");
    tablist.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      const idx = tabs.indexOf(document.activeElement);
      if (idx === -1) return;
      e.preventDefault();
      let next = idx;
      if (e.key === "ArrowRight") next = (idx + 1) % tabs.length;
      if (e.key === "ArrowLeft") next = (idx - 1 + tabs.length) % tabs.length;
      if (e.key === "Home") next = 0;
      if (e.key === "End") next = tabs.length - 1;
      selectTab(tabs[next].id, true);
    });

    document.addEventListener("beforematch", (e) => {
      const panel = e.target.closest('[role="tabpanel"]');
      if (panel && panel.getAttribute("aria-labelledby")) {
        selectTab(panel.getAttribute("aria-labelledby"));
      }
    });
  }

  // Dialog helpers + closedby="any" fallback
  function setupDialogs() {
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      const outside = (e, dialog) => {
        const r = dialog.getBoundingClientRect();
        return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      };
      for (const dialog of $$('dialog[closedby="any"]')) {
        let pressedOutside = false;
        dialog.addEventListener("pointerdown", (e) => {
          pressedOutside = outside(e, dialog);
        });
        dialog.addEventListener("pointerup", (e) => {
          if (pressedOutside && outside(e, dialog)) dialog.close();
        });
      }
    }

    const confirmDialog = $("#confirm-dialog");
    confirmDialog.addEventListener("close", () => {
      // Clear stale returnValue per dialog guidance
      const val = confirmDialog.returnValue;
      confirmDialog.returnValue = "";
      if (val === "confirm" && typeof state.onConfirmDelete === "function") {
        const fn = state.onConfirmDelete;
        state.onConfirmDelete = null;
        fn();
      } else {
        state.onConfirmDelete = null;
      }
    });

    $("#btn-close-blur").addEventListener("click", () => $("#blur-dialog").close());
    $("#btn-close-md").addEventListener("click", () => $("#md-dialog").close());
    $("#btn-close-avatar").addEventListener("click", () => $("#avatar-dialog").close());
    const closeAbout = $("#btn-close-about");
    if (closeAbout) closeAbout.addEventListener("click", () => $("#about-dialog").close());
  }

  function applyBackdropPreference(enabled) {
    document.body.classList.toggle("backdrop-minimal", !enabled);
    const btn = $("#btn-toggle-backdrop");
    if (btn) {
      btn.setAttribute("aria-pressed", String(enabled));
      btn.textContent = enabled ? "Lab Backdrop: On" : "Lab Backdrop: Off";
    }
  }

  async function openAboutDialog() {
    const dlg = $("#about-dialog");
    const body = $("#about-body");
    if (!dlg || !body) return;
    body.innerHTML = '<p class="hint">Loading release metadata...</p>';
    dlg.showModal();
    try {
      const info = await apiGet("/api/about");
      $("#about-dialog-title").textContent = info.app_name || "LabGuide Studio";
      $("#about-subtitle").textContent = `Version ${info.version} · ${info.copyright} · ${info.license}`;
      const ossRows = (info.third_party || [])
        .map(
          (row) =>
            `<tr><td>${escapeHtml(row.component)}</td><td>${escapeHtml(row.license)}</td><td>${escapeHtml(row.role)}</td></tr>`
        )
        .join("");
      body.innerHTML = `
        <p>${escapeHtml(info.tagline || "")}</p>
        <div class="meta-box">
          <strong>Privacy Guarantee:</strong> ${escapeHtml(info.privacy_guarantee || "")}
          <br /><span class="hint">Python ${escapeHtml(info.python_version || "")} &bull; ${escapeHtml(info.platform || "")} &bull; Port ${escapeHtml(String(info.port || 8897))}</span>
        </div>
        <div>
          <strong>Software License (${escapeHtml(info.license || "MIT License")})</strong>
          <pre class="about-license-pre" tabindex="0"><code>${escapeHtml(info.license_text || "")}</code></pre>
        </div>
        <div>
          <strong>Third-Party Open-Source Attributions</strong>
          <table class="about-oss-table">
            <thead><tr><th>Component</th><th>License</th><th>Role</th></tr></thead>
            <tbody>${ossRows}</tbody>
          </table>
        </div>
      `;
    } catch (err) {
      body.innerHTML = `<p class="hint">${escapeHtml(err.message)}</p>`;
    }
  }

  function askConfirm(title, message, onConfirm) {
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    state.onConfirmDelete = onConfirm;
    $("#confirm-dialog").showModal();
  }

  // Help Accordion Expand/Collapse All (Invoker Command + click fallback)
  function setupAccordionToggle() {
    const accordion = $("#help-accordion");
    const btn = accordion.querySelector('[command="--toggle-all"]');

    const toggleAll = () => {
      const expand = btn.getAttribute("aria-expanded") !== "true";
      for (const details of accordion.querySelectorAll(":scope > details")) {
        details.open = expand;
      }
      btn.setAttribute("aria-expanded", String(expand));
    };

    if ("commandForElement" in HTMLButtonElement.prototype) {
      document.addEventListener(
        "command",
        (e) => {
          if (e.command === "--toggle-all" && e.target === accordion) {
            toggleAll();
          }
        },
        { capture: true }
      );
    } else {
      btn.addEventListener("click", toggleAll);
    }

    document.addEventListener(
      "toggle",
      (e) => {
        if (!e.target.matches("#help-accordion > details")) return;
        const items = Array.from(accordion.querySelectorAll(":scope > details"));
        btn.setAttribute("aria-expanded", String(items.every((d) => d.open)));
      },
      { capture: true }
    );
  }

  // Preflight & Readiness
  async function loadPreflight() {
    const ollamaUrl = $("#rec-ollama-url").value.trim() || "http://localhost:11434";
    try {
      const data = await apiGet(`/api/preflight?ollama_url=${encodeURIComponent(ollamaUrl)}`);
      state.preflight = data;
      renderPreflight(data);
    } catch (err) {
      showToast(`System check failed: ${err.message}`, true);
    }
  }

  function renderPreflight(data) {
    const ollamaPill = $("#pill-ollama");
    if (data.ollama.online) {
      ollamaPill.textContent = `Ollama: Online (${data.ollama.models.length} models)`;
      ollamaPill.className = "pill pill-ok";
    } else {
      ollamaPill.textContent = "Ollama: Offline (UIA Fallback Active)";
      ollamaPill.className = "pill pill-warn";
    }

    const stagingCount = data.counts.staging;
    $("#pill-staging").textContent = `Drafts: ${stagingCount}`;
    $("#badge-saved-count").textContent = String(data.counts.saved);
    const reviewBadge = $("#badge-review-count");
    reviewBadge.textContent = String(stagingCount);
    reviewBadge.hidden = stagingCount === 0;

    // Populate model selector
    const modelSelect = $("#rec-model");
    const prevValue = modelSelect.value;
    const models = data.ollama.models;
    if (models.length > 0) {
      modelSelect.innerHTML = "";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.appendChild(document.createElement("selectedcontent"));
      modelSelect.appendChild(btn);

      for (const m of models) {
        const opt = document.createElement("option");
        opt.value = m.name;
        const tag = m.is_vision_recommended ? "Vision Recommended" : "Text/Other";
        opt.textContent = `${m.name} (${m.size_gb} GB — ${tag})`;
        if (m.name === (prevValue || data.ollama.recommended_default)) {
          opt.selected = true;
        }
        modelSelect.appendChild(opt);
      }
    }

    // Render Readiness Grid
    const grid = $("#readiness-grid");
    const depsOk = data.all_dependencies_ready;
    const missingDeps = Object.entries(data.dependencies)
      .filter(([, ok]) => !ok)
      .map(([name]) => name);

    const cards = [
      {
        ok: data.python.in_venv,
        title: "Python Virtual Environment",
        detail: `Python ${data.python.version} (${data.python.in_venv ? "Workspace .venv Active" : "Not in .venv"})`,
        sub: data.python.executable,
      },
      {
        ok: depsOk,
        title: "Capture, UIA & CPU OCR Packages",
        detail: depsOk ? "All 6 dependencies installed & ready" : `Missing: ${missingDeps.join(", ")}`,
        sub: "pynput, mss, Pillow, uiautomation, requests, rapidocr-onnxruntime",
      },
      {
        ok: data.platform.dpi_v2_supported,
        title: "Windows 11 Per-Monitor V2 DPI",
        detail: data.platform.dpi_v2_supported ? "Active (SetProcessDpiAwareness(2))" : "Non-Windows OS",
        sub: "Click coordinates match physical screen pixels.",
      },
      {
        ok: data.platform.is_admin,
        title: "Windows UIPI / Elevation Status",
        detail: data.platform.is_admin
          ? "Administrator (Can capture elevated Admin apps)"
          : "Standard User (Admin windows require elevated launch)",
        sub: "Standard mode works for browsers, IDE, Office, and Explorer.",
      },
      {
        ok: data.ollama.online,
        title: "Local Ollama Vision Server",
        detail: data.ollama.online
          ? `Connected at ${data.ollama.url} (${data.ollama.models.length} models)`
          : "Offline — deterministic UIA fallback remains ready",
        sub: `Recommended: ${data.ollama.recommended_default} (>= 1,200 MB GPU VRAM headroom)`,
      },
      {
        ok: true,
        title: "Staging & Saved Workflows Folders",
        detail: `${data.counts.staging} staging drafts | ${data.counts.saved} saved workflows`,
        sub: data.folders.workflows,
      },
    ];

    grid.innerHTML = "";
    for (const c of cards) {
      const div = document.createElement("div");
      div.className = `readiness-item ${c.ok ? "ok" : "warn"}`;
      div.innerHTML = `
        <strong>${escapeHtml(c.title)}</strong>
        <div>${escapeHtml(c.detail)}</div>
        <div class="hint">${escapeHtml(c.sub)}</div>
      `;
      grid.appendChild(div);
    }
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Recording Lifecycle
  async function startRecording() {
    try {
      const payload = {
        title: $("#rec-title").value.trim() || "Standard Operating Procedure",
        model: $("#rec-model").value || "gemma4:e4b",
        ollama_url: $("#rec-ollama-url").value.trim() || "http://localhost:11434",
        countdown_seconds: Number($("#rec-countdown").value || 3),
        auto_compile: $("#rec-post-action").value === "true",
        prompt_style: $("#rec-style").value || "concise",
        redact_pii: $("#rec-redact").checked,
        capture_shortcuts: $("#rec-shortcuts").checked,
        show_hud: $("#rec-hud").checked,
        exclude_self: $("#rec-exclude-self").checked,
        coalesce_typing: $("#rec-coalesce-typing").checked,
        keep_alive: $("#rec-keep-alive").checked ? "0" : "5m",
      };
      const status = await apiPost("/api/record/start", payload);
      applyRecorderStatus(status);
      showToast("Recording initiated!");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function togglePause() {
    try {
      const status = await apiPost("/api/record/pause");
      applyRecorderStatus(status);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function stopRecording() {
    try {
      $("#btn-stop-record").disabled = true;
      const result = await apiPost("/api/record/stop");
      applyRecorderStatus(result.status);
      await loadSessions();
      await loadPreflight();
      if (result.cancelled_countdown) {
        showToast("Countdown cancelled.");
        return;
      }
      if (result.session) {
        showToast("Draft captured! Review your steps before saving.");
        loadSessionIntoEditor(result.session.path);
        selectTab("tab-review");
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function applyRecorderStatus(st) {
    const pill = $("#pill-recorder");
    const banner = $("#live-banner");
    const btnStart = $("#btn-start-record");
    const btnPause = $("#btn-pause-record");
    const btnStop = $("#btn-stop-record");

    if (st.mode === "idle") {
      pill.textContent = "Recorder: Idle";
      pill.className = "pill pill-idle";
      btnStart.disabled = false;
      btnPause.disabled = true;
      btnPause.textContent = "Pause (Ctrl+Alt+P)";
      btnStop.disabled = true;
      banner.innerHTML =
        'Ready to record. Click <strong>Start Recording</strong>, switch to your target Windows app during the countdown, and click through your workflow.';
    } else if (st.mode === "countdown") {
      pill.textContent = `Starting in ${st.countdown_remaining}s`;
      pill.className = "pill pill-paused";
      btnStart.disabled = true;
      btnPause.disabled = true;
      btnStop.disabled = false;
      banner.innerHTML = `<strong>Countdown: ${st.countdown_remaining}s</strong> — Switch to your target Windows application now!`;
    } else if (st.mode === "recording") {
      pill.textContent = `Recording (${st.step_count} steps)`;
      pill.className = "pill pill-recording";
      btnStart.disabled = true;
      btnPause.disabled = false;
      btnPause.textContent = "Pause (Ctrl+Alt+P)";
      btnStop.disabled = false;
      banner.innerHTML = `<strong>Recording Active</strong> ("${escapeHtml(st.title)}") — ${st.step_count} step(s) captured. Press <kbd>Ctrl+Alt+S</kbd> when done.`;
    } else if (st.mode === "paused") {
      pill.textContent = `Paused (${st.step_count} steps)`;
      pill.className = "pill pill-paused";
      btnStart.disabled = true;
      btnPause.disabled = false;
      btnPause.textContent = "Resume (Ctrl+Alt+P)";
      btnStop.disabled = false;
      banner.innerHTML = "<strong>Recording Paused</strong> — Safe to type passwords or switch apps.";
    } else if (st.mode === "compiling") {
      pill.textContent = "Compiling Guide...";
      pill.className = "pill pill-compiling";
      btnStart.disabled = true;
      btnPause.disabled = true;
      btnStop.disabled = true;
      banner.innerHTML = "<strong>Finalizing draft &amp; generating outputs...</strong>";
    }

    const list = $("#live-steps-list");
    if (st.recent_steps && st.recent_steps.length > 0) {
      list.innerHTML = "";
      for (const step of st.recent_steps) {
        const li = document.createElement("li");
        const label = step.control_name || step.control_type || step.event_type;
        li.textContent = `Step ${step.step_num}: ${label} in "${step.window_title}"`;
        list.appendChild(li);
      }
    } else if (st.mode === "idle") {
      list.innerHTML = '<li class="empty-note">No active capture yet.</li>';
    }

    // Detect transition from hotkey Ctrl+Alt+S stop
    if (
      (state.lastKnownMode === "recording" || state.lastKnownMode === "paused" || state.lastKnownMode === "compiling") &&
      st.mode === "idle" &&
      st.last_completed_session
    ) {
      loadSessions().then(() => loadPreflight());
      if (!state.currentSession || state.currentSession.path !== st.last_completed_session) {
        loadSessionIntoEditor(st.last_completed_session);
        selectTab("tab-review");
        showToast("Recording stopped via hotkey! Ready for review.");
      }
    }
    state.lastKnownMode = st.mode;
  }

  async function pollStatus() {
    try {
      const st = await apiGet("/api/status");
      applyRecorderStatus(st);
    } catch {
      // ignore transient poll errors
    }
  }

  // Sessions & Library
  async function loadSessions() {
    try {
      const data = await apiGet("/api/sessions");
      state.sessions = data;
      populateSessionDropdown(data);
      renderLibrary(data);
    } catch (err) {
      showToast(`Failed to load sessions: ${err.message}`, true);
    }
  }

  function populateSessionDropdown(data) {
    const sel = $("#review-session-select");
    const vidSel = $("#vid-session-select");
    const currentPath = state.currentSession ? state.currentSession.path : sel.value;

    const fillSelect = (targetSel, selectedVal) => {
      if (!targetSel) return;
      targetSel.innerHTML = "";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.appendChild(document.createElement("selectedcontent"));
      targetSel.appendChild(btn);

      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "-- Choose a staging draft or workflow --";
      targetSel.appendChild(placeholder);

      const groups = [
        { label: "Pending Review (Staging)", items: data.staging },
        { label: "Saved Workflows", items: data.saved },
        { label: "Legacy / CLI Sessions", items: data.legacy },
      ];
      for (const grp of groups) {
        for (const item of grp.items) {
          const opt = document.createElement("option");
          opt.value = item.path;
          opt.textContent = `[${grp.label}] ${item.title} (${item.step_count} steps)`;
          if (item.path === selectedVal) opt.selected = true;
          targetSel.appendChild(opt);
        }
      }
    };

    fillSelect(sel, currentPath);
    fillSelect(vidSel, (vidSel && vidSel.value) || currentPath);
  }

  function renderLibrary(data) {
    const query = ($("#library-search-input").value || "").trim().toLowerCase();
    const filterItems = (items) =>
      items.filter(
        (i) =>
          !query ||
          i.title.toLowerCase().includes(query) ||
          i.id.toLowerCase().includes(query)
      );

    renderWorkflowList("#list-staging", filterItems(data.staging), "No pending drafts in staging.");
    renderWorkflowList("#list-saved", filterItems(data.saved), "No saved workflows yet. Review a draft and click Save Workflow to Library.");
    renderWorkflowList("#list-legacy", filterItems(data.legacy), "No legacy CLI sessions in sessions/.");
  }

  function renderWorkflowList(containerSel, items, emptyMessage) {
    const container = $(containerSel);
    container.innerHTML = "";
    if (!items.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = emptyMessage;
      container.appendChild(p);
      return;
    }

    for (const item of items) {
      const card = document.createElement("article");
      card.className = "card workflow-card";
      const htmlUrl = `/api/sessions/html?path=${encodeURIComponent(item.path)}`;
      const docxUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=docx`;
      const pdfUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=pdf_print`;
      const replayUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=replay_py`;
      const jsonUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=json`;
      const scormUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=scorm`;
      const bundleUrl = `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=offline_zip`;
      card.innerHTML = `
        <div>
          <h4>${escapeHtml(item.title)}</h4>
          <div class="hint">${escapeHtml(item.id)} &bull; ${item.step_count} steps</div>
        </div>
        <div class="action-row">
          <button type="button" class="btn btn-primary btn-sm" data-action="review">Review / Edit</button>
          ${
            item.has_html
              ? `<a class="btn btn-outline btn-sm" href="${htmlUrl}" target="_blank" rel="noopener">HTML</a>`
              : ""
          }
          <a class="btn btn-outline btn-sm" href="${docxUrl}">.docx</a>
          <a class="btn btn-outline btn-sm" href="${pdfUrl}" target="_blank" rel="noopener">PDF</a>
          <a class="btn btn-outline btn-sm" href="${replayUrl}">replay.py</a>
          <a class="btn btn-outline btn-sm" href="${jsonUrl}">JSON</a>
          <a class="btn btn-accent btn-sm" href="${scormUrl}">SCORM .zip</a>
          <a class="btn btn-outline btn-sm" href="${bundleUrl}">Bundle .zip</a>
          <button type="button" class="btn btn-outline btn-sm" data-action="copy-md">Copy MD</button>
          <button type="button" class="btn btn-ghost btn-sm" data-action="open-folder">Folder</button>
          <button type="button" class="btn btn-danger btn-sm" data-action="delete">Delete</button>
        </div>
      `;

      card.querySelector('[data-action="review"]').addEventListener("click", () => {
        loadSessionIntoEditor(item.path);
        selectTab("tab-review");
      });
      card.querySelector('[data-action="copy-md"]').addEventListener("click", async () => {
        try {
          const res = await fetch(
            `/api/sessions/export?path=${encodeURIComponent(item.path)}&format=md_b64&inline=1`,
            { cache: "no-store" }
          );
          const text = await res.text();
          await navigator.clipboard.writeText(text);
          showToast("Self-contained Markdown copied to clipboard.");
        } catch (err) {
          showToast(err.message, true);
        }
      });
      card.querySelector('[data-action="open-folder"]').addEventListener("click", () => {
        openFolderOrFile(item.path);
      });
      card.querySelector('[data-action="delete"]').addEventListener("click", () => {
        askConfirm(
          `Delete "${item.title}"?`,
          `This permanently deletes "${item.path}" and all screenshots.`,
          async () => {
            await apiPost("/api/sessions/discard", { path: item.path });
            if (state.currentSession && state.currentSession.path === item.path) {
              state.currentSession = null;
              renderEditor(null);
            }
            await loadSessions();
            await loadPreflight();
            showToast("Workflow deleted.");
          }
        );
      });

      container.appendChild(card);
    }
  }

  // Review & Step Editor
  async function loadSessionIntoEditor(sessionPath) {
    if (!sessionPath) {
      state.currentSession = null;
      renderEditor(null);
      return;
    }
    try {
      const detail = await apiGet(`/api/sessions/detail?path=${encodeURIComponent(sessionPath)}`);
      state.currentSession = detail;
      $("#review-session-select").value = detail.path;
      renderEditor(detail);
    } catch (err) {
      showToast(`Could not load session: ${err.message}`, true);
    }
  }

  function renderEditor(detail) {
    const emptyState = $("#editor-empty-state");
    const titleBar = $("#editor-title-bar");
    const isoQuizBox = $("#iso-quiz-box");
    const decisionBanner = $("#save-decision-banner");
    const stepsContainer = $("#editor-steps-container");
    const btnAiAll = $("#btn-ai-compile-all");
    const btnMd = $("#btn-preview-md");
    const btnMdB64 = $("#btn-copy-md-b64");
    const linkHtml = $("#link-preview-html");
    const btnDocx = $("#btn-export-docx");
    const btnPdf = $("#btn-export-pdf");
    const btnJson = $("#btn-export-json");
    const btnReplay = $("#btn-export-replay");
    const btnSrt = $("#btn-export-srt");
    const btnOfflineZip = $("#btn-export-offline-zip");
    const btnScorm = $("#btn-export-scorm");
    const btnFolder = $("#btn-open-session-folder");

    stepsContainer.innerHTML = "";

    if (!detail) {
      emptyState.hidden = false;
      titleBar.hidden = true;
      isoQuizBox.hidden = true;
      decisionBanner.hidden = true;
      btnAiAll.disabled = true;
      btnMd.disabled = true;
      btnMdB64.disabled = true;
      btnDocx.disabled = true;
      btnPdf.disabled = true;
      btnJson.disabled = true;
      btnReplay.disabled = true;
      btnSrt.disabled = true;
      btnOfflineZip.disabled = true;
      btnScorm.disabled = true;
      btnFolder.disabled = true;
      linkHtml.classList.add("is-disabled");
      linkHtml.setAttribute("aria-disabled", "true");
      linkHtml.href = "#";
      return;
    }

    emptyState.hidden = true;
    titleBar.hidden = false;
    isoQuizBox.hidden = false;
    decisionBanner.hidden = false;

    const meta = detail.metadata || {};
    $("#iso-doc-id").value = meta.doc_id || "";
    $("#iso-version").value = meta.version || "1.0";
    $("#iso-owner").value = meta.owner || "";
    $("#iso-date").value = meta.effective_date || "";
    $("#iso-prereq").value = meta.prerequisites || "";
    $("#iso-warnings").value = meta.safety_warnings || "";

    const isStaging = detail.category === "staging";
    $("#decision-heading").textContent = isStaging
      ? "Pending Review: Save or Discard This Draft?"
      : `Saved Workflow (${detail.category === "saved" ? "Library" : "Sessions"})`;
    $("#decision-copy").innerHTML = isStaging
      ? "Review, prune, edit, or blur steps below. When satisfied, click <strong>Save Workflow to Library</strong> to move it to your permanent workflows folder — or click <strong>Discard Draft</strong> if it isn't needed."
      : `Stored at <code>${escapeHtml(detail.path)}</code>. Edits below immediately update <code>guide.md</code> and <code>guide.html</code>. You can also copy/save to another destination preset or delete it.`;
    $("#btn-save-workflow").textContent = isStaging ? "Save Workflow to Library" : "Save Copy to Destination";
    $("#btn-discard-session").textContent = isStaging ? "Discard Draft" : "Delete Workflow";

    $("#edit-guide-title").value = detail.title || "";
    const hasSteps = detail.steps.length > 0;
    btnAiAll.disabled = !hasSteps;
    btnMd.disabled = !detail.has_md;
    btnMdB64.disabled = !hasSteps;
    btnDocx.disabled = !hasSteps;
    btnPdf.disabled = !hasSteps;
    btnJson.disabled = !hasSteps;
    btnReplay.disabled = !hasSteps;
    btnSrt.disabled = !hasSteps;
    btnOfflineZip.disabled = !hasSteps;
    btnScorm.disabled = !hasSteps;
    btnFolder.disabled = false;

    if (detail.has_html) {
      linkHtml.href = `/api/sessions/html?path=${encodeURIComponent(detail.path)}`;
      linkHtml.classList.remove("is-disabled");
      linkHtml.removeAttribute("aria-disabled");
    } else {
      linkHtml.href = "#";
      linkHtml.classList.add("is-disabled");
      linkHtml.setAttribute("aria-disabled", "true");
    }

    const total = detail.steps.length;
    if (total === 0) {
      const p = document.createElement("div");
      p.className = "card";
      p.innerHTML = "<p>No steps remain in this draft. You can safely discard it.</p>";
      stepsContainer.appendChild(p);
      return;
    }

    const cacheBust = Date.now();
    for (const step of detail.steps) {
      const card = document.createElement("article");
      card.className = "card step-card";

      const cropSrc = `/api/sessions/asset?session=${encodeURIComponent(detail.path)}&file=${encodeURIComponent(step.crop_image)}&v=${cacheBust}`;
      const fullSrc = `/api/sessions/asset?session=${encodeURIComponent(detail.path)}&file=${encodeURIComponent(step.full_image)}&v=${cacheBust}`;

      card.innerHTML = `
        <div class="step-media">
          <img src="${cropSrc}" alt="Step ${step.step_num} screenshot" data-view="crop" />
          <div class="step-media-actions">
            <button type="button" class="btn btn-outline btn-sm" data-act="toggle-img">Show Full Screen</button>
            <button type="button" class="btn btn-outline btn-sm" data-act="blur">Manual Blur / Redact</button>
            <button type="button" class="btn btn-outline btn-sm" data-act="replace-img">Replace Screenshot</button>
            ${
              step.has_raw
                ? '<button type="button" class="btn btn-ghost btn-sm" data-act="reset-img">Reset from Raw</button>'
                : ""
            }
          </div>
          <div class="step-meta-pill">
            ${escapeHtml(step.event_type)} &bull; ${escapeHtml(step.window_title || "Active Window")} ${
              step.control_name ? `&bull; ${escapeHtml(step.control_name)}` : ""
            }
          </div>
        </div>
        <div class="step-fields">
          <div class="step-card-header">
            <h3>Step ${step.step_num} of ${total}</h3>
            <div class="step-card-actions">
              <button type="button" class="btn btn-ghost btn-sm" data-act="up" ${step.step_num === 1 ? "disabled" : ""}>Move Up</button>
              <button type="button" class="btn btn-ghost btn-sm" data-act="down" ${step.step_num === total ? "disabled" : ""}>Move Down</button>
              <button type="button" class="btn btn-danger btn-sm" data-act="delete">Delete</button>
            </div>
          </div>
          <div class="field">
            <label for="step-title-${step.step_num}">Step Title</label>
            <input type="text" id="step-title-${step.step_num}" value="${escapeHtml(step.step_title || "")}" />
          </div>
          <div class="field">
            <label for="step-instr-${step.step_num}">Instruction</label>
            <textarea id="step-instr-${step.step_num}">${escapeHtml(step.step_instruction || "")}</textarea>
          </div>
          <div class="field">
            <label for="step-note-${step.step_num}">Tip / Note (Optional)</label>
            <textarea id="step-note-${step.step_num}">${escapeHtml(step.step_note || "")}</textarea>
          </div>
          <div class="action-row">
            <button type="button" class="btn btn-primary btn-sm" data-act="save-step">Save Step</button>
            <button type="button" class="btn btn-accent btn-sm" data-act="ai-step">AI Rewrite Step</button>
            <button type="button" class="btn btn-outline btn-sm" data-act="rerender-clip">Re-Render Video Clip</button>
            <button type="button" class="btn btn-ghost btn-sm" data-act="add-pause">+ [pause 1s]</button>
          </div>
        </div>
      `;

      const imgEl = card.querySelector("img");
      const toggleBtn = card.querySelector('[data-act="toggle-img"]');
      toggleBtn.addEventListener("click", () => {
        const isCrop = imgEl.dataset.view === "crop";
        imgEl.src = isCrop ? fullSrc : cropSrc;
        imgEl.dataset.view = isCrop ? "full" : "crop";
        toggleBtn.textContent = isCrop ? "Show Cropped Target" : "Show Full Screen";
      });

      card.querySelector('[data-act="blur"]').addEventListener("click", () => {
        const target = imgEl.dataset.view === "full" ? "full" : "crop";
        openBlurStudio(step.step_num, target, target === "full" ? fullSrc : cropSrc);
      });

      card.querySelector('[data-act="replace-img"]').addEventListener("click", () => {
        state.pendingReplaceStep = step.step_num;
        $("#step-replace-file-input").value = "";
        $("#step-replace-file-input").click();
      });

      card.querySelector('[data-act="add-pause"]').addEventListener("click", () => {
        const ta = card.querySelector(`#step-instr-${step.step_num}`);
        ta.value = `${ta.value.trim()} [pause 1s]`.trim();
      });

      card.querySelector('[data-act="rerender-clip"]').addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Re-rendering...";
        try {
          const presenterMode = ($('input[name="vid-presenter-mode"]:checked') || {}).value || "none";
          const res = await apiPost("/api/sessions/step/rerender", {
            path: detail.path,
            step_num: step.step_num,
            voice: $("#vid-voice").value || "af_heart",
            speed: Number($("#vid-speed").value || 1.0),
            presenter_mode: presenterMode,
            avatar_id: $("#vid-avatar").value || "aria_architect",
            consent_confirmed: $("#vid-consent").checked,
            zoom_to_click: $("#vid-zoom").checked,
            burn_captions: $("#vid-captions").checked,
          });
          state.currentSession = res.detail;
          renderEditor(res.detail);
          showToast(`Step ${step.step_num} video clip re-rendered (${res.duration_sec}s total).`);
        } catch (err) {
          showToast(err.message, true);
        } finally {
          btn.disabled = false;
          btn.textContent = "Re-Render Video Clip";
        }
      });

      const resetBtn = card.querySelector('[data-act="reset-img"]');
      if (resetBtn) {
        resetBtn.addEventListener("click", async () => {
          try {
            const updated = await apiPost("/api/sessions/step/reset-image", {
              path: detail.path,
              step_num: step.step_num,
              redact_pii: $("#rec-redact").checked,
            });
            state.currentSession = updated;
            renderEditor(updated);
            showToast(`Step ${step.step_num} image restored from raw capture.`);
          } catch (err) {
            showToast(err.message, true);
          }
        });
      }

      card.querySelector('[data-act="save-step"]').addEventListener("click", async () => {
        try {
          const updated = await apiPost("/api/sessions/step/update", {
            path: detail.path,
            step_num: step.step_num,
            step_title: card.querySelector(`#step-title-${step.step_num}`).value,
            step_instruction: card.querySelector(`#step-instr-${step.step_num}`).value,
            step_note: card.querySelector(`#step-note-${step.step_num}`).value,
          });
          state.currentSession = updated;
          renderEditor(updated);
          showToast(`Saved Step ${step.step_num}.`);
        } catch (err) {
          showToast(err.message, true);
        }
      });

      card.querySelector('[data-act="ai-step"]').addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Generating...";
        try {
          const updated = await apiPost("/api/sessions/step/recompile", {
            path: detail.path,
            step_num: step.step_num,
            model: $("#rec-model").value || "gemma4:e4b",
            ollama_url: $("#rec-ollama-url").value.trim() || "http://localhost:11434",
            prompt_style: $("#rec-style").value || "concise",
            keep_alive: $("#rec-keep-alive").checked ? "0" : "5m",
          });
          state.currentSession = updated;
          renderEditor(updated);
          showToast(`Step ${step.step_num} rewritten locally.`);
        } catch (err) {
          showToast(err.message, true);
          btn.disabled = false;
          btn.textContent = "AI Rewrite Step";
        }
      });

      card.querySelector('[data-act="up"]').addEventListener("click", async () => {
        const updated = await apiPost("/api/sessions/step/move", {
          path: detail.path,
          step_num: step.step_num,
          direction: "up",
        });
        state.currentSession = updated;
        renderEditor(updated);
      });

      card.querySelector('[data-act="down"]').addEventListener("click", async () => {
        const updated = await apiPost("/api/sessions/step/move", {
          path: detail.path,
          step_num: step.step_num,
          direction: "down",
        });
        state.currentSession = updated;
        renderEditor(updated);
      });

      card.querySelector('[data-act="delete"]').addEventListener("click", async () => {
        const updated = await apiPost("/api/sessions/step/delete", {
          path: detail.path,
          step_num: step.step_num,
        });
        state.currentSession = updated;
        renderEditor(updated);
        await loadSessions();
        showToast(`Deleted Step ${step.step_num}.`);
      });

      stepsContainer.appendChild(card);
    }
  }

  // Manual Blur Canvas Studio
  function openBlurStudio(stepNum, target, imgSrc) {
    const canvas = $("#blur-canvas");
    const ctx = canvas.getContext("2d");
    const img = new Image();
    state.blurContext = {
      stepNum,
      target,
      img,
      boxes: [],
      drawing: false,
      startX: 0,
      startY: 0,
      currentBox: null,
    };
    $("#blur-box-count").textContent = "0 blur regions drawn";

    img.onload = () => {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      redrawBlurCanvas();
      $("#blur-dialog").showModal();
    };
    img.src = imgSrc;
  }

  function redrawBlurCanvas() {
    const { img, boxes, currentBox } = state.blurContext;
    const canvas = $("#blur-canvas");
    const ctx = canvas.getContext("2d");
    if (!img) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);

    const all = currentBox ? [...boxes, currentBox] : boxes;
    const isBlackout = $("#blur-mode").value === "blackout";
    ctx.fillStyle = isBlackout ? "rgba(9, 13, 22, 0.92)" : "rgba(239, 68, 68, 0.32)";
    ctx.strokeStyle = "#ef4444";
    ctx.lineWidth = Math.max(2, canvas.width / 350);
    for (const b of all) {
      const px = b.x * canvas.width;
      const py = b.y * canvas.height;
      const pw = b.w * canvas.width;
      const ph = b.h * canvas.height;
      ctx.fillRect(px, py, pw, ph);
      ctx.strokeRect(px, py, pw, ph);
    }
    $("#blur-box-count").textContent = `${boxes.length} redaction region(s) queued (${isBlackout ? "Blackout" : "Blur"})`;
  }

  function setupBlurCanvasEvents() {
    const canvas = $("#blur-canvas");
    const getNorm = (e) => {
      const r = canvas.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
        y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
      };
    };

    canvas.addEventListener("pointerdown", (e) => {
      canvas.setPointerCapture(e.pointerId);
      const pt = getNorm(e);
      state.blurContext.drawing = true;
      state.blurContext.startX = pt.x;
      state.blurContext.startY = pt.y;
      state.blurContext.currentBox = { x: pt.x, y: pt.y, w: 0, h: 0 };
    });

    canvas.addEventListener("pointermove", (e) => {
      if (!state.blurContext.drawing) return;
      const pt = getNorm(e);
      const x = Math.min(state.blurContext.startX, pt.x);
      const y = Math.min(state.blurContext.startY, pt.y);
      const w = Math.abs(pt.x - state.blurContext.startX);
      const h = Math.abs(pt.y - state.blurContext.startY);
      state.blurContext.currentBox = { x, y, w, h };
      redrawBlurCanvas();
    });

    const finishDrag = () => {
      if (!state.blurContext.drawing) return;
      state.blurContext.drawing = false;
      const box = state.blurContext.currentBox;
      state.blurContext.currentBox = null;
      if (box && box.w > 0.01 && box.h > 0.01) {
        state.blurContext.boxes.push(box);
      }
      redrawBlurCanvas();
    };
    canvas.addEventListener("pointerup", finishDrag);
    canvas.addEventListener("pointercancel", finishDrag);

    $("#blur-mode").addEventListener("change", redrawBlurCanvas);

    $("#btn-blur-clear").addEventListener("click", () => {
      state.blurContext.boxes = [];
      redrawBlurCanvas();
    });

    $("#btn-blur-apply").addEventListener("click", async () => {
      if (!state.currentSession || state.blurContext.boxes.length === 0) {
        $("#blur-dialog").close();
        return;
      }
      try {
        const mode = $("#blur-mode").value || "blur";
        const updated = await apiPost("/api/sessions/step/blur", {
          path: state.currentSession.path,
          step_num: state.blurContext.stepNum,
          target: state.blurContext.target,
          boxes: state.blurContext.boxes,
          mode,
        });
        state.currentSession = updated;
        renderEditor(updated);
        $("#blur-dialog").close();
        showToast(`${mode === "blackout" ? "Solid blackout" : "Manual blur"} applied and guide.html updated.`);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  }

  async function openFolderOrFile(targetPath, reveal = false) {
    try {
      await apiPost("/api/open-path", { path: targetPath, reveal });
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function wireControls() {
    $("#btn-refresh-preflight").addEventListener("click", () => {
      loadPreflight();
      loadOfficialGuide();
      loadSessions();
      showToast("System readiness refreshed.");
    });

    $("#btn-help-inspect-editor").addEventListener("click", () => {
      if (!state.officialGuide || !state.officialGuide.available) return;
      loadSessionIntoEditor(state.officialGuide.path);
      selectTab("tab-review");
    });

    for (const btn of $$("[data-open-folder]")) {
      btn.addEventListener("click", () => {
        const key = btn.dataset.openFolder;
        if (!state.preflight || !state.preflight.folders[key]) return;
        openFolderOrFile(state.preflight.folders[key]);
      });
    }

    $("#btn-start-record").addEventListener("click", startRecording);
    $("#btn-pause-record").addEventListener("click", togglePause);
    $("#btn-stop-record").addEventListener("click", stopRecording);

    $("#review-session-select").addEventListener("change", (e) => {
      loadSessionIntoEditor(e.target.value);
    });

    $("#btn-save-title").addEventListener("click", async () => {
      if (!state.currentSession) return;
      try {
        const updated = await apiPost("/api/sessions/update-title", {
          path: state.currentSession.path,
          title: $("#edit-guide-title").value,
        });
        state.currentSession = updated;
        renderEditor(updated);
        await loadSessions();
        showToast("Guide title updated.");
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-ai-compile-all").addEventListener("click", async (e) => {
      if (!state.currentSession) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Compiling with Ollama...";
      try {
        const updated = await apiPost("/api/sessions/compile", {
          path: state.currentSession.path,
          model: $("#rec-model").value || "gemma4:e4b",
          ollama_url: $("#rec-ollama-url").value.trim() || "http://localhost:11434",
          prompt_style: $("#rec-style").value || "concise",
          keep_alive: $("#rec-keep-alive").checked ? "0" : "5m",
          use_ollama: true,
        });
        state.currentSession = updated;
        renderEditor(updated);
        showToast("All steps described via local Ollama!");
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Run AI Descriptions on All Steps";
      }
    });

    $("#btn-preview-md").addEventListener("click", () => {
      if (!state.currentSession) return;
      $("#md-preview-code code").textContent = state.currentSession.guide_md || "";
      $("#md-dialog").showModal();
    });

    $("#btn-copy-md").addEventListener("click", async () => {
      if (!state.currentSession) return;
      await navigator.clipboard.writeText(state.currentSession.guide_md || "");
      showToast("Markdown copied to clipboard.");
    });

    $("#btn-copy-md-b64").addEventListener("click", async () => {
      if (!state.currentSession) return;
      try {
        const res = await fetch(
          `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=md_b64&inline=1`,
          { cache: "no-store" }
        );
        const text = await res.text();
        await navigator.clipboard.writeText(text);
        showToast("Self-contained Base64 Markdown copied to clipboard.");
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-export-docx").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=docx`;
    });

    $("#btn-export-pdf").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.open(
        `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=pdf_print`,
        "_blank",
        "noopener"
      );
    });

    $("#btn-export-json").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=json`;
    });

    $("#btn-export-replay").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=replay_py`;
    });

    $("#btn-export-srt").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=srt`;
    });

    $("#btn-export-offline-zip").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=offline_zip`;
    });

    $("#btn-export-scorm").addEventListener("click", () => {
      if (!state.currentSession) return;
      window.location.href = `/api/sessions/export?path=${encodeURIComponent(state.currentSession.path)}&format=scorm`;
    });

    $("#step-replace-file-input").addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file || !state.currentSession || !state.pendingReplaceStep) return;
      try {
        const b64 = await readFileBase64(file);
        const updated = await apiPost("/api/sessions/step/replace-image", {
          path: state.currentSession.path,
          step_num: state.pendingReplaceStep,
          image_b64: b64,
        });
        state.currentSession = updated;
        renderEditor(updated);
        showToast(`Step ${state.pendingReplaceStep} screenshot replaced.`);
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-save-iso").addEventListener("click", async () => {
      if (!state.currentSession) return;
      try {
        const updated = await apiPost("/api/sessions/iso/update", {
          path: state.currentSession.path,
          doc_id: $("#iso-doc-id").value,
          version: $("#iso-version").value,
          owner: $("#iso-owner").value,
          effective_date: $("#iso-date").value,
          prerequisites: $("#iso-prereq").value,
          safety_warnings: $("#iso-warnings").value,
        });
        state.currentSession = updated;
        renderEditor(updated);
        showToast("ISO compliance header saved.");
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-gen-quiz").addEventListener("click", async (e) => {
      if (!state.currentSession) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Generating Quiz...";
      try {
        const updated = await apiPost("/api/sessions/quiz/generate", {
          path: state.currentSession.path,
          use_ollama: true,
          model: $("#rec-model").value || "gemma4:e4b",
          ollama_url: $("#rec-ollama-url").value.trim() || "http://localhost:11434",
        });
        state.currentSession = updated;
        renderEditor(updated);
        showToast("3-question Knowledge Check added to guide.html!");
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Generate 3-Question Quiz (Ollama)";
      }
    });

    // Electronic & Video Studio controls
    for (const radio of $$('input[name="vid-presenter-mode"]')) {
      radio.addEventListener("change", () => {
        $("#pip-options-box").hidden = radio.value !== "pip" || !radio.checked;
      });
    }

    $("#vid-avatar").addEventListener("change", updateAvatarThumb);
    $("#btn-open-avatar-dialog").addEventListener("click", () => $("#avatar-dialog").showModal());

    $("#btn-upload-avatar").addEventListener("click", async () => {
      const file = $("#avatar-file-input").files && $("#avatar-file-input").files[0];
      if (!file) {
        showToast("Please select a PNG or JPG portrait image.", true);
        return;
      }
      try {
        const b64 = await readFileBase64(file);
        const created = await apiPost("/api/avatars/upload", {
          name: $("#avatar-name").value.trim(),
          role_title: $("#avatar-role").value.trim(),
          image_b64: b64,
          ext: file.name.slice(file.name.lastIndexOf(".")),
          consent_confirmed: $("#avatar-consent-check").checked,
        });
        await loadAvatars(created.id);
        $("#avatar-dialog").close();
        showToast(`Avatar "${created.name}" registered.`);
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-import-sop").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Building Visual Slides...";
      try {
        const file = $("#imp-file").files && $("#imp-file").files[0];
        const file_b64 = file ? await readFileBase64(file) : "";
        const detail = await apiPost("/api/sop/import", {
          title: $("#imp-title").value.trim() || "Electronic SOP",
          doc_id: $("#imp-doc-id").value.trim(),
          owner: $("#imp-owner").value.trim(),
          raw_text: $("#imp-raw-text").value,
          file_name: file ? file.name : "",
          file_b64,
          use_ollama: $("#imp-parser-mode").value === "true",
          model: $("#rec-model").value || "gemma4:e4b",
          ollama_url: $("#rec-ollama-url").value.trim() || "http://localhost:11434",
        });
        await loadSessions();
        await loadPreflight();
        $("#vid-session-select").value = detail.path;
        loadSessionIntoEditor(detail.path);
        selectTab("tab-review");
        showToast(`Imported ${detail.steps.length} visual SOP slides into Staging!`);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Create Visual Slide SOP";
      }
    });

    $("#btn-render-video").addEventListener("click", async (e) => {
      const sessionPath = $("#vid-session-select").value;
      if (!sessionPath) {
        showToast("Select a target session or workflow first.", true);
        return;
      }
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "Rendering CPU Video...";
      try {
        const presenterMode = ($('input[name="vid-presenter-mode"]:checked') || {}).value || "none";
        const res = await apiPost("/api/sessions/render-video", {
          path: sessionPath,
          voice: $("#vid-voice").value || "af_heart",
          speed: Number($("#vid-speed").value || 1.0),
          presenter_mode: presenterMode,
          avatar_id: $("#vid-avatar").value || "aria_architect",
          consent_confirmed: $("#vid-consent").checked,
          zoom_to_click: $("#vid-zoom").checked,
          burn_captions: $("#vid-captions").checked,
        });
        const videoLink = $("#link-rendered-video");
        videoLink.href = `/api/sessions/asset?session=${encodeURIComponent(sessionPath)}&file=walkthrough.mp4&v=${Date.now()}`;
        videoLink.classList.remove("is-disabled");
        videoLink.removeAttribute("aria-disabled");
        await loadSessions();
        if (state.currentSession && state.currentSession.path === sessionPath) {
          state.currentSession = res.detail;
          renderEditor(res.detail);
        }
        showToast(`Rendered ${res.duration_sec}s MP4 (${res.tts_engine} voice) + SRT!`);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Render Video SOP (MP4 + SRT)";
      }
    });

    $("#btn-open-md-os").addEventListener("click", () => {
      if (!state.currentSession) return;
      openFolderOrFile(`${state.currentSession.path}\\guide.md`);
    });

    $("#btn-open-session-folder").addEventListener("click", () => {
      if (!state.currentSession) return;
      openFolderOrFile(state.currentSession.path);
    });

    $("#btn-save-workflow").addEventListener("click", async () => {
      if (!state.currentSession) return;
      try {
        const res = await apiPost("/api/sessions/save", {
          path: state.currentSession.path,
          preset: $("#save-preset").value || "workflows",
          custom_name: $("#save-custom-name").value.trim(),
        });
        $("#save-custom-name").value = "";
        state.currentSession = res.detail;
        await loadSessions();
        await loadPreflight();
        renderEditor(res.detail);
        showToast(`Workflow saved to: ${res.saved_path}`);
      } catch (err) {
        showToast(err.message, true);
      }
    });

    $("#btn-discard-session").addEventListener("click", () => {
      if (!state.currentSession) return;
      const target = state.currentSession;
      askConfirm(
        `Discard "${target.title}"?`,
        `This permanently deletes "${target.path}" and all its screenshots.`,
        async () => {
          await apiPost("/api/sessions/discard", { path: target.path });
          state.currentSession = null;
          renderEditor(null);
          await loadSessions();
          await loadPreflight();
          showToast("Session deleted.");
        }
      );
    });

    $("#library-search-input").addEventListener("input", () => renderLibrary(state.sessions));
    $("#library-search-form").addEventListener("submit", (e) => e.preventDefault());

    for (const btn of $$("[data-copy-target]")) {
      btn.addEventListener("click", async () => {
        const target = document.getElementById(btn.dataset.copyTarget);
        if (!target) return;
        await navigator.clipboard.writeText(target.textContent || "");
        showToast("Command copied to clipboard.");
      });
    }
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const str = String(reader.result || "");
        const idx = str.indexOf(",");
        resolve(idx >= 0 ? str.slice(idx + 1) : str);
      };
      reader.onerror = () => reject(reader.error || new Error("File read failed."));
      reader.readAsDataURL(file);
    });
  }

  async function loadVoices() {
    try {
      const data = await apiGet("/api/tts/voices");
      $("#tts-engine-label").textContent = data.kokoro_ready ? "Kokoro-82M CPU Ready" : "Windows SAPI Fallback";
      const sel = $("#vid-voice");
      if (data.voices && data.voices.length > 0) {
        sel.innerHTML = "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.appendChild(document.createElement("selectedcontent"));
        sel.appendChild(btn);
        for (const v of data.voices) {
          const opt = document.createElement("option");
          opt.value = v.id;
          opt.textContent = v.label;
          if (v.id === "af_heart") opt.selected = true;
          sel.appendChild(opt);
        }
      }
    } catch {
      // ignore
    }
  }

  function updateAvatarThumb() {
    const sel = $("#vid-avatar");
    const chosen = (state.avatars || []).find((a) => a.id === sel.value) || (state.avatars || [])[0];
    if (chosen && chosen.image_url) {
      $("#vid-avatar-preview").src = chosen.image_url;
    }
  }

  async function loadAvatars(selectId = "") {
    try {
      const data = await apiGet("/api/avatars");
      state.avatars = data.avatars || [];
      const sel = $("#vid-avatar");
      const prev = selectId || sel.value;
      if (state.avatars.length > 0) {
        sel.innerHTML = "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.appendChild(document.createElement("selectedcontent"));
        sel.appendChild(btn);
        for (const a of state.avatars) {
          const opt = document.createElement("option");
          opt.value = a.id;
          opt.textContent = `${a.name} — ${a.role}`;
          if (a.id === prev) opt.selected = true;
          sel.appendChild(opt);
        }
        updateAvatarThumb();
      }
    } catch {
      // ignore
    }
  }

  async function loadOfficialGuide() {
    try {
      const info = await apiGet("/api/official-guide");
      state.officialGuide = info;
      const setLinkState = (el, url, enabled) => {
        if (!el) return;
        if (enabled && url) {
          el.href = url;
          el.classList.remove("is-disabled");
          el.removeAttribute("aria-disabled");
        } else {
          el.href = "#";
          el.classList.add("is-disabled");
          el.setAttribute("aria-disabled", "true");
        }
      };
      setLinkState($("#link-official-guide"), info.html_url, info.available);
      setLinkState($("#btn-help-open-html"), info.html_url, info.available);
      setLinkState($("#btn-help-open-video"), info.video_url, info.available && info.has_video);
      setLinkState($("#btn-help-open-docx"), info.docx_url, info.available);
      $("#btn-help-inspect-editor").disabled = !info.available;
    } catch {
      // ignore if endpoint unavailable
    }
  }

  async function applyUrlDeepLinks() {
    const params = new URLSearchParams(window.location.search);
    const tabMap = {
      capture: "tab-capture",
      review: "tab-review",
      library: "tab-library",
      readiness: "tab-readiness",
      help: "tab-help",
    };
    const tabKey = params.get("tab");
    if (tabKey && tabMap[tabKey]) {
      selectTab(tabMap[tabKey]);
    }
    if (params.get("advanced") === "1") {
      const adv = $(".advanced-box");
      if (adv) adv.open = true;
    }
    const sessionParam = params.get("session");
    if (sessionParam) {
      await loadSessionIntoEditor(sessionParam);
      const blurStep = params.get("blur_step");
      if (blurStep && state.currentSession) {
        const stepObj = state.currentSession.steps.find(
          (s) => Number(s.step_num) === Number(blurStep)
        );
        if (stepObj) {
          const blurMode = params.get("blur_mode");
          if (blurMode) $("#blur-mode").value = blurMode;
          const imgSrc = `/api/sessions/asset?session=${encodeURIComponent(state.currentSession.path)}&file=${encodeURIComponent(stepObj.crop_image)}&v=${Date.now()}`;
          openBlurStudio(stepObj.step_num, "crop", imgSrc);
          if (params.get("demo_boxes") === "1") {
            state.blurContext.boxes = [
              { x: 0.06, y: 0.25, w: 0.56, h: 0.13 },
              { x: 0.06, y: 0.43, w: 0.62, h: 0.13 },
            ];
            setTimeout(() => redrawBlurCanvas(), 250);
          }
        }
      }
    }
    const sopStep = Number(params.get("sop_step") || 0);
    if (sopStep > 0) {
      setTimeout(() => renderSopCallouts(sopStep), 320);
    }
  }

  function placeCallout(container, targetEl, num, text, pos = "top") {
    if (!targetEl) return;
    const r = targetEl.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;

    let originX = -window.scrollX;
    let originY = -window.scrollY;
    let maxW = window.innerWidth;
    if (container !== document.body) {
      const cr = container.getBoundingClientRect();
      originX = cr.left;
      originY = cr.top;
      maxW = cr.width;
    }

    const relLeft = r.left - originX;
    const relTop = r.top - originY;
    const relRight = r.right - originX;
    const relBottom = r.bottom - originY;

    const box = document.createElement("div");
    box.className = "sop-callout-box";
    box.style.left = `${Math.max(4, relLeft - 5)}px`;
    box.style.top = `${Math.max(4, relTop - 5)}px`;
    box.style.width = `${r.width + 10}px`;
    box.style.height = `${r.height + 10}px`;
    container.appendChild(box);

    const tag = document.createElement("div");
    tag.className = "sop-callout-tag";
    tag.innerHTML = `<span class="sop-callout-num">${num}</span><span>${escapeHtml(text)}</span>`;
    container.appendChild(tag);
    const tr = tag.getBoundingClientRect();

    let left = relLeft;
    let top = pos === "bottom" ? relBottom + 10 : relTop - tr.height - 10;
    if (pos === "right") {
      left = relRight + 10;
      top = relTop + Math.max(0, (r.height - tr.height) / 2);
    }
    if (top < 6) top = relBottom + 10;
    if (left + tr.width > maxW - 8) {
      left = Math.max(8, maxW - tr.width - 10);
    }
    tag.style.left = `${left}px`;
    tag.style.top = `${top}px`;
  }

  function renderSopCallouts(stepNum) {
    if (stepNum === 3) {
      const adv = $(".advanced-box");
      if (adv) {
        adv.open = true;
        adv.scrollIntoView({ behavior: "instant", block: "center" });
      }
    } else if (stepNum === 5) {
      const ban = $("#save-decision-banner");
      if (ban) ban.scrollIntoView({ behavior: "instant", block: "center" });
    } else if (stepNum === 7) {
      const aiBtn = $("#btn-ai-compile-all");
      if (aiBtn) aiBtn.scrollIntoView({ behavior: "instant", block: "center" });
    } else if (stepNum === 8) {
      const saved = $("#list-saved");
      if (saved) saved.scrollIntoView({ behavior: "instant", block: "center" });
    }

    setTimeout(() => {
      if (stepNum === 1) {
        placeCallout(document.body, $(".status-pills"), 1, "Live Ollama & Draft Pills", "bottom");
        placeCallout(document.body, $("#readiness-grid"), 2, "Pre-Flight System Checks", "top");
      } else if (stepNum === 2) {
        placeCallout(document.body, $("#rec-title"), 1, "Auto-Names if Left Default", "top");
        placeCallout(document.body, $("#rec-post-action"), 2, "0 MB VRAM Instant Draft", "bottom");
        placeCallout(document.body, $("#btn-start-record"), 3, "Start Recording", "right");
      } else if (stepNum === 3) {
        placeCallout(document.body, $('label[for="rec-hud"]'), 1, "Floating Mini-HUD + Self-Click Filter", "right");
        placeCallout(document.body, $('label[for="rec-coalesce-typing"]'), 2, "Masks Passwords as ••••••••", "right");
        placeCallout(document.body, $('label[for="rec-keep-alive"]'), 3, "Frees VRAM Immediately", "right");
      } else if (stepNum === 4) {
        const hud = document.createElement("div");
        hud.className = "sop-mini-hud-demo";
        hud.innerHTML =
          '<span class="sop-hud-rec">● REC</span><span>3 steps</span>' +
          '<span class="sop-hud-btn">Pause</span><span class="sop-hud-btn sop-hud-stop">Stop</span>';
        document.body.appendChild(hud);
        placeCallout(document.body, hud, 1, "Always-on-Top Mini-HUD (Hidden from Capture)", "bottom");
        placeCallout(document.body, $(".hotkey-grid"), 2, "Global Hotkeys: Pause & Stop", "top");
      } else if (stepNum === 5) {
        placeCallout(document.body, $("#save-decision-banner"), 1, "Staging Gate: Save or Discard", "top");
        const stepActions = $(".step-card-actions");
        if (stepActions) placeCallout(document.body, stepActions, 2, "Reorder or Delete Misclicks", "top");
      } else if (stepNum === 6) {
        const dlg = $("#blur-dialog");
        placeCallout(dlg, $("#blur-mode"), 1, "Choose Blur or Solid Blackout", "top");
        placeCallout(dlg, $("#blur-canvas"), 2, "Drag Boxes Over Secrets", "bottom");
        placeCallout(dlg, $("#btn-blur-apply"), 3, "Updates PNG & guide.html", "top");
      } else if (stepNum === 7) {
        placeCallout(document.body, $("#btn-ai-compile-all"), 1, "Run Ollama VLM Across All Steps", "top");
        const aiOne = $('[data-act="ai-step"]');
        if (aiOne) placeCallout(document.body, aiOne, 2, "Or Rewrite One Step", "right");
      } else if (stepNum === 8) {
        const card = $("#list-saved .workflow-card");
        placeCallout(document.body, card || $("#list-saved"), 1, "1-Click Exports: HTML, .docx, PDF, replay.py", "top");
      }
    }, 150);
  }

  async function init() {
    setupTabs();
    setupDialogs();
    setupAccordionToggle();
    setupBlurCanvasEvents();
    wireControls();

    let backdropOn = true;
    try {
      backdropOn = localStorage.getItem("labguide_backdrop") !== "off";
    } catch {
      backdropOn = true;
    }
    applyBackdropPreference(backdropOn);

    const toggleBackdropBtn = $("#btn-toggle-backdrop");
    if (toggleBackdropBtn) {
      toggleBackdropBtn.addEventListener("click", () => {
        const next = document.body.classList.contains("backdrop-minimal");
        applyBackdropPreference(next);
        try {
          localStorage.setItem("labguide_backdrop", next ? "on" : "off");
        } catch {
          /* ignore storage quota */
        }
      });
    }
    const openAboutBtn = $("#btn-open-about");
    if (openAboutBtn) openAboutBtn.addEventListener("click", openAboutDialog);
    const footerAboutBtn = $("#btn-footer-about");
    if (footerAboutBtn) footerAboutBtn.addEventListener("click", openAboutDialog);

    await Promise.all([
      loadPreflight(),
      loadOfficialGuide(),
      loadVoices(),
      loadAvatars(),
      loadSessions(),
      pollStatus(),
    ]);
    await applyUrlDeepLinks();
    state.pollTimer = setInterval(pollStatus, 1000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
