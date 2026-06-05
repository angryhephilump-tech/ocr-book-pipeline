const drop = document.getElementById("drop");
const fileInput = document.getElementById("file");
const fileName = document.getElementById("file-name");
const form = document.getElementById("form");
const startBtn = document.getElementById("start");
const openFolderBtn = document.getElementById("open-folder");
const progressPanel = document.getElementById("progress-panel");
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
  const srcEl = document.getElementById("source-id");
  if (srcEl && Array.isArray(data.source_ids)) {
    const current = data.source_id || "ixtlilxochitl";
    srcEl.innerHTML = "";
    for (const id of data.source_ids) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = id.replace(/_/g, " ");
      if (id === current) opt.selected = true;
      srcEl.appendChild(opt);
    }
  }
}

async function loadKeyStatus() {
  try {
    const res = await fetch("/api/key-status");
    const data = await res.json();
    syncProcessingModeUi(data.processing_mode);
    syncSpotCheckUi(data.spot_check_enabled);
    syncSourceUi(data);
    if (data.saved) {
      showKeyForm(false);
      document.getElementById("key-hint").textContent = data.hint || "saved";
      if (banner) {
        banner.textContent = `Ready — ${data.model || "claude-opus-4-8"} · 2576px grayscale · batch 50% off`;
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

async function pollProgress() {
  try {
    const res = await fetch("/api/progress");
    const data = await res.json();
    if (data.work_dir) workDir = data.work_dir;
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
      startBtn.disabled = false;
      openFolderBtn.disabled = false;
      donePanel.classList.remove("hidden");
      outPath.textContent = workDir || "";
      progressMsg.textContent = "All done!";
    }
    if (data.phase === "error" || data.error) {
      clearInterval(pollTimer);
      startBtn.disabled = false;
      errEl.textContent = data.error || data.message;
      errEl.classList.remove("hidden");
    }
  } catch (_) {
    /* ignore */
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errEl.classList.add("hidden");
  donePanel.classList.add("hidden");
  progressPanel.classList.remove("hidden");
  startBtn.disabled = true;
  openFolderBtn.disabled = true;
  barFill.style.width = "2%";
  progressMsg.textContent = "Starting…";

  const fd = new FormData(form);
  if (!document.getElementById("remember-key").checked) fd.delete("remember_key");

  const pasted = (keyInput.value || "").trim();
  if (pasted) {
    await fetch("/api/save-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: pasted }),
    });
    await loadKeyStatus();
  }

  try {
    const res = await fetch("/api/start", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) {
      errEl.textContent = data.error || "Could not start.";
      errEl.classList.remove("hidden");
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
  } catch (err) {
    errEl.textContent = String(err);
    errEl.classList.remove("hidden");
    startBtn.disabled = false;
  }
});

openFolderBtn.addEventListener("click", async () => {
  await fetch("/api/open-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: workDir }),
  });
});
