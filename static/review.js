(() => {
  let manifest = null;
  let queue = [];
  let allPages = [];
  let currentPage = null;
  let flagIndex = 0;

  const el = (id) => document.getElementById(id);
  const toast = el("toast");

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    toast.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => toast.classList.remove("show"), 2800);
  }

  async function loadManifest() {
    const res = await fetch("/api/manifest");
    manifest = await res.json();
    el("book-title").textContent = manifest.book_title || "Review";
    allPages = manifest.all_pages || manifest.pages?.map((p) => p.page_id) || [];
    queue = manifest.flagged_queue?.length ? manifest.flagged_queue : allPages;

    if (!queue.length) {
      el("progress").textContent = "No pages found — upload and transcribe from home.";
      return;
    }

    el("queue-total").textContent = queue.length;
    await loadPage(queue[0]);
  }

  async function loadPage(pageId) {
    const res = await fetch(`/api/page/${pageId}`);
    if (!res.ok) {
      showToast("Could not load page");
      return;
    }
    currentPage = await res.json();
    flagIndex = 0;
    el("page-text").value = currentPage.text || "";
    el("page-image").src = currentPage.image_url || "";
    el("page-image").style.display = currentPage.image_url ? "block" : "none";
    updateProgress();
    await showCurrentFlag();
  }

  function updateProgress() {
    const pos = queue.indexOf(currentPage.page_id) + 1;
    const unresolved = (currentPage.flags || []).filter((f) => !f.resolved).length;
    const totalFlags = (currentPage.flags || []).length;
    el("progress").textContent =
      `Page ${pos} of ${queue.length}` +
      (totalFlags ? ` · ${unresolved} flag${unresolved === 1 ? "" : "s"} to review` : " · clean");
    el("jump-page").value = pos;
    el("jump-page").max = queue.length;
  }

  function topEngineSuggestion(flag) {
    const texts = flag.engine_texts || {};
    const counts = {};
    Object.values(texts).forEach((t) => {
      if (t) counts[t] = (counts[t] || 0) + 1;
    });
    let best = flag.text;
    let bestCount = 0;
    Object.entries(counts).forEach(([t, c]) => {
      if (c > bestCount) {
        best = t;
        bestCount = c;
      }
    });
    return best;
  }

  async function showCurrentFlag() {
    const flags = (currentPage.flags || []).filter((f) => !f.resolved);
    const bar = el("engine-buttons");
    bar.innerHTML = "";
    const crop = el("crop-preview");
    crop.classList.remove("visible");
    crop.innerHTML = "";

    if (!flags.length) {
      el("flag-info").textContent = totalFlagsLabel(currentPage.flags);
      return;
    }

    if (flagIndex >= flags.length) flagIndex = flags.length - 1;
    const flag = flags[flagIndex];
    el("flag-info").textContent = `Flag ${flagIndex + 1}/${flags.length} · ${flag.reason}`;

    const top = topEngineSuggestion(flag);
    Object.entries(flag.engine_texts || {}).forEach(([run, text]) => {
      if (!text) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "engine-btn" + (text === top ? " top" : "");
      btn.textContent = `${run}: ${text}`;
      btn.onclick = () => acceptSuggestion(flag, text);
      bar.appendChild(btn);
    });

    const allFlags = currentPage.flags || [];
    const globalIdx = allFlags.indexOf(flag);
    if (globalIdx >= 0) {
      const cropRes = await fetch(`/api/crop/${currentPage.page_id}/${globalIdx}`);
      if (cropRes.ok) {
        const data = await cropRes.json();
        crop.innerHTML = `<img src="${data.crop_url}" alt="Flagged region">`;
        crop.classList.add("visible");
      }
    }
  }

  function totalFlagsLabel(flags) {
    const n = (flags || []).length;
    if (!n) return "No flags — page looks clean";
    const open = flags.filter((f) => !f.resolved).length;
    if (!open) return "All flags resolved";
    return `${open} flag${open === 1 ? "" : "s"} remaining`;
  }

  async function acceptSuggestion(flag, text) {
    const textarea = el("page-text");
    if (flag.text && textarea.value.includes(flag.text)) {
      textarea.value = textarea.value.replace(flag.text, text);
    }
    await savePage(flag.span_id, text);
    flag.resolved = true;
    updateProgress();
    flagIndex = 0;
    await showCurrentFlag();
    showToast("Suggestion applied");
  }

  async function savePage(resolvedFlag = null, resolution = "") {
    await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page_id: currentPage.page_id,
        text: el("page-text").value,
        resolved_flag: resolvedFlag,
        resolution,
      }),
    });
  }

  function nextFlag() {
    const flags = (currentPage.flags || []).filter((f) => !f.resolved);
    if (flagIndex < flags.length - 1) {
      flagIndex++;
      showCurrentFlag();
    }
  }

  function prevFlag() {
    if (flagIndex > 0) {
      flagIndex--;
      showCurrentFlag();
    }
  }

  async function nextPage() {
    const pos = queue.indexOf(currentPage.page_id);
    if (pos < queue.length - 1) {
      await savePage();
      await loadPage(queue[pos + 1]);
    }
  }

  async function prevPage() {
    const pos = queue.indexOf(currentPage.page_id);
    if (pos > 0) {
      await savePage();
      await loadPage(queue[pos - 1]);
    }
  }

  function acceptTopSuggestion() {
    const flags = (currentPage.flags || []).filter((f) => !f.resolved);
    if (!flags.length) return;
    acceptSuggestion(flags[flagIndex], topEngineSuggestion(flags[flagIndex]));
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const k = e.key.toLowerCase();
    if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); nextFlag(); }
    if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); prevFlag(); }
    if (k === "enter" && !e.shiftKey) { e.preventDefault(); acceptTopSuggestion(); }
    if (k === "n") { e.preventDefault(); nextPage(); }
    if (k === "p") { e.preventDefault(); prevPage(); }
    if (k === "s" && (e.ctrlKey || e.metaKey || !e.shiftKey)) {
      if (!e.ctrlKey && !e.metaKey) { e.preventDefault(); savePage(); showToast("Saved"); }
    }
  });

  el("btn-export").onclick = async () => {
    await savePage();
    el("btn-export").disabled = true;
    try {
      const res = await fetch("/api/export", { method: "POST" });
      const data = await res.json();
      el("export-pdf").textContent = data.pdf;
      el("export-txt").textContent = data.txt;
      el("export-modal").classList.remove("hidden");
    } finally {
      el("btn-export").disabled = false;
    }
  };

  el("btn-close-modal").onclick = () => el("export-modal").classList.add("hidden");
  el("btn-save").onclick = async () => { await savePage(); showToast("Page saved"); };
  el("btn-next-page").onclick = () => nextPage();
  el("btn-prev-page").onclick = () => prevPage();

  el("jump-page").addEventListener("change", async (e) => {
    const idx = parseInt(e.target.value, 10) - 1;
    if (idx >= 0 && idx < queue.length) {
      await savePage();
      await loadPage(queue[idx]);
    }
  });

  loadManifest();
})();
