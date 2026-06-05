const drop = document.getElementById("drop");
const fileInput = document.getElementById("file");
const fileName = document.getElementById("file-name");
const form = document.getElementById("form");
const startBtn = document.getElementById("start");
const openFolderBtn = document.getElementById("open-folder");
const progressPanel = document.getElementById("progress-panel");
const confirmPanel = document.getElementById("confirm-panel");
const profileLines = document.getElementById("profile-lines");
const profileEdit = document.getElementById("profile-edit");
const confirmYes = document.getElementById("confirm-yes");
const confirmEditBtn = document.getElementById("confirm-edit");
const confirmCancel = document.getElementById("confirm-cancel");
const progressMsg = document.getElementById("progress-msg");
const progressDetail = document.getElementById("progress-detail");
const barFill = document.getElementById("bar-fill");
const donePanel = document.getElementById("done-panel");
const outPath = document.getElementById("out-path");
const errEl = document.getElementById("err");
const banner = document.getElementById("api-banner");
const keySaved = document.getElementById("key-saved");
const keyEntry = document.getElementById("key-entry");
const changeKeyBtn = document.getElementById("change-key");
const keyInput = document.getElementById("api-key");

let workDir = null;
let pollTimer = null;
let editMode = false;

const CONFIRM_YES_DEFAULT = "Yes, proceed";
const CONFIRM_YES_EDIT = "Save & proceed";
const CONFIRM_EDIT_DEFAULT = "Edit overrides";

async function parseApiResponse(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch (_) {
    const hint =
      res.status === 404
        ? "Server is out of date or wrong port — close this tab and reopen Launch PDF Transcribe.bat."
        : `Server returned an error page instead of JSON (HTTP ${res.status}). Restart the app.`;
    throw new Error(hint);
  }
}

function resetConfirmUi() {
  editMode = false;
  profileEdit.classList.add("hidden");
  confirmYes.textContent = CONFIRM_YES_DEFAULT;
  confirmEditBtn.textContent = CONFIRM_EDIT_DEFAULT;
  confirmEditBtn.disabled = false;
}

function setFormLocked(locked) {
  form.querySelectorAll("input, select, button, textarea").forEach((el) => {
    if (el === startBtn || el === openFolderBtn) return;
    el.disabled = locked;
  });
  startBtn.disabled = locked;
}

function showKeyForm(show) {
  if (show) {
    keyEntry.classList.remove("hidden");
    keySaved.classList.add("hidden");
    changeKeyBtn.classList.add("hidden");
  } else {
    keyEntry.classList.add("hidden");
    keySaved.classList.remove("hidden");
    changeKeyBtn.classList.remove("hidden");
  }
}

function syncProcessingModeUi(savedMode) {
  const mode = savedMode || "auto";
  document.querySelectorAll('input[name="processing_mode"]').forEach((el) => {
    el.checked = el.value === mode;
  });
}

function syncSpotCheckUi(enabled) {
  const el = document.getElementById("spot-check");
  if (el) el.checked = enabled !== false;
}

function syncSourceUi(data) {
  const langEl = document.getElementById("language");
  if (langEl && data.language) langEl.value = data.language;
  const scriptEl = document.getElementById("script");
  if (scriptEl && data.script) scriptEl.value = data.script;
}

async function loadKeyStatus() {
  try {
    const res = await fetch("/api/key-status");
    const data = await parseApiResponse(res);
    syncProcessingModeUi(data.processing_mode);
    syncSpotCheckUi(data.spot_check_enabled);
    syncSourceUi(data);
    if (data.saved) {
      showKeyForm(false);
      document.getElementById("key-hint").textContent = data.hint || "saved";
      if (banner) {
        banner.textContent = `Ready — ${data.model || "claude-opus-4-8"} · v3 auto-detect · batch 50% off`;
        banner.className = "api-banner ok";
      }
    } else {
      showKeyForm(true);
      if (banner) {
        banner.textContent = "First time? Paste your Claude key below (sk-ant-…)";
        banner.className = "api-banner warn";
      }
    }
  } catch (_) {
    if (banner) {
      banner.textContent = "Close old window, reopen Launch PDF Transcribe.bat";
      banner.className = "api-banner warn";
    }
    showKeyForm(true);
  }
}

changeKeyBtn.addEventListener("click", () => {
  showKeyForm(true);
  keyInput.focus();
});

loadKeyStatus();

function setFile(file) {
  if (!file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
  fileName.textContent = file.name;
}

document.getElementById("pick").addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.click();
});

drop.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) fileName.textContent = fileInput.files[0].name;
});

["dragenter", "dragover"].forEach((ev) => {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("dragover");
  });
});

["dragleave", "drop"].forEach((ev) => {
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("dragover");
  });
});

drop.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) setFile(f);
});

function formatEta(sec) {
  if (sec == null || sec < 0) return "";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `~${m}m ${s}s left` : `~${s}s left`;
}

function updateBar(data) {
  if (data.phase === "done") {
    barFill.style.width = "100%";
    return;
  }
  if (data.batch_total > 0) {
    const done = data.batch_done || 0;
    const pct = (done / data.batch_total) * 100;
    barFill.style.width = `${Math.min(100, Math.max(2, pct))}%`;
    return;
  }
  const total = data.total_pages || 0;
  const run = data.current_run || 0;
  const page = data.page || 0;
  if (!total || !run) {
    barFill.style.width = "5%";
    return;
  }
  const runIndex = Math.max(0, run - 1);
  const doneInPriorRuns = runIndex * total;
  const current = Math.min(page, total);
  const denom = total * 2;
  const pct = denom > 0 ? ((doneInPriorRuns + current) / denom) * 100 : 0;
  barFill.style.width = `${Math.min(100, pct)}%`;
}

function showConfirmPanel(lines, fromSaved) {
  resetConfirmUi();
  profileLines.innerHTML = "";
  for (const line of lines) {
    const li = document.createElement("li");
    li.textContent = line;
    profileLines.appendChild(li);
  }
  const hint = document.getElementById("confirm-hint");
  if (hint) {
    hint.textContent = fromSaved
      ? "Loaded saved profile for this source. Proceed?"
      : "Proceed with these settings?";
  }
  confirmPanel.classList.remove("hidden");
  progressPanel.classList.add("hidden");
  setFormLocked(true);
  startBtn.disabled = true;
}

function hideConfirmPanel() {
  confirmPanel.classList.add("hidden");
  progressPanel.classList.remove("hidden");
}

async function fetchProfile() {
  try {
    const res = await fetch("/api/profile");
    return await parseApiResponse(res);
  } catch (_) {
    return { ready: false };
  }
}

async function pollProgress() {
  try {
    const res = await fetch("/api/progress");
    const data = await parseApiResponse(res);
    if (data.work_dir) workDir = data.work_dir;

    if (data.awaiting_confirm || data.phase === "awaiting_confirm") {
      const prof = await fetchProfile();
      if (prof.ready && prof.needs_confirmation) {
        showConfirmPanel(prof.lines || [], prof.from_saved);
        return;
      }
    }

    if (data.phase !== "awaiting_confirm") {
      hideConfirmPanel();
    }

    progressMsg.textContent = data.message || data.phase || "Working…";
    const detail = [];
    if (data.processing_mode) detail.push(data.processing_mode);
    if (data.batch_total > 0) {
      detail.push(`Batch ${data.batch_done || 0} / ${data.batch_total} pages`);
    } else {
      if (data.current_run) detail.push(`Pass ${data.current_run} of 2`);
      if (data.page && data.total_pages) detail.push(`Page ${data.page} / ${data.total_pages}`);
    }
    const eta = formatEta(data.eta_seconds);
    if (eta) detail.push(eta);
    progressDetail.textContent = detail.join(" · ");
    updateBar(data);

    if (data.phase === "done") {
      clearInterval(pollTimer);
      setFormLocked(false);
      startBtn.disabled = false;
      openFolderBtn.disabled = false;
      donePanel.classList.remove("hidden");
      outPath.textContent = workDir || "";
      progressMsg.textContent = "All done!";
    }
    if (data.phase === "error" || data.error) {
      clearInterval(pollTimer);
      setFormLocked(false);
      startBtn.disabled = false;
      errEl.textContent = data.error || data.message;
      errEl.classList.remove("hidden");
    }
  } catch (_) {
    /* ignore */
  }
}

async function startPrepare() {
  resetConfirmUi();
  errEl.classList.add("hidden");
  donePanel.classList.add("hidden");
  confirmPanel.classList.add("hidden");
  progressPanel.classList.remove("hidden");
  barFill.style.width = "2%";
  progressMsg.textContent = "Uploading and analyzing sample pages…";

  const pdfFile = fileInput.files[0];
  if (!pdfFile) {
    progressPanel.classList.add("hidden");
    errEl.textContent = "Choose a PDF file first.";
    errEl.classList.remove("hidden");
    return;
  }

  // Build FormData before locking — disabled inputs are omitted from FormData.
  const fd = new FormData(form);
  fd.set("pdf", pdfFile);
  const sourceName = (document.getElementById("source-name")?.value || "").trim();
  if (!sourceName) {
    progressPanel.classList.add("hidden");
    errEl.textContent = "Name this source (e.g. anales_de_tlatelolco).";
    errEl.classList.remove("hidden");
    return;
  }
  fd.set("source_name", sourceName);
  if (!document.getElementById("remember-key").checked) fd.delete("remember_key");

  setFormLocked(true);
  startBtn.disabled = true;
  openFolderBtn.disabled = true;

  const pasted = (keyInput.value || "").trim();
  if (pasted) {
    await fetch("/api/save-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: pasted }),
    });
    await loadKeyStatus();
  }

  const res = await fetch("/api/prepare", { method: "POST", body: fd });
  const data = await parseApiResponse(res);
  if (!data.ok) {
    errEl.textContent = data.error || "Could not start.";
    errEl.classList.remove("hidden");
    setFormLocked(false);
    startBtn.disabled = false;
    if ((data.error || "").includes("key")) showKeyForm(true);
    return;
  }
  workDir = data.work_dir;
  openFolderBtn.disabled = false;
  progressMsg.textContent = `${data.message} — working…`;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollProgress, 1200);
  pollProgress();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await startPrepare();
  } catch (err) {
    errEl.textContent = String(err);
    errEl.classList.remove("hidden");
    startBtn.disabled = false;
  }
});

async function sendConfirm(action, overrides) {
  startBtn.disabled = true;
  errEl.classList.add("hidden");
  hideConfirmPanel();
  progressMsg.textContent = "Starting transcription…";

  const body = { action, work_dir: workDir };
  if (overrides) body.overrides = overrides;

  const res = await fetch("/api/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await parseApiResponse(res);
  if (!data.ok) {
    errEl.textContent = data.error || "Could not confirm.";
    errEl.classList.remove("hidden");
    startBtn.disabled = false;
    return;
  }
  if (data.cancelled) {
    resetConfirmUi();
    progressPanel.classList.add("hidden");
    setFormLocked(false);
    startBtn.disabled = false;
    return;
  }
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollProgress, 1200);
  pollProgress();
}

confirmYes.addEventListener("click", () => {
  const overrides = {};
  if (editMode) {
    const lang = document.getElementById("edit-language").value;
    const script = document.getElementById("edit-script").value;
    if (lang) overrides.language = lang;
    if (script) overrides.script = script;
    sendConfirm("edit", overrides);
  } else {
    sendConfirm("yes");
  }
});

confirmEditBtn.addEventListener("click", () => {
  editMode = !editMode;
  profileEdit.classList.toggle("hidden", !editMode);
  confirmYes.textContent = editMode ? CONFIRM_YES_EDIT : CONFIRM_YES_DEFAULT;
  confirmEditBtn.textContent = editMode ? "Hide overrides" : CONFIRM_EDIT_DEFAULT;
});

confirmCancel.addEventListener("click", () => {
  if (pollTimer) clearInterval(pollTimer);
  sendConfirm("cancel");
  resetConfirmUi();
  confirmPanel.classList.add("hidden");
  progressPanel.classList.add("hidden");
  setFormLocked(false);
  startBtn.disabled = false;
});

openFolderBtn.addEventListener("click", async () => {
  await fetch("/api/open-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: workDir }),
  });
});
