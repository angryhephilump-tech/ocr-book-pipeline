(() => {
  let manifest = null;
  let queue = [];
  let queueIndex = 0;
  let currentPage = null;
  let flagIndex = 0;

  const el = (id) => document.getElementById(id);

  async function loadManifest() {
    const res = await fetch("/api/manifest");
    manifest = await res.json();
    el("book-title").textContent = manifest.book_title || "OCR Book Review";
    queue = manifest.flagged_queue || [];
    el("queue-total").textContent = queue.length;
    if (queue.length === 0) {
      el("progress").textContent = "No flagged pages — all auto-accepted or run pipeline first.";
      return;
    }
    await loadPage(queue[0]);
  }

  async function loadPage(pageId) {
    const res = await fetch(`/api/page/${pageId}`);
    currentPage = await res.json();
    flagIndex = 0;
    el("page-text").value = currentPage.text || "";
    el("page-image").src = currentPage.image_url || "";
    updateProgress();
    await showCurrentFlag();
  }

  function updateProgress() {
    const pos = queue.indexOf(currentPage.page_id) + 1;
    const unresolved = (currentPage.flags || []).filter((f) => !f.resolved).length;
    el("progress").textContent =
      `Page ${pos} of ${queue.length} flagged pages · ${unresolved} flag(s) remaining on this page`;
    el("jump-page").value = pos;
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
    el("crop-preview").classList.remove("visible");
    el("crop-preview").innerHTML = "";

    if (!flags.length) {
      el("flag-info").textContent = "All flags resolved on this page.";
      return;
    }

    if (flagIndex >= flags.length) flagIndex = flags.length - 1;
    const flag = flags[flagIndex];
    el("flag-info").textContent =
      `Flag ${flagIndex + 1}/${flags.length}: "${flag.text}" (${flag.reason})`;

    const top = topEngineSuggestion(flag);
    Object.entries(flag.engine_texts || {}).forEach(([run, text]) => {
      if (!text) return;
      const btn = document.createElement("button");
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
        const crop = await cropRes.json();
        el("crop-preview").innerHTML = `<img src="${crop.crop_url}" alt="crop">`;
        el("crop-preview").classList.add("visible");
      }
    }
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
      queueIndex = pos + 1;
      await loadPage(queue[queueIndex]);
    }
  }

  async function prevPage() {
    const pos = queue.indexOf(currentPage.page_id);
    if (pos > 0) {
      await savePage();
      queueIndex = pos - 1;
      await loadPage(queue[queueIndex]);
    }
  }

  function acceptTopSuggestion() {
    const flags = (currentPage.flags || []).filter((f) => !f.resolved);
    if (!flags.length) return;
    const flag = flags[flagIndex];
    acceptSuggestion(flag, topEngineSuggestion(flag));
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const k = e.key.toLowerCase();
    if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); nextFlag(); }
    if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); prevFlag(); }
    if (k === "enter") { e.preventDefault(); acceptTopSuggestion(); }
    if (k === "n") { e.preventDefault(); nextPage(); }
    if (k === "p") { e.preventDefault(); prevPage(); }
    if (k === "s") { e.preventDefault(); savePage(); }
  });

  el("btn-export").onclick = async () => {
    await savePage();
    const res = await fetch("/api/export", { method: "POST" });
    const data = await res.json();
    alert(`Exported:\n${data.pdf}\n${data.txt}`);
  };

  el("jump-page").addEventListener("change", async (e) => {
    const idx = parseInt(e.target.value, 10) - 1;
    if (idx >= 0 && idx < queue.length) {
      await savePage();
      await loadPage(queue[idx]);
    }
  });

  loadManifest();
})();
