(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const browseBtn = document.getElementById("browse-btn");
  const fileList = document.getElementById("file-list");
  const btnClear = document.getElementById("btn-clear");
  const btnTranscribe = document.getElementById("btn-transcribe");
  const bookTitle = document.getElementById("book-title");
  const progressPanel = document.getElementById("progress-panel");
  const progressPct = document.getElementById("progress-pct");
  const progressTitle = document.getElementById("progress-title");
  const progressDetail = document.getElementById("progress-detail");
  const ringFill = document.getElementById("ring-fill");
  const toast = document.getElementById("toast");

  const CIRC = 327;
  let pollTimer = null;

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    toast.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.remove("show"), 3200);
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderFiles(uploads) {
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
    btnTranscribe.disabled = false;
  }

  async function refreshStatus() {
    const res = await fetch("/api/status");
    const data = await res.json();
    renderFiles(data.uploads || []);

    if (data.has_manifest && data.job.status !== "running") {
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
      progressDetail.textContent = job.filename ? `Processing ${job.filename}` : "First run may download OCR models";
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

  async function uploadFiles(files) {
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append("files", f);

    btnTranscribe.disabled = true;
    try {
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");
      showToast(`${data.saved.length} file(s) added`);
      await refreshStatus();
    } catch (err) {
      showToast(err.message);
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
      dropzone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });

  dropzone.addEventListener("drop", (e) => {
    uploadFiles([...e.dataTransfer.files]);
  });

  btnClear.addEventListener("click", async () => {
    await fetch("/api/clear-uploads", { method: "POST" });
    showToast("Uploads cleared");
    await refreshStatus();
  });

  btnTranscribe.addEventListener("click", async () => {
    progressPanel.classList.remove("hidden");
    progressPct.textContent = "…";
    progressTitle.textContent = "Starting engines…";
    progressDetail.textContent = "This may take a few minutes";

    const res = await fetch("/api/run-ocr", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: bookTitle.value.trim() || "Untitled Book" }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || "Could not start");
      return;
    }
    startPoll();
    refreshStatus();
  });

  refreshStatus();
  if (document.visibilityState === "visible") {
    fetch("/api/status").then((r) => r.json()).then((d) => {
      if (d.job?.status === "running") startPoll();
    });
  }
})();
