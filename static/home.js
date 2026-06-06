(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const browseBtn = document.getElementById("browse-btn");
  const fileList = document.getElementById("file-list");
  const btnClear = document.getElementById("btn-clear");
  const btnTranscribe = document.getElementById("btn-transcribe");
  const bookTitle = document.getElementById("book-title");
  const pageStart = document.getElementById("page-start");
  const pageEnd = document.getElementById("page-end");
  const progressPanel = document.getElementById("progress-panel");
  const progressPct = document.getElementById("progress-pct");
  const progressTitle = document.getElementById("progress-title");
  const progressDetail = document.getElementById("progress-detail");
  const progressPass = document.getElementById("progress-pass");
  const progressStale = document.getElementById("progress-stale");
  const ringFill = document.getElementById("ring-fill");
  const toast = document.getElementById("toast");
  const creditsPill = document.getElementById("credits-pill");
  const creditsWarning = document.getElementById("credits-warning");

  const langPrimary = document.getElementById("lang-primary");
  const langSecondary = document.getElementById("lang-secondary");
  const langPrimarySearch = document.getElementById("lang-primary-search");
  const langSecondarySearch = document.getElementById("lang-secondary-search");
  const langIndigenous = document.getElementById("lang-indigenous");
  const pdfSplitRanges = document.getElementById("pdf-split-ranges");
  const pdfMergeFiles = document.getElementById("pdf-merge-files");
  const btnPdfSplit = document.getElementById("btn-pdf-split");
  const btnPdfMerge = document.getElementById("btn-pdf-merge");

  const CIRC = 327;
  let pollTimer = null;
  let allLanguages = [];
  let currentUploadsCount = 0;
  let currentCredits = 0;

  const ACCEPT_EXT = new Set([
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".pdf", ".jfif", ".heic", ".heif",
  ]);

  function filterUploadFiles(fileList) {
    const accepted = [];
    const rejected = [];
    for (const f of fileList) {
      const name = (f.name || "").toLowerCase();
      const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
      if (ACCEPT_EXT.has(ext)) accepted.push(f);
      else rejected.push(f.name || "unknown file");
    }
    return { accepted, rejected };
  }

  ["dragenter", "dragover", "dragleave", "drop"].forEach((ev) => {
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
  });

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    toast.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.remove("show"), 3200);
  }

  async function safeJsonFetch(url, options) {
    const res = await fetch(url, options);
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) {
      const err = data?.error || data?.detail || `Request failed (${res.status})`;
      throw new Error(err);
    }
    return data || {};
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  let buyMoreUrl = "https://gumroad.com/";

  function renderCredits(credits) {
    const remaining = Number(credits?.remaining_credits ?? 0);
    const total = Number(credits?.total_credits ?? 0);
    currentCredits = remaining;
    creditsPill.textContent = `Credits: ${remaining}${total ? ` / ${total}` : ""}`;
    if (remaining === 0) {
      creditsWarning.innerHTML = `No credits remaining. <a href="${buyMoreUrl}" target="_blank" rel="noreferrer">Buy more credits</a> before starting a new job.`;
      creditsWarning.classList.remove("hidden");
      btnTranscribe.disabled = true;
      return;
    }
    if (remaining < 20) {
      creditsWarning.textContent = `Low credits warning: ${remaining} page${remaining === 1 ? "" : "s"} left.`;
      creditsWarning.classList.remove("hidden");
    } else {
      creditsWarning.textContent = "";
      creditsWarning.classList.add("hidden");
    }
    updateStartButtonState();
  }

  function languagePayload() {
    return {
      primary_language: langPrimary.value || "spa",
      secondary_language: langSecondary.value || null,
      indigenous_minority_mode: langIndigenous.checked,
    };
  }

  function filterLangList(query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return allLanguages;
    return allLanguages.filter(
      (l) =>
        l.name.toLowerCase().includes(q) ||
        l.code.toLowerCase().includes(q)
    );
  }

  function fillSelect(selectEl, items, selectedCode, includeNone) {
    const prev = selectedCode || selectEl.value;
    selectEl.innerHTML = "";
    if (includeNone) {
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "— None —";
      selectEl.appendChild(none);
    }
    for (const lang of items) {
      const opt = document.createElement("option");
      opt.value = lang.code;
      const tag = lang.installed ? "" : lang.bundled ? " (bundled)" : " (download on first use)";
      opt.textContent = `${lang.name} (${lang.code})${tag}`;
      selectEl.appendChild(opt);
    }
    if (prev && [...selectEl.options].some((o) => o.value === prev)) {
      selectEl.value = prev;
    } else if (!includeNone && selectEl.options.length) {
      selectEl.value = selectEl.options[0].value;
    }
  }

  function refreshLanguageSelects() {
    const pItems = filterLangList(langPrimarySearch.value);
    const sItems = filterLangList(langSecondarySearch.value);
    fillSelect(langPrimary, pItems, langPrimary.value, false);
    fillSelect(langSecondary, sItems, langSecondary.value, true);
  }

  async function loadLanguages() {
    const res = await fetch("/api/languages");
    const data = await res.json();
    allLanguages = data.languages || [];
    refreshLanguageSelects();
    const statusRes = await fetch("/api/status");
    const status = await statusRes.json();
    if (status.languages) {
      applySavedLanguages(status.languages);
    }
  }

  function applySavedLanguages(cfg) {
    if (cfg.primary_language) langPrimary.value = cfg.primary_language;
    if (cfg.secondary_language) langSecondary.value = cfg.secondary_language;
    else langSecondary.value = "";
    langIndigenous.checked = !!cfg.indigenous_minority_mode;
    const primary = allLanguages.find((l) => l.code === cfg.primary_language);
    const secondary = allLanguages.find((l) => l.code === cfg.secondary_language);
    if (primary) langPrimarySearch.value = primary.name;
    if (secondary) langSecondarySearch.value = secondary.name;
  }

  langPrimarySearch.addEventListener("input", refreshLanguageSelects);
  langSecondarySearch.addEventListener("input", refreshLanguageSelects);

  langPrimary.addEventListener("change", () => {
    const lang = allLanguages.find((l) => l.code === langPrimary.value);
    if (lang) langPrimarySearch.value = lang.name;
  });

  langSecondary.addEventListener("change", () => {
    const lang = allLanguages.find((l) => l.code === langSecondary.value);
    langSecondarySearch.value = lang ? lang.name : "";
  });

  function renderFiles(uploads) {
    currentUploadsCount = uploads.length;
    if (!uploads.length) {
      fileList.classList.add("hidden");
      fileList.innerHTML = "";
      btnClear.disabled = true;
      btnTranscribe.disabled = true;
      return;
    }
    fileList.classList.remove("hidden");
    fileList.innerHTML = uploads
      .map((f) => `<div class="file-item"><span>${f.name}</span><span>${formatSize(f.size)}</span></div>`)
      .join("");
    btnClear.disabled = false;
    updateStartButtonState();
  }

  function updateStartButtonState() {
    const hasFiles = currentUploadsCount > 0;
    const hasCredits = currentCredits > 0;
    btnTranscribe.disabled = !(hasFiles && hasCredits);
  }

  async function refreshStatus() {
    let data;
    try {
      data = await safeJsonFetch("/api/status");
    } catch (err) {
      showToast(`Status error: ${err.message}`);
      return;
    }
    renderFiles(data.uploads || []);
    if (data.buy_more_url) buyMoreUrl = data.buy_more_url;
    renderCredits(data.credits || {});
    if (data.languages) applySavedLanguages(data.languages);

    if (data.has_manifest && data.job.status === "running") {
      // stay on home while OCR runs
    } else if (data.has_manifest && data.job.status === "done") {
      window.location.href = "/review";
      return;
    }

    const job = data.job;
    if (job.status === "running" || job.status === "done" || job.status === "error") {
      progressPanel.classList.remove("hidden");
      btnTranscribe.disabled = true;
      btnClear.disabled = true;
    }

    if (job.status === "running") {
      const pct = job.total ? Math.round((job.current / job.total) * 100) : 5;
      progressPct.textContent = `${pct}%`;
      ringFill.style.strokeDashoffset = String(CIRC - (CIRC * pct) / 100);
      progressTitle.textContent = job.message || "Transcribing…";
      const fileLine = job.filename ? `File: ${job.filename}` : "";
      const stageLine = job.stage ? `Step: ${job.stage.replace(/_/g, " ")}` : "";
      const etaLine = Number.isFinite(job.eta_seconds) && job.eta_seconds > 0
        ? `Estimated ${Math.ceil(job.eta_seconds / 60)} min remaining`
        : "";
      progressDetail.textContent = [fileLine, stageLine, etaLine].filter(Boolean).join(" · ") || "3 DeepSeek runs per page";

      if (job.pass_label && (job.stage === "pass_start" || job.stage === "pass_done")) {
        const mins = job.seconds_on_pass ? Math.floor(job.seconds_on_pass / 60) : 0;
        const secs = job.seconds_on_pass ? Math.floor(job.seconds_on_pass % 60) : 0;
        const elapsed = job.seconds_on_pass > 0 ? ` (${mins}m ${secs}s on this pass)` : "";
        progressPass.textContent = `OCR pass ${job.pass_id || "?"}: ${job.pass_label}${elapsed}`;
        progressPass.classList.remove("hidden");
      } else if (job.stage === "consensus") {
        progressPass.textContent = "Comparing engines and flagging uncertain words…";
        progressPass.classList.remove("hidden");
      } else if (job.stage === "load_image" || job.stage === "preprocess") {
        progressPass.textContent = job.message || "";
        progressPass.classList.remove("hidden");
      } else {
        progressPass.classList.add("hidden");
        progressPass.textContent = "";
      }

      if (job.stage === "page_skip" || job.message?.includes("Skipped")) {
        progressPass.textContent = "Resuming — skipping pages already in output folder";
        progressPass.classList.remove("hidden");
      }

      if (job.stale) {
        const waitMin = Math.max(1, Math.round((job.seconds_since_update || 0) / 60));
        progressStale.textContent =
          `No progress for about ${waitMin} minute(s). The job may be hung — check Task Manager for python.exe, then stop and restart. ` +
          `Details are appended to output/ocr_progress.log`;
        progressStale.classList.remove("hidden");
        progressPanel.classList.add("progress-stale-active");
      } else {
        progressStale.classList.add("hidden");
        progressStale.textContent = "";
        progressPanel.classList.remove("progress-stale-active");
      }
    } else {
      progressPass.classList.add("hidden");
      progressStale.classList.add("hidden");
      progressPanel.classList.remove("progress-stale-active");
    }

    if (job.status === "done") {
      progressPct.textContent = "✓";
      ringFill.style.strokeDashoffset = "0";
      progressTitle.textContent = job.message;
      progressDetail.textContent = "Opening review studio…";
      setTimeout(() => { window.location.href = "/review"; }, 800);
      stopPoll();
    }

    if (job.status === "error") {
      progressPass.classList.add("hidden");
      progressStale.classList.add("hidden");
      progressTitle.textContent = "Something went wrong";
      progressDetail.textContent = job.error || "Try again with fewer pages";
      btnTranscribe.disabled = false;
      btnClear.disabled = false;
      showToast(job.error || "OCR failed");
      stopPoll();
    }
  }

  function startPoll() {
    stopPoll();
    pollTimer = setInterval(refreshStatus, 1200);
  }

  function stopPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function uploadFiles(fileList) {
    const { accepted, rejected } = filterUploadFiles(fileList);
    if (rejected.length) {
      showToast(`Unsupported: ${rejected.slice(0, 2).join(", ")}${rejected.length > 2 ? "…" : ""} — use JPG, PNG, or PDF`);
    }
    if (!accepted.length) {
      if (!rejected.length) showToast("No files detected — drop onto the dashed box");
      return;
    }

    const fd = new FormData();
    for (const f of accepted) fd.append("files", f);

    btnTranscribe.disabled = true;
    dropzone.classList.add("uploading");
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      let data;
      try {
        data = await res.json();
      } catch {
        throw new Error("Upload failed — is Archive Studios still running?");
      }
      if (!res.ok) throw new Error(data.error || "Upload failed");
      showToast(`${data.saved.length} file(s) added`);
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
    } finally {
      dropzone.classList.remove("uploading");
    }
  }

  browseBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });

  dropzone.addEventListener("click", () => fileInput.click());

  fileInput.addEventListener("change", () => {
    uploadFiles([...fileInput.files]);
    fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      dropzone.classList.add("dragover");
    });
  });

  dropzone.addEventListener("dragleave", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!dropzone.contains(e.relatedTarget)) {
      dropzone.classList.remove("dragover");
    }
  });

  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.remove("dragover");
    const files = e.dataTransfer?.files;
    if (files?.length) uploadFiles([...files]);
    else showToast("No files detected — try browse files instead");
  });

  btnClear.addEventListener("click", async () => {
    await fetch("/api/clear-uploads", { method: "POST" });
    showToast("Uploads cleared");
    await refreshStatus();
  });

  const btnNewProject = document.getElementById("btn-new-project");
  if (btnNewProject) {
    btnNewProject.addEventListener("click", async () => {
      if (!confirm("Start a new project? This clears uploads and any previous transcription.")) return;
      await fetch("/api/reset-project", { method: "POST" });
      await fetch("/api/clear-uploads", { method: "POST" });
      showToast("Ready for a new project");
      progressPanel.classList.add("hidden");
      await refreshStatus();
    });
  }

  btnTranscribe.addEventListener("click", async () => {
    try {
      progressPanel.classList.remove("hidden");
      progressPct.textContent = "…";
      progressTitle.textContent = "Starting OCR…";
      progressDetail.textContent = "Verifying gateway and credits";
      btnTranscribe.disabled = true;

      const body = {
        title: bookTitle.value.trim() || "Untitled Book",
        ...languagePayload(),
        page_start: pageStart?.value ? Number(pageStart.value) : null,
        page_end: pageEnd?.value ? Number(pageEnd.value) : null,
      };

      const credits = await safeJsonFetch("/api/credits");
      if ((credits?.remaining_credits ?? 0) <= 0) {
        showToast("No credits remaining — buy more credits first.");
        progressPanel.classList.add("hidden");
        updateStartButtonState();
        return;
      }

      await safeJsonFetch("/api/run-ocr", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      startPoll();
      refreshStatus();
    } catch (err) {
      progressPanel.classList.add("hidden");
      showToast(err.message || "Could not start transcription");
      updateStartButtonState();
    }
  });

  if (btnPdfSplit) {
    btnPdfSplit.addEventListener("click", async () => {
      const statusRes = await fetch("/api/status");
      const status = await statusRes.json();
      const pdf = (status.uploads || []).find((u) => String(u.name || "").toLowerCase().endsWith(".pdf"));
      if (!pdf) {
        showToast("Upload at least one PDF first.");
        return;
      }
      const rangesRaw = (pdfSplitRanges?.value || "").trim();
      if (!rangesRaw) {
        showToast("Enter ranges like 1-30,31-60.");
        return;
      }
      const ranges = rangesRaw.split(",").map((chunk) => {
        const [s, e] = chunk.split("-").map((v) => Number(v.trim()));
        return { start: s, end: e };
      }).filter((r) => Number.isFinite(r.start) && Number.isFinite(r.end));
      const res = await fetch("/api/pdf/split", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: pdf.name, ranges }),
      });
      const body = await res.json();
      if (!res.ok) {
        showToast(body.error || "Split failed");
        return;
      }
      showToast(`Split complete: ${body.outputs.length} file(s).`);
    });
  }

  if (btnPdfMerge) {
    btnPdfMerge.addEventListener("click", async () => {
      const names = (pdfMergeFiles?.value || "").split(",").map((v) => v.trim()).filter(Boolean);
      if (!names.length) {
        showToast("Enter PDF filenames separated by commas.");
        return;
      }
      const res = await fetch("/api/pdf/merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: names, output_name: "merged.pdf" }),
      });
      const body = await res.json();
      if (!res.ok) {
        showToast(body.error || "Merge failed");
        return;
      }
      showToast("Merge complete.");
    });
  }

  loadLanguages().then(refreshStatus);
  if (document.visibilityState === "visible") {
    fetch("/api/status").then((r) => r.json()).then((d) => {
      if (d.job?.status === "running") startPoll();
    });
  }
})();
