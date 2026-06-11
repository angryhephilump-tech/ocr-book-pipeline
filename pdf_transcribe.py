#!/usr/bin/env python3
# Install dependencies:
#   pip install pdf2image requests Pillow
# Poppler (required for PDF → images):
#   winget install poppler
#   — or copy Poppler bin into vendor/poppler/bin/ in this repo

"""
Transcribe a scanned PDF with Claude Opus vision (high-res).

Pipeline v2: 2 independent transcriptions → reconcile content disagreements only →
sentence-level hard-term spot patches → transcribed.txt + reconcile_log.txt.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import ndiff
from io import BytesIO
from pathlib import Path
from typing import Callable

import requests
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
PROMPT_FILE = CONFIG_DIR / "transcription_prompt.txt"
HARD_TERMS_FILE = CONFIG_DIR / "hard_terms.txt"  # legacy; prefer hard_terms_<source_id>.txt

ARCHIVAL_VERBATIM = (
    "If the source image shows something that appears to be a printing error, inconsistency, "
    "or unusual convention, transcribe it exactly as it appears on the page. Do not correct it. "
    "If the source itself does not flag it as an error, append [sic] after it. "
    "Your job is to report what is printed, not to improve it."
)

SKIP_LINE_RE = re.compile(r"^\[Skipped:\s*.+\]\s*$", re.IGNORECASE)
UNCERTAIN_RE = re.compile(r"^UNCERTAIN:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

SYSTEM_PROMPT = (
    "You are a mechanical OCR engine, not a chat assistant. "
    "Transcribe visible PRINTED or stamped text exactly for a library digitization project. "
    "The book is public-domain. "
    "FORBIDDEN: apologies, refusals, image descriptions, 'I can see', 'This page', 'However'. "
    "For non-content pages output exactly one skip line as instructed in the user message. "
    "Otherwise output ONLY transcribed characters with original line breaks."
)

PROVIDER = "claude"
DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
# Older PDF Transcribe builds defaulted to Haiku; upgrade saved settings once.
_LEGACY_SAVED_MODELS = frozenset(
    {
        "claude-haiku-4-5",
        "claude-3-5-haiku-20241022",
        "claude-3-haiku-20240307",
    }
)
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_VERSION = "2023-06-01"
API_DELAY_SEC = 0.5
BATCH_POLL_SEC = 15
# Vision batches embed full JPEGs; large chunks often trigger SSL EOF on upload.
BATCH_MAX_REQUESTS = 20
PROCESSING_MODES = ("auto", "realtime", "batch")
DEFAULT_PROCESSING_MODE = "auto"
DPI = 300
MAX_IMAGE_DIM = 2576
MAX_TOKENS = 8192
JPEG_QUALITY = 88
NUM_TRANSCRIPTION_RUNS = 2
NUM_RUNS = NUM_TRANSCRIPTION_RUNS
PAGE_MARKER_RE = re.compile(r"^--- Page (\d+) ---\s*$", re.MULTILINE)

DEFAULT_SKIP_FRONT_PAGES = 2

_CHATTER_MARKERS = (
    "i can see",
    "i cannot",
    "this page appears",
    "this page is",
    "this image appears",
    "this image shows",
    "the image appears",
    "however,",
    "if you have",
    "please provide",
    "there is no visible printed text",
    "there is no substantive",
    "there is no discernible",
    "no substantial printed text",
    "no readable printed text",
    "no clearly legible",
    "would be happy to",
    "cannot identify any readable",
    "does not appear to contain",
    "not appear to contain printed",
)


def looks_like_chatter(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in _CHATTER_MARKERS)


def load_transcription_prompt() -> str:
    if PROMPT_FILE.is_file():
        return PROMPT_FILE.read_text(encoding="utf-8").strip()
    return (
        "OCR this page. Transcribe exactly what is printed; never correct or normalize. "
        f"{ARCHIVAL_VERBATIM} "
        "Use *asterisks* for italics. Footnotes after --- FOOTNOTES ---. "
        "Use [illegible] or [damaged] as needed. "
        "Skip lines: [Skipped: Google boilerplate], [Skipped: blank page], [Skipped: library stamp]."
    )


def load_hard_terms(source_id: str | None = None, state: dict | None = None) -> list[str]:
    from pdf_transcribe_lang import load_hard_terms as _load

    return _load(source_id, state)


def load_impossible_strings(source_id: str | None = None, state: dict | None = None) -> list[str]:
    from pdf_transcribe_lang import load_impossible_strings as _load

    return _load(source_id, state)


def job_config_from_state(state: dict):
    from pdf_transcribe_lang import job_language_config_from_state

    return job_language_config_from_state(state)


def transcription_user_prompt(work_dir: Path | None = None, state: dict | None = None) -> str:
    base = load_transcription_prompt()
    st = state or (load_state(work_dir) if work_dir else {})
    rules = (st or {}).get("normalization_rules") or ""
    profile = (st or {}).get("detected_source_profile") or {}
    if not rules:
        rules = profile.get("normalization_rules") or ""
    from pdf_transcribe_lang import (
        job_language_config_from_state,
        macron_tilde_prompt_line,
        notation_prompt_line,
    )

    notation_parts = [notation_prompt_line()]
    if st:
        cfg = job_language_config_from_state(st)
        if cfg.unify_abbreviation_marks:
            notation_parts.append(macron_tilde_prompt_line())
    notation = "\n".join(notation_parts)
    if rules:
        return (
            f"{base}\n\nLanguage-specific archival rules (auto-detected):\n{rules}\n\n"
            f"Notation: {notation}"
        )
    return f"{base}\n\nNotation: {notation}"


def skip_line(reason: str) -> str:
    return f"[Skipped: {reason}]"


def format_page_block(page_num: int, body: str) -> str:
    return f"--- Page {page_num} ---\n{body.strip()}"


def is_skip_body(text: str) -> bool:
    t = text.strip()
    return bool(SKIP_LINE_RE.match(t)) or t.startswith("[Skipped:")


def normalize_transcription_output(text: str, lang_cfg=None, *, section: str | None = None) -> str:
    from pdf_transcribe_lang import (
        normalize_notation_tier1,
        should_apply_tier2_unification,
        unify_abbreviation_marks_tier2,
    )

    t = text.strip()
    if not t or looks_like_chatter(t):
        return skip_line("blank page")
    if is_skip_body(t):
        return t.splitlines()[0].strip()
    lower = t.lower()
    if "google" in lower and "book" in lower:
        return skip_line("Google boilerplate")
    if "library" in lower and "stamp" in lower and len(t) < 120:
        return skip_line("library stamp")
    t = normalize_notation_tier1(t)
    if should_apply_tier2_unification(lang_cfg, section=section):
        t = unify_abbreviation_marks_tier2(t)
    return t


def normalize_ocr_output(text: str) -> str:
    return normalize_transcription_output(text)


def api_key_storage_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PDF Transcribe"
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def mask_api_key(key: str) -> str:
    key = key.strip()
    if len(key) <= 8:
        return "••••••••"
    return f"{key[:4]}…{key[-4:]}"


def _read_env_key() -> str:
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    dotenv = Path(__file__).resolve().parent / ".env"
    if not dotenv.is_file():
        return ""
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _resolve_saved_model(raw: str) -> tuple[str, bool]:
    """Return (model, migrated). Env CLAUDE_MODEL always wins over a saved legacy default."""
    env_model = (os.environ.get("CLAUDE_MODEL") or "").strip()
    if env_model:
        return env_model, False
    m = (raw or "").strip() or DEFAULT_MODEL
    if m in _LEGACY_SAVED_MODELS or "haiku" in m.lower():
        if m != DEFAULT_MODEL:
            return DEFAULT_MODEL, True
    return m, False


def load_settings() -> dict:
    settings = {
        "provider": PROVIDER,
        "api_key": _read_env_key(),
        "model": DEFAULT_MODEL,
        "processing_mode": DEFAULT_PROCESSING_MODE,
        "spot_check_enabled": True,
        "language": "spanish",
        "source_id": "ixtlilxochitl",
        "script": "latin",
    }
    path = api_key_storage_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            key = (data.get("api_key") or data.get("anthropic_api_key") or data.get("deepseek_api_key") or "").strip()
            if key:
                settings["api_key"] = key
            saved_model = (data.get("model") or "").strip()
            if saved_model:
                model, migrated = _resolve_saved_model(saved_model)
                settings["model"] = model
                if migrated:
                    data["model"] = model
                    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            if (data.get("provider") or "").strip():
                settings["provider"] = data["provider"].strip()
            mode = (data.get("processing_mode") or "").strip().lower()
            if mode in PROCESSING_MODES:
                settings["processing_mode"] = mode
            if "spot_check_enabled" in data:
                settings["spot_check_enabled"] = bool(data["spot_check_enabled"])
            if (data.get("language") or "").strip():
                settings["language"] = data["language"].strip().lower()
            if (data.get("source_id") or "").strip():
                settings["source_id"] = data["source_id"].strip().lower()
            if (data.get("script") or "").strip():
                settings["script"] = data["script"].strip().lower()
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def load_saved_api_key() -> str | None:
    key = load_settings().get("api_key", "").strip()
    return key or None


def save_api_key(api_key: str) -> Path:
    return save_settings(api_key=api_key.strip())


def save_settings(
    *,
    api_key: str | None = None,
    model: str | None = None,
    processing_mode: str | None = None,
    spot_check_enabled: bool | None = None,
    language: str | None = None,
    source_id: str | None = None,
    script: str | None = None,
) -> Path:
    current = load_settings()
    if api_key is not None:
        current["api_key"] = api_key.strip()
    if model is not None:
        current["model"] = model.strip()
    if processing_mode is not None:
        mode = processing_mode.strip().lower()
        current["processing_mode"] = mode if mode in PROCESSING_MODES else DEFAULT_PROCESSING_MODE
    if spot_check_enabled is not None:
        current["spot_check_enabled"] = spot_check_enabled
    if language is not None:
        current["language"] = language.strip().lower()
    if source_id is not None:
        current["source_id"] = source_id.strip().lower()
    if script is not None:
        current["script"] = script.strip().lower()
    path = api_key_storage_path()
    payload = {
        "provider": PROVIDER,
        "api_key": current["api_key"],
        "anthropic_api_key": current["api_key"],
        "model": current["model"],
        "processing_mode": current.get("processing_mode", DEFAULT_PROCESSING_MODE),
        "spot_check_enabled": current.get("spot_check_enabled", True),
        "language": current.get("language", "spanish"),
        "source_id": current.get("source_id", "ixtlilxochitl"),
        "script": current.get("script", "latin"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    env_path = Path(__file__).resolve().parent / ".env"
    env_path.write_text(f"ANTHROPIC_API_KEY={current['api_key']}\n", encoding="utf-8")
    return path


def api_key_status() -> dict:
    settings = load_settings()
    key = settings.get("api_key", "").strip()
    base = {
        "provider": PROVIDER,
        "model": settings["model"],
        "processing_mode": settings.get("processing_mode", DEFAULT_PROCESSING_MODE),
        "spot_check_enabled": settings.get("spot_check_enabled", True),
        "language": settings.get("language", "spanish"),
        "source_id": settings.get("source_id", "ixtlilxochitl"),
        "script": settings.get("script", "latin"),
        "storage_path": str(api_key_storage_path()),
        "setup_url": "https://console.anthropic.com/settings/keys",
        "batch_discount": "50% off token usage via Anthropic Message Batches API",
    }
    if not key:
        return {**base, "saved": False, "hint": None}
    return {**base, "saved": True, "hint": mask_api_key(key)}


def _format_api_error(status: int, body: str) -> str:
    snippet = body[:600] if body else "(no details)"
    if "content filtering" in body.lower():
        return (
            "Claude blocked this page with its automated safety filter — "
            "this is NOT a copyright claim.\n\n"
            "It can false-alarm on old book scans (illustrations, archaic text, damage stains). "
            "Close the app, reopen Launch PDF Transcribe.bat (updated prompts), and try again.\n\n"
            "If it keeps happening on the same page, tell Anthropic support the request_id "
            "below and say you are doing historical OCR.\n\n"
            f"Details: {snippet}"
        )
    if status in (401, 403):
        return (
            f"Claude rejected your API key ({status}).\n"
            f"Details: {snippet}\n\n"
            "Fix: double-click 'Save Claude Key.bat' and paste a key from\n"
            "  https://console.anthropic.com/settings/keys"
        )
    return f"Claude API error {status}: {snippet}"


def resolve_api_key(provided: str | None = None) -> str:
    key = (provided or "").strip()
    if key:
        return key
    saved = load_saved_api_key()
    if saved:
        return saved
    raise ValueError(
        "No Claude API key found. Double-click 'Save Claude Key.bat', "
        "or paste your key in the app (starts with sk-ant-)."
    )


def _poppler_path() -> str | None:
    env = os.environ.get("POPPLER_PATH", "").strip()
    if env and Path(env).is_dir():
        return env
    try:
        from pipeline.paths import poppler_bin_dir

        found = poppler_bin_dir()
        return str(found) if found else None
    except ImportError:
        pass
    return None


def work_dir_for_pdf(
    pdf_path: Path,
    source_name: str | None = None,
    *,
    custom_work_dir: str | None = None,
) -> Path:
    from pdf_transcribe_integrity import work_dir_for_source

    return work_dir_for_source(
        pdf_path,
        source_name,
        custom_work_dir=custom_work_dir,
    )


def state_path(work_dir: Path) -> Path:
    return work_dir / "state.json"


def progress_path(work_dir: Path) -> Path:
    return work_dir / "progress.json"


def load_state(work_dir: Path) -> dict:
    path = state_path(work_dir)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def load_progress(work_dir: Path) -> dict | None:
    path = progress_path(work_dir)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def save_state(work_dir: Path, state: dict) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    state_path(work_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def _atomic_write_json(path: Path, payload: dict, *, attempts: int = 10) -> None:
    """Write JSON via temp file + replace; retry on Windows lock races with UI polling."""
    text = json.dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    last_err: OSError | None = None
    for attempt in range(attempts):
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_err = exc
            winerr = getattr(exc, "winerror", None)
            retryable = winerr in (5, 32) or exc.errno in (5, 13, 16)
            if not retryable:
                raise
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    # Fallback: direct write (load_progress already tolerates empty/invalid JSON).
    try:
        path.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
    except OSError:
        if last_err is not None:
            raise last_err
        raise


def write_progress(
    work_dir: Path,
    *,
    phase: str,
    current_run: int,
    page: int,
    total_pages: int,
    message: str,
    eta_seconds: float | None = None,
    processing_mode: str | None = None,
    batch_done: int | None = None,
    batch_total: int | None = None,
    step_done: int | None = None,
    step_total: int | None = None,
) -> None:
    payload = {
        "phase": phase,
        "current_run": current_run,
        "page": page,
        "total_pages": total_pages,
        "message": message,
        "eta_seconds": eta_seconds,
        "processing_mode": processing_mode,
        "batch_done": batch_done,
        "batch_total": batch_total,
        "step_done": step_done,
        "step_total": step_total,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(progress_path(work_dir), payload)


def resolve_processing_mode(mode: str | None, *, max_pages: int | None) -> str:
    """auto: 10-page tests use live API; full book uses batch (50% off)."""
    chosen = (mode or load_settings().get("processing_mode") or DEFAULT_PROCESSING_MODE).strip().lower()
    if chosen not in PROCESSING_MODES:
        chosen = DEFAULT_PROCESSING_MODE
    if chosen == "auto":
        if max_pages is not None and max_pages > 0:
            return "realtime"
        return "batch"
    return chosen


def processing_mode_label(mode: str, *, max_pages: int | None) -> str:
    resolved = resolve_processing_mode(mode, max_pages=max_pages)
    if resolved == "batch":
        return "batch (50% off — usually under 1 hour)"
    return "live API (results as each page finishes)"


@dataclass(frozen=True)
class WorkPageRange:
    """PDF page indices to render and transcribe (real book page numbers)."""

    page_numbers: list[int]
    skip_front_pages: int
    max_pages: int | None
    pdf_page_count: int

    @property
    def first_page(self) -> int:
        return self.page_numbers[0] if self.page_numbers else 0

    @property
    def last_page(self) -> int:
        return self.page_numbers[-1] if self.page_numbers else 0

    @property
    def job_page_count(self) -> int:
        return len(self.page_numbers)


def parse_page_list(raw: str) -> list[int]:
    """Parse comma/range page list: '13,45,60-62' → [13,45,60,61,62]."""
    pages: list[int] = []
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    return sorted(set(pages))


def resolve_explicit_page_list(pdf_page_count: int, page_numbers: list[int]) -> WorkPageRange:
    if not page_numbers:
        raise ValueError("No pages specified.")
    bad = [p for p in page_numbers if p < 1 or p > pdf_page_count]
    if bad:
        raise ValueError(f"Page numbers out of range (1–{pdf_page_count}): {bad[:5]}")
    return WorkPageRange(
        page_numbers=sorted(set(page_numbers)),
        skip_front_pages=0,
        max_pages=len(page_numbers),
        pdf_page_count=pdf_page_count,
    )


def resolve_work_page_range(
    pdf_page_count: int,
    *,
    skip_front_pages: int,
    max_pages: int | None,
    explicit_pages: list[int] | None = None,
) -> WorkPageRange:
    if explicit_pages:
        return resolve_explicit_page_list(pdf_page_count, explicit_pages)
    """Test N pages after skipping: skip=12 and test=10 → PDF pages 13–22."""
    skip = max(0, skip_front_pages)
    if pdf_page_count <= 0:
        raise ValueError("PDF has no pages.")
    if skip >= pdf_page_count:
        raise ValueError(
            f"Skip first {skip} pages, but the PDF only has {pdf_page_count} page(s). "
            "Lower the skip count."
        )
    if max_pages is not None and max_pages > 0:
        first = skip + 1
        last = min(skip + max_pages, pdf_page_count)
    else:
        first = skip + 1
        last = pdf_page_count
    if first > last:
        raise ValueError(
            f"No pages to process after skipping {skip} (first page would be {first})."
        )
    page_numbers = list(range(first, last + 1))
    return WorkPageRange(
        page_numbers=page_numbers,
        skip_front_pages=skip,
        max_pages=max_pages,
        pdf_page_count=pdf_page_count,
    )


def job_page_numbers(state: dict) -> list[int]:
    raw = state.get("page_numbers")
    if raw:
        return [int(x) for x in raw]
    total = int(state.get("total_pages") or 0)
    return list(range(1, total + 1))


def page_should_skip_google_auto(page_num: int, skip_front_pages: int, image_path: Path) -> bool:
    """Auto-detect Google notice only near the skipped front matter (not body-page footers)."""
    if page_num <= skip_front_pages:
        return True
    buffer_end = skip_front_pages + 5
    if page_num <= buffer_end and is_google_books_boilerplate(image_path):
        return True
    return False


# list of (pdf_page_number, image_path)
WorkPages = list[tuple[int, Path]]


@dataclass
class ProgressTracker:
    total_pages: int
    total_api_calls: int
    completed_calls: int = 0
    run_start: float = field(default_factory=time.monotonic)
    job_start: float = field(default_factory=time.monotonic)

    def tick(self) -> float | None:
        self.completed_calls += 1
        if self.completed_calls < 2 or self.total_api_calls <= 0:
            return None
        elapsed = max(time.monotonic() - self.job_start, 0.001)
        rate = self.completed_calls / elapsed
        if rate <= 0:
            return None
        remaining = max(0, self.total_api_calls - self.completed_calls)
        return remaining / rate


def _pdf_page_count(pdf_path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf_path)).pages)


def render_pdf_pages(
    pdf_path: Path,
    images_dir: Path,
    *,
    page_numbers: list[int],
    dpi: int = DPI,
) -> WorkPages:
    try:
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise SystemExit(
            "Missing pdf2image. Install with:\n  pip install pdf2image requests Pillow"
        ) from exc

    images_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"dpi": dpi}
    poppler = _poppler_path()
    if poppler:
        kwargs["poppler_path"] = poppler

    pages: WorkPages = []
    for page_num in page_numbers:
        out = images_dir / f"page_{page_num:04d}.png"
        if out.is_file():
            pages.append((page_num, out))
            continue
        images = convert_from_path(
            str(pdf_path),
            first_page=page_num,
            last_page=page_num,
            **kwargs,
        )
        if not images:
            break
        images[0].save(out, "PNG")
        prepare_image_file(out, out)
        pages.append((page_num, out))
    return pages


def prepare_image_file(src: Path, dest: Path | None = None) -> Path:
    """Grayscale + resize to MAX_IMAGE_DIM on long edge; save as JPEG."""
    dest = dest or src
    with Image.open(src) as img:
        img = img.convert("L")
        w, h = img.size
        longest = max(w, h, 1)
        if longest > MAX_IMAGE_DIM:
            scale = MAX_IMAGE_DIM / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() in (".jpg", ".jpeg"):
            img.save(dest, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        else:
            img.save(dest, format="PNG", optimize=True)
    return dest


def batch_state_path(work_dir: Path) -> Path:
    return work_dir / "batch_state.json"


def load_batch_state(work_dir: Path) -> dict:
    path = batch_state_path(work_dir)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_batch_state(work_dir: Path, data: dict) -> None:
    batch_state_path(work_dir).write_text(json.dumps(data, indent=2), encoding="utf-8")


def batch_custom_id(run: int, page_num: int) -> str:
    return f"r{run}_p{page_num:04d}"


def reconcile_custom_id(page_num: int) -> str:
    return f"rec_p{page_num:04d}"


def spot_check_custom_id(page_num: int, op_index: int = 0) -> str:
    return f"spot_p{page_num:04d}_{op_index:02d}"


def spot_page_custom_id(page_num: int, chunk_idx: int = 0) -> str:
    if chunk_idx:
        return f"spotpg_p{page_num:04d}_{chunk_idx:02d}"
    return f"spotpg_p{page_num:04d}"


def pass4_custom_id(page_num: int) -> str:
    return f"p4_p{page_num:04d}"


def parse_batch_custom_id(custom_id: str) -> tuple[str, int, int]:
    """Return (kind, run_or_op_index, page_num). kind: transcribe|reconcile|spot|spotpg|pass4."""
    match = re.match(r"^r(\d+)_p(\d+)$", custom_id)
    if match:
        return "transcribe", int(match.group(1)), int(match.group(2))
    match = re.match(r"^rec_p(\d+)$", custom_id)
    if match:
        return "reconcile", 0, int(match.group(1))
    match = re.match(r"^spotpg_p(\d+)(?:_(\d+))?$", custom_id)
    if match:
        return "spotpg", int(match.group(2) or 0), int(match.group(1))
    match = re.match(r"^p4_p(\d+)$", custom_id)
    if match:
        return "pass4", 0, int(match.group(1))
    match = re.match(r"^spot_p(\d+)(?:_(\d+))?$", custom_id)
    if match:
        return "spot", int(match.group(2) or 0), int(match.group(1))
    raise ValueError(f"Unexpected batch id: {custom_id!r}")


def anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def anthropic_request(
    method: str,
    url: str,
    api_key: str,
    *,
    json_body: dict | None = None,
    timeout: tuple[int, int] = (60, 120),
    stream: bool = False,
    max_retries: int = 6,
) -> requests.Response:
    """POST/GET with retries for transient SSL, timeouts, and rate limits."""
    headers = anthropic_headers(api_key)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
                stream=stream,
            )
            if resp.status_code == 429:
                time.sleep(min(90, 2 ** (attempt + 2)))
                continue
            return resp
        except requests.exceptions.RequestException as exc:
            last_err = exc
            time.sleep(min(120, 3 * (2**attempt)))
    raise RuntimeError(
        f"Network error reaching Anthropic ({method} {url}): {last_err}\n\n"
        "This is usually a dropped connection (Wi‑Fi, VPN, sleep, or an upload that was too large). "
        "Check your connection, keep the PC awake, and click Start again — finished pages are saved."
    ) from last_err


def build_vision_message_params(
    image_path: Path,
    model: str,
    *,
    user_text: str | None = None,
    max_tokens: int | None = None,
) -> dict:
    b64 = image_to_base64_jpeg(image_path)
    return {
        "model": model,
        "system": SYSTEM_PROMPT,
        "max_tokens": max_tokens if max_tokens is not None else MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": user_text or load_transcription_prompt()},
                ],
            }
        ],
    }


def extract_assistant_text(message: dict) -> str:
    parts = message.get("content") or []
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return normalize_ocr_output("\n".join(texts).strip())


def image_to_base64_jpeg(image_path: Path) -> str:
    buf = BytesIO()
    with Image.open(image_path) as img:
        img = img.convert("L")
        w, h = img.size
        longest = max(w, h, 1)
        if longest > MAX_IMAGE_DIM:
            scale = MAX_IMAGE_DIM / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def call_claude(
    api_key: str,
    image_path: Path,
    *,
    model: str | None = None,
    max_retries: int = 4,
    user_text: str | None = None,
) -> str:
    model = (model or load_settings()["model"]).strip()
    headers = anthropic_headers(api_key)
    payload = build_vision_message_params(image_path, model, user_text=user_text)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=(60, 300))
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(_format_api_error(resp.status_code, resp.text))
            data = resp.json()
            parts = data.get("content") or []
            raw = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
            if not looks_like_chatter(raw):
                return normalize_ocr_output(raw)
            if looks_like_chatter(raw):
                strict_payload = {
                    **payload,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                payload["messages"][0]["content"][0],
                                {
                                    "type": "text",
                                    "text": (
                                        "Copy ONLY visible printed/stamped text. "
                                        "No commentary. One line per printed line. "
                                        "If no content, one line only: [Skipped: blank page]"
                                    ),
                                },
                            ],
                        }
                    ],
                }
                retry = requests.post(
                    ANTHROPIC_URL, headers=headers, json=strict_payload, timeout=(60, 300)
                )
                if retry.status_code < 400:
                    rdata = retry.json()
                    rparts = rdata.get("content") or []
                    rtexts = [p.get("text", "") for p in rparts if p.get("type") == "text"]
                    raw = "\n".join(rtexts).strip()
            return normalize_ocr_output(raw)
        except requests.exceptions.RequestException as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
        except Exception as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Claude failed for {image_path.name}: {last_err}") from last_err


@dataclass(frozen=True)
class PageWorkItem:
    run: int
    page_num: int
    image_path: Path


def create_message_batch(
    api_key: str,
    items: list[PageWorkItem],
    model: str,
    *,
    user_prompt: str | None = None,
) -> str:
    prompt = user_prompt or load_transcription_prompt()
    requests_payload = [
        {
            "custom_id": batch_custom_id(item.run, item.page_num),
            "params": build_vision_message_params(item.image_path, model, user_text=prompt),
        }
        for item in items
    ]
    resp = anthropic_request(
        "POST",
        ANTHROPIC_BATCH_URL,
        api_key,
        json_body={"requests": requests_payload},
        timeout=(120, 600),
        max_retries=8,
    )
    if resp.status_code >= 400:
        raise RuntimeError(_format_api_error(resp.status_code, resp.text))
    batch_id = resp.json().get("id")
    if not batch_id:
        raise RuntimeError("Batch API did not return a batch id.")
    return batch_id


def retrieve_message_batch(api_key: str, batch_id: str) -> dict:
    resp = anthropic_request(
        "GET",
        f"{ANTHROPIC_BATCH_URL}/{batch_id}",
        api_key,
        timeout=(30, 120),
    )
    if resp.status_code >= 400:
        raise RuntimeError(_format_api_error(resp.status_code, resp.text))
    return resp.json()


def iter_batch_results(api_key: str, batch_id: str, batch_info: dict) -> list[dict]:
    results_url = (batch_info.get("results_url") or "").strip()
    if results_url:
        url = results_url
    else:
        url = f"{ANTHROPIC_BATCH_URL}/{batch_id}/results"
    resp = anthropic_request(
        "GET",
        url,
        api_key,
        timeout=(60, 900),
        stream=True,
    )
    if resp.status_code >= 400:
        raise RuntimeError(_format_api_error(resp.status_code, resp.text))
    lines: list[dict] = []
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        lines.append(json.loads(raw_line))
    return lines


def parse_batch_result_line(line: dict) -> tuple[str, str | None, str | None]:
    """Return (custom_id, text_or_none, error_or_none)."""
    custom_id = line.get("custom_id") or ""
    result = line.get("result") or {}
    rtype = result.get("type")
    if rtype == "succeeded":
        message = result.get("message") or {}
        return custom_id, extract_assistant_text(message), None
    if rtype == "errored":
        err = result.get("error") or {}
        return custom_id, None, json.dumps(err)[:500]
    return custom_id, None, f"batch result type: {rtype}"


def wait_for_batch(
    api_key: str,
    batch_id: str,
    *,
    on_status: Callable[[dict], None] | None = None,
) -> dict:
    while True:
        info = retrieve_message_batch(api_key, batch_id)
        if on_status:
            on_status(info)
        status = info.get("processing_status")
        if status == "ended":
            return info
        if status in ("canceling", "canceled"):
            raise RuntimeError(f"Batch {batch_id} was canceled.")
        time.sleep(BATCH_POLL_SEC)


def write_page_result(
    work_dir: Path,
    state: dict,
    run: int,
    page_num: int,
    text: str,
) -> None:
    from pdf_transcribe_source import classify_page_section, page_section_hint

    lang_cfg = job_config_from_state(state)
    section: str | None = None
    if not is_skip_body(text):
        if run == 1:
            profile = state.get("detected_source_profile") or {}
            langs = profile.get("languages") or {}
            section = classify_page_section(text, langs)
            state.setdefault("page_sections", {})[str(page_num)] = section
        else:
            section = page_section_hint(state, page_num)
    text = normalize_transcription_output(text, lang_cfg, section=section)
    out_page = page_transcript_path(work_dir, run, page_num)
    out_page.parent.mkdir(parents=True, exist_ok=True)
    out_page.write_text(text, encoding="utf-8")
    mark_page_complete(state, run, page_num)
    save_state(work_dir, state)


def apply_local_skips(
    work_dir: Path,
    state: dict,
    work_pages: WorkPages,
    *,
    skip_front_pages: int,
) -> None:
    for run in range(1, NUM_RUNS + 1):
        done = completed_pages_for_run(state, run)
        for page_num, image_path in work_pages:
            if page_num in done:
                continue
            if page_should_skip_google_auto(page_num, skip_front_pages, image_path):
                write_page_result(work_dir, state, run, page_num, skip_line("Google boilerplate"))


def collect_api_work(
    state: dict,
    work_pages: WorkPages,
    *,
    skip_front_pages: int,
    run_only: int | None = None,
) -> list[PageWorkItem]:
    pending: list[PageWorkItem] = []
    runs = [run_only] if run_only else list(range(1, NUM_RUNS + 1))
    for run in runs:
        done = completed_pages_for_run(state, run)
        for page_num, image_path in work_pages:
            if page_num in done:
                continue
            if page_should_skip_google_auto(page_num, skip_front_pages, image_path):
                continue
            pending.append(PageWorkItem(run=run, page_num=page_num, image_path=image_path))
    return pending


def is_google_books_boilerplate(image_path: Path) -> bool:
    """Detect Google Books front-matter (English or Spanish scans)."""
    try:
        import pytesseract

        with Image.open(image_path) as img:
            snippet = ""
            for lang in ("eng+spa", "spa+eng", "eng"):
                try:
                    snippet = pytesseract.image_to_string(img, lang=lang)[:3500].lower()
                    break
                except Exception:
                    continue
    except Exception:
        return False
    if not snippet:
        return False
    markers = (
        "google books",
        "books.google",
        "books.google.com",
        "about this book",
        "acerca de este libro",
        "terms of use",
        "normas de uso",
        "public domain",
        "dominio público",
        "non-commercial",
        "no comercial",
        "automated querying",
        "consultas automatizadas",
        "búsqueda de libros de google",
        "book search",
    )
    return sum(1 for m in markers if m in snippet) >= 2


def transcribe_page(
    api_key: str,
    image_path: Path,
    *,
    page_num: int,
    skip_front_pages: int = 0,
    user_prompt: str | None = None,
) -> str:
    if page_should_skip_google_auto(page_num, skip_front_pages, image_path):
        return skip_line("Google boilerplate")
    try:
        return call_claude(api_key, image_path, user_text=user_prompt)
    except RuntimeError as exc:
        if "content filtering" in str(exc).lower() and page_should_skip_google_auto(
            page_num, skip_front_pages, image_path
        ):
            return skip_line("Google boilerplate")
        raise


def call_vision_api(api_key: str, image_path: Path, **kwargs) -> str:
    page_num = kwargs.pop("page_num", 0)
    skip_front_pages = kwargs.pop("skip_front_pages", 0)
    if page_num:
        return transcribe_page(
            api_key, image_path, page_num=page_num, skip_front_pages=skip_front_pages
        )
    return call_claude(api_key, image_path, **kwargs)


def page_transcript_path(work_dir: Path, run: int, page_num: int) -> Path:
    return work_dir / f"run{run}" / "pages" / f"page_{page_num:04d}.txt"


def reconcile_page_path(work_dir: Path, page_num: int) -> Path:
    return work_dir / "reconcile" / "pages" / f"page_{page_num:04d}.txt"


def spot_check_page_path(work_dir: Path, page_num: int) -> Path:
    return work_dir / "spot_check" / "pages" / f"page_{page_num:04d}.txt"


def completed_pages_for_run(state: dict, run: int) -> set[int]:
    runs = state.get("runs") or {}
    return set(runs.get(str(run), {}).get("completed", []))


def mark_page_complete(state: dict, run: int, page_num: int) -> None:
    runs = state.setdefault("runs", {})
    entry = runs.setdefault(str(run), {"completed": []})
    done = set(entry.get("completed", []))
    done.add(page_num)
    entry["completed"] = sorted(done)


def assemble_run_txt(
    work_dir: Path,
    run: int,
    page_numbers: list[int],
    *,
    source_slug: str | None = None,
) -> Path:
    from pdf_transcribe_integrity import read_source_lock, write_sourced_text

    parts: list[str] = []
    for page_num in page_numbers:
        path = page_transcript_path(work_dir, run, page_num)
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        parts.append(f"--- Page {page_num} ---\n{text}")
    out = work_dir / f"run{run}.txt"
    body = "\n\n".join(parts) + "\n"
    slug = source_slug or read_source_lock(work_dir)
    if slug:
        write_sourced_text(out, slug, body)
    else:
        out.write_text(body, encoding="utf-8")
    return out


def parse_pages(text: str) -> dict[int, str]:
    sections: dict[int, str] = {}
    chunks = PAGE_MARKER_RE.split(text)
    if not chunks:
        return sections
    if chunks[0].strip():
        sections[0] = chunks[0].strip()
    i = 1
    while i + 1 < len(chunks):
        num = int(chunks[i])
        body = chunks[i + 1].strip()
        sections[num] = body
        i += 2
    return sections


def char_diff_note(a: str, b: str, c: str) -> str:
    if a == b == c:
        return ""
    max_len = max(len(a), len(b), len(c))
    notes: list[str] = []
    for i in range(max_len):
        ca = a[i] if i < len(a) else ""
        cb = b[i] if i < len(b) else ""
        cc = c[i] if i < len(c) else ""
        if len({ca, cb, cc}) == 1:
            continue
        notes.append(
            f"  col {i + 1}: run1={ca!r} run2={cb!r} run3={cc!r}"
        )
    if not notes and a != b != c:
        notes.append(f"  line lengths: run1={len(a)} run2={len(b)} run3={len(c)}")
    return "\n".join(notes)


def line_diff_preview(a: str, b: str, c: str) -> str:
    pairs = [("run1 vs run2", ndiff(a, b)), ("run1 vs run3", ndiff(a, c)), ("run2 vs run3", ndiff(a, c))]
    lines: list[str] = []
    for label, diff in pairs:
        changed = [d for d in diff if d[:2] != "  "]
        if changed:
            lines.append(f"  {label}: " + "".join(changed)[:200])
    return "\n".join(lines) if lines else ""


def read_run_page_body(work_dir: Path, run: int, page_num: int) -> str:
    path = page_transcript_path(work_dir, run, page_num)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def pages_disagree(body1: str, body2: str) -> bool:
    return body1.strip() != body2.strip()


def pages_need_content_reconcile(
    body1: str, body2: str, lang_cfg=None, *, section: str | None = None
) -> bool:
    from pdf_transcribe_lang import pages_need_content_reconcile as _need

    return _need(body1, body2, lang_cfg, section=section)


def build_reconcile_params(
    image_path: Path,
    model: str,
    run1_text: str,
    run2_text: str,
    lang_cfg=None,
) -> dict:
    norm = ""
    if lang_cfg is not None:
        norm = (lang_cfg.normalization_rule or "").strip()
    user_text = (
        "Reconcile two OCR passes using the page image as the only ground truth.\n\n"
        "When run 1 and run 2 disagree, look at the image and choose whichever reading "
        "matches the image exactly — even if that reading looks wrong or implausible. "
        "Do not use logical reasoning to pick the more plausible reading. "
        "The image is ground truth, not plausibility.\n\n"
        "Transcribe exactly what is printed. Never correct anything. Never normalize anything.\n"
        "Decorative drop capitals at chapter openings are part of the first word — include them "
        '(e.g. "COMO", never "OMO").\n'
    )
    from pdf_transcribe_lang import macron_tilde_prompt_line, notation_prompt_line

    notation_parts = [notation_prompt_line()]
    if lang_cfg is not None and getattr(lang_cfg, "unify_abbreviation_marks", False):
        notation_parts.append(macron_tilde_prompt_line())
    user_text += f"Notation: {' '.join(notation_parts)}\n\n"
    user_text += (
        f"{ARCHIVAL_VERBATIM}\n\n"
        "If the source image shows something that appears to be a printing error, "
        "inconsistency, or unusual convention, transcribe it exactly as it appears on the page. "
        "Do not correct it. If the source itself does not flag it as an error, append [sic] "
        "immediately after it.\n"
        "Do not use logical reasoning to pick the more plausible reading. The image is ground "
        "truth not plausibility. If a footnote is numbered 8 in the image transcribe 8 even if "
        "the in-text superscript is ³.\n\n"
    )
    if norm:
        user_text += f"Language-specific archival rules (auto-detected):\n{norm}\n\n"
    else:
        user_text += (
            "Archival rules (same as transcription):\n"
            "- Period Spanish (fué, á, hácia, inéditas, substituído): transcribe exactly; "
            "never modernize.\n"
            "- Unflagged apparent errors (e.g. footnote \"8\" vs superscript ³): transcribe "
            "as printed; append [sic] after; never silently fix.\n"
            "- Editor-flagged errors (body misprint + footnote \"Debe ser …\"): body and "
            "footnote verbatim; do not merge, fix the body, or add [sic].\n\n"
        )
    user_text += (
        f"RUN 1:\n{run1_text}\n\n"
        f"RUN 2:\n{run2_text}\n\n"
        "Output the single best full-page transcription from the image. "
        "Preserve *italics*, --- FOOTNOTES ---, [illegible], [damaged], and [sic]. "
        "If not confident, add a final line: UNCERTAIN: <reason>\n"
        "No other commentary."
    )
    return build_vision_message_params(image_path, model, user_text=user_text)


def build_spot_patch_params(
    image_path: Path,
    model: str,
    sentence: str,
    terms: list[str],
    lang_cfg,
    *,
    section_hint: str | None = None,
) -> dict:
    from pdf_transcribe_spot import build_patch_prompt

    return build_vision_message_params(
        image_path,
        model,
        user_text=build_patch_prompt(sentence, terms, lang_cfg, section_hint=section_hint),
    )


def build_page_spot_patch_params(
    image_path: Path,
    model: str,
    operations,
    lang_cfg,
    *,
    section_hint: str | None = None,
) -> dict:
    from pdf_transcribe_spot import build_page_patch_prompt, page_spot_output_max_tokens

    return build_vision_message_params(
        image_path,
        model,
        user_text=build_page_patch_prompt(
            operations, lang_cfg, section_hint=section_hint
        ),
        max_tokens=page_spot_output_max_tokens(operations),
    )


def parse_reconcile_output(
    text: str, lang_cfg=None, *, section: str | None = None
) -> tuple[str, bool, str]:
    uncertain = False
    note = ""
    match = UNCERTAIN_RE.search(text)
    if match:
        uncertain = True
        note = match.group(1).strip()
        text = UNCERTAIN_RE.sub("", text).strip()
    return normalize_transcription_output(text, lang_cfg, section=section), uncertain, note


def page_needs_hard_term_spot_check(text: str, terms: list[str], lang_cfg=None) -> bool:
    if is_skip_body(text):
        return False
    from pdf_transcribe_lang import effective_hard_terms, page_has_hard_term

    cfg = lang_cfg or job_config_from_state({})
    all_terms = effective_hard_terms(text, cfg) if lang_cfg else terms
    if not all_terms and terms:
        all_terms = terms
    return page_has_hard_term(text, all_terms)


def generate_differences(work_dir: Path) -> Path:
    from pdf_transcribe_finalize import generate_differences as _gen

    return _gen(work_dir)


ProgressCallback = Callable[[str, int, int, int, float | None], None]


def _init_transcription_job(
    pdf_path: Path,
    *,
    max_pages: int | None,
    skip_front_pages: int,
    work_dir: Path | None,
    language: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    script: str | None = None,
    explicit_pages: list[int] | None = None,
) -> tuple[Path, WorkPages, WorkPageRange, dict]:
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    work_dir = work_dir or work_dir_for_pdf(pdf_path, source_name)
    work_dir.mkdir(parents=True, exist_ok=True)

    from pdf_transcribe_integrity import run_startup_integrity

    from pdf_transcribe_integrity import read_source_lock

    integrity_slug = source_name or source_id or read_source_lock(work_dir) or "unknown"
    integrity, healed_state = run_startup_integrity(work_dir, integrity_slug)
    if integrity.blocking:
        raise RuntimeError(integrity.blocking[0])
    if integrity.fixes:
        write_progress(
            work_dir,
            phase="config_check",
            current_run=0,
            page=0,
            total_pages=0,
            message=integrity.status_label + ": " + "; ".join(integrity.fixes),
        )

    pdf_count = _pdf_page_count(pdf_path)
    page_range = resolve_work_page_range(
        pdf_count,
        skip_front_pages=skip_front_pages,
        max_pages=max_pages,
        explicit_pages=explicit_pages,
    )
    images_dir = work_dir / "images"
    work_pages = render_pdf_pages(
        pdf_path, images_dir, page_numbers=page_range.page_numbers, dpi=DPI
    )
    if len(work_pages) == 0:
        raise RuntimeError("No pages could be rendered from this PDF. Is Poppler installed?")

    job_sig = {
        "pdf": str(pdf_path),
        "page_numbers": page_range.page_numbers,
        "skip_front_pages": page_range.skip_front_pages,
        "max_pages": max_pages,
    }
    settings = load_settings()
    from pdf_transcribe_detect import slugify_source_name
    from pdf_transcribe_lang import normalize_script
    from pdf_transcribe_source import ensure_source_identity

    job_language = (language or settings.get("language") or "spanish").strip().lower()
    job_source = ""
    if source_name:
        job_source = slugify_source_name(source_name)
    elif source_id:
        job_source = source_id.strip().lower().replace(" ", "_")
    job_script = normalize_script(
        (script or settings.get("script")),
        job_language,
    )
    state = healed_state if healed_state else load_state(work_dir)
    if state.get("pdf") != job_sig["pdf"] or state.get("page_numbers") != job_sig["page_numbers"]:
        profile_preserve = {
            k: state[k]
            for k in (
                "detected_source_profile",
                "normalization_rules",
                "hard_terms_file",
                "impossible_strings_file",
                "seed_hard_terms",
                "accuracy_notes",
                "direction",
            )
            if k in state and state[k]
        }
        state = {
            "pdf": job_sig["pdf"],
            "total_pages": page_range.job_page_count,
            "page_numbers": page_range.page_numbers,
            "first_page": page_range.first_page,
            "last_page": page_range.last_page,
            "skip_front_pages": page_range.skip_front_pages,
            "max_pages": max_pages,
            "pdf_page_count": pdf_count,
            "dpi": DPI,
            "language": job_language,
            "source_id": job_source or None,
            "source_name": job_source or None,
            "script": job_script,
            "impossible_strings": [],
            "batch_collisions": [],
            "runs": {str(i): {"completed": []} for i in range(1, NUM_TRANSCRIPTION_RUNS + 1)},
            "reconcile": {"completed": []},
            "spot_check": {"completed": []},
        }
        state.update(profile_preserve)
        save_state(work_dir, state)
    else:
        state["total_pages"] = page_range.job_page_count
        state["page_numbers"] = page_range.page_numbers
        state["first_page"] = page_range.first_page
        state["last_page"] = page_range.last_page
        state["skip_front_pages"] = page_range.skip_front_pages
        state["max_pages"] = max_pages
        state["pdf_page_count"] = pdf_count
        state["language"] = job_language
        if job_source:
            state["source_id"] = job_source
            state["source_name"] = job_source
        state["script"] = job_script
        if (state.get("source_name") or "").strip() and (state.get("source_id") or "").strip():
            ensure_source_identity(state)
        save_state(work_dir, state)
    return work_dir, work_pages, page_range, state


def run_source_detection(
    api_key: str,
    work_dir: Path,
    state: dict,
    page_numbers: list[int],
    source_name: str,
    report: Callable[..., None] | None = None,
    *,
    use_saved: bool = True,
    profile_override: dict | None = None,
) -> dict:
    """Phase 0: detect or load saved profile. Returns profile dict (may need confirmation)."""
    from pdf_transcribe_detect import (
        apply_profile_to_state,
        load_saved_source_profile,
        pick_detection_sample_pages,
        profile_display_lines,
        run_phase0_detection,
        slugify_source_name,
    )

    slug = slugify_source_name(source_name)
    if profile_override:
        prof = {**profile_override, "confirmed": True}
        apply_profile_to_state(state, prof, slug)
        save_state(work_dir, state)
        return state["detected_source_profile"]

    if use_saved:
        saved = load_saved_source_profile(slug)
        if saved:
            saved["confirmed"] = True
            saved["from_saved_config"] = True
            apply_profile_to_state(state, saved, slug)
            save_state(work_dir, state)
            if report:
                report("detecting", 0, 0, 0, None, f"Loaded saved profile for {slug}")
            return state["detected_source_profile"]

    samples = pick_detection_sample_pages(page_numbers)
    images = [work_dir / "images" / f"page_{n:04d}.png" for n in samples if (work_dir / "images" / f"page_{n:04d}.png").is_file()]
    if report:
        report("detecting", 0, 0, len(page_numbers), None, f"Analyzing {len(images)} sample pages…")
    profile = run_phase0_detection(api_key, images)
    profile["languages_display"] = profile.get("languages_raw") or ""
    profile["confirmed"] = False
    state["detected_source_profile"] = profile
    state["source_name"] = slug
    save_state(work_dir, state)
    return profile


def confirm_source_profile(
    work_dir: Path,
    state: dict,
    source_name: str,
    profile: dict,
    *,
    language: str | None = None,
    script: str | None = None,
) -> None:
    from pdf_transcribe_detect import apply_profile_to_state, slugify_source_name
    from pdf_transcribe_lang import build_normalization_rules_text, normalize_script

    prof = dict(profile)
    prof["confirmed"] = True
    if language:
        prof["languages"] = {language: 1.0}
        prof["languages_raw"] = language
    if script:
        prof["script"] = normalize_script(script)
    if prof.get("languages"):
        prof["normalization_rules"] = build_normalization_rules_text(prof["languages"])
    apply_profile_to_state(state, prof, slugify_source_name(source_name))
    save_state(work_dir, state)


def ensure_source_profile_confirmed(
    api_key: str,
    work_dir: Path,
    state: dict,
    page_numbers: list[int],
    source_name: str,
    *,
    use_saved: bool = True,
    auto_confirm: bool = False,
    language: str | None = None,
    script: str | None = None,
    report: Callable[..., None] | None = None,
) -> dict:
    """Phase 0 + optional confirmation. Raises if profile needs user confirmation."""
    profile = state.get("detected_source_profile") or {}
    if profile.get("confirmed"):
        return profile
    profile = run_source_detection(
        api_key,
        work_dir,
        state,
        page_numbers,
        source_name,
        report,
        use_saved=use_saved,
    )
    if profile.get("confirmed"):
        return profile
    if auto_confirm:
        confirm_source_profile(
            work_dir,
            state,
            source_name,
            profile,
            language=language,
            script=script,
        )
        return state["detected_source_profile"]
    raise RuntimeError(
        "Source profile not confirmed. Review detected settings before transcribing."
    )


def _run_batch_chunks(
    api_key: str,
    work_dir: Path,
    state: dict,
    work_pages: WorkPages,
    pending: list[PageWorkItem],
    model: str,
    total_pages: int,
    report: Callable[..., None],
    *,
    processing_mode: str,
    batch_done: int,
    batch_total: int,
    user_prompt: str | None = None,
    label: str = "Batch",
    run_only: int | None = None,
) -> tuple[list[PageWorkItem], int]:
    failed_retries: list[PageWorkItem] = []
    chunks = [pending[i : i + BATCH_MAX_REQUESTS] for i in range(0, len(pending), BATCH_MAX_REQUESTS)]
    done = batch_done
    for chunk_idx, chunk in enumerate(chunks):
        if not chunk:
            continue
        batch_id = create_message_batch(api_key, chunk, model, user_prompt=user_prompt)
        custom_ids = [batch_custom_id(item.run, item.page_num) for item in chunk]
        save_batch_state(
            work_dir,
            {"batch_id": batch_id, "custom_ids": custom_ids, "applied": False, "chunk": chunk_idx + 1},
        )

        def on_batch_status(info: dict, _idx=chunk_idx, _done=done) -> None:
            counts = info.get("request_counts") or {}
            succeeded = int(counts.get("succeeded") or 0)
            processing = int(counts.get("processing") or 0)
            report(
                "batch_waiting",
                0,
                min(_done + succeeded, total_pages),
                total_pages,
                None,
                f"{label} {chunk_idx + 1}/{len(chunks)} — processing ({processing} active)…",
                batch_done=_done + succeeded,
                batch_total=batch_total,
            )

        report(
            "batch_submitting",
            0,
            done,
            total_pages,
            None,
            f"{label}: submitted {chunk_idx + 1}/{len(chunks)} ({len(chunk)} pages)…",
            batch_done=done,
            batch_total=batch_total,
        )
        info = wait_for_batch(api_key, batch_id, on_status=on_batch_status)
        failed = _apply_batch_results(api_key, work_dir, state, batch_id, info, pending_items=chunk)
        failed_retries.extend(failed)
        done = batch_total - len(
            collect_api_work(
                state,
                work_pages,
                skip_front_pages=state.get("skip_front_pages", 0),
                run_only=run_only,
            )
        )
        save_batch_state(work_dir, {"batch_id": batch_id, "custom_ids": custom_ids, "applied": True})
    return failed_retries, done


def _finish_transcription(
    work_dir: Path,
    state: dict,
    total_pages: int,
    report: Callable[..., None],
    *,
    api_key: str | None = None,
    use_batch: bool = True,
) -> Path:
    settings = load_settings()
    model = settings["model"]
    spot_check = settings.get("spot_check_enabled", True)
    if api_key:
        from pdf_transcribe_finalize import finalize_pipeline

        report("finalize", 0, total_pages, total_pages, None, "Reconcile, spot-check, build transcribed.txt…")
        finalize_pipeline(
            api_key,
            work_dir,
            state,
            total_pages,
            model=model,
            use_batch=use_batch,
            spot_check_enabled=spot_check,
            report=report,
        )
    else:
        nums = job_page_numbers(state)
        for run in range(1, NUM_TRANSCRIPTION_RUNS + 1):
            assemble_run_txt(work_dir, run, nums, source_slug=state.get("source_name"))
        generate_differences(work_dir)
    nums = job_page_numbers(state)
    report("done", NUM_TRANSCRIPTION_RUNS, nums[-1] if nums else 0, total_pages, None, "All finished!")
    return work_dir


def run_transcription_realtime(
    pdf_path: Path,
    api_key: str,
    *,
    max_pages: int | None = None,
    work_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
    skip_front_pages: int = DEFAULT_SKIP_FRONT_PAGES,
    processing_mode: str = "realtime",
    language: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    script: str | None = None,
    explicit_pages: list[int] | None = None,
    auto_confirm: bool = False,
    skip_detection: bool = False,
) -> Path:
    work_dir = (work_dir or work_dir_for_pdf(pdf_path.resolve(), source_name)).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    def report(
        phase: str,
        run: int,
        page: int,
        total: int,
        eta: float | None,
        msg: str,
        **extra: int | None,
    ) -> None:
        write_progress(
            work_dir,
            phase=phase,
            current_run=run,
            page=page,
            total_pages=total,
            message=msg,
            eta_seconds=eta,
            processing_mode=processing_mode,
            batch_done=extra.get("batch_done"),
            batch_total=extra.get("batch_total"),
            step_done=extra.get("step_done"),
            step_total=extra.get("step_total"),
        )
        if on_progress:
            on_progress(phase, run, page, total, eta)

    report("rendering", 0, 0, 0, None, "Converting PDF pages to images…")
    work_dir, work_pages, page_range, state = _init_transcription_job(
        pdf_path,
        max_pages=max_pages,
        skip_front_pages=skip_front_pages,
        work_dir=work_dir,
        language=language,
        source_id=source_id,
        source_name=source_name,
        script=script,
        explicit_pages=explicit_pages,
    )
    page_nums = page_range.page_numbers
    total_pages = page_range.job_page_count
    report(
        "rendering",
        0,
        0,
        total_pages,
        None,
        f"PDF pages {page_range.first_page}–{page_range.last_page} ({total_pages} pages)…",
    )
    apply_local_skips(work_dir, state, work_pages, skip_front_pages=skip_front_pages)
    if skip_detection:
        if not (state.get("detected_source_profile") or {}).get("confirmed"):
            raise RuntimeError("Source profile not confirmed. Run detection and confirm before transcribing.")
    else:
        from pdf_transcribe_detect import slugify_source_name

        ensure_source_profile_confirmed(
            api_key,
            work_dir,
            state,
            page_nums,
            source_name or state.get("source_name") or slugify_source_name(pdf_path.stem),
            auto_confirm=auto_confirm,
            language=language,
            script=script,
            report=report,
        )

    tracker = ProgressTracker(
        total_pages=total_pages,
        total_api_calls=total_pages * NUM_RUNS,
    )
    for run in range(1, NUM_RUNS + 1):
        tracker.completed_calls += len(completed_pages_for_run(state, run))

    user_prompt = transcription_user_prompt(work_dir, state)
    job_index = 0
    for run in range(1, NUM_RUNS + 1):
        if run == 2:
            user_prompt = transcription_user_prompt(work_dir, state)
        done = completed_pages_for_run(state, run)
        if len(done) >= total_pages and not (work_dir / f"run{run}.txt").is_file():
            assemble_run_txt(work_dir, run, page_nums, source_slug=state.get("source_name"))
        for page_num, image_path in work_pages:
            if page_num in done:
                continue
            job_index += 1
            if page_should_skip_google_auto(page_num, skip_front_pages, image_path):
                text = skip_line("Google boilerplate")
                tracker.tick()
                report(
                    "transcribing",
                    run,
                    page_num,
                    total_pages,
                    None,
                    f"Skipping Google notice (PDF page {page_num})…",
                )
            else:
                eta = tracker.tick()
                msg = f"Run {run}/{NUM_RUNS} — PDF page {page_num} ({job_index}/{total_pages} in job)"
                report("transcribing", run, page_num, total_pages, eta, msg)
                text = transcribe_page(
                    api_key,
                    image_path,
                    page_num=page_num,
                    skip_front_pages=skip_front_pages,
                    user_prompt=user_prompt,
                )
                time.sleep(API_DELAY_SEC)

            write_page_result(work_dir, state, run, page_num, text)
        if run == 1:
            from pdf_transcribe_detect import run_post_run1_term_pipeline

            report("extracting", 1, total_pages, total_pages, None, "Extracting hard terms from run 1…")
            run_post_run1_term_pipeline(api_key, work_dir, state)
            save_state(work_dir, state)

    return _finish_transcription(
        work_dir, state, total_pages, report, api_key=api_key, use_batch=False
    )


def run_transcription_batch(
    pdf_path: Path,
    api_key: str,
    *,
    max_pages: int | None = None,
    work_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
    skip_front_pages: int = DEFAULT_SKIP_FRONT_PAGES,
    processing_mode: str = "batch",
    language: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    script: str | None = None,
    explicit_pages: list[int] | None = None,
    auto_confirm: bool = False,
    skip_detection: bool = False,
) -> Path:
    settings = load_settings()
    model = settings["model"]
    work_dir = (work_dir or work_dir_for_pdf(pdf_path.resolve(), source_name)).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    batch_done = 0
    batch_total = 0

    def report(
        phase: str,
        run: int,
        page: int,
        total: int,
        eta: float | None,
        msg: str,
        **extra: int | None,
    ) -> None:
        write_progress(
            work_dir,
            phase=phase,
            current_run=run,
            page=page,
            total_pages=total,
            message=msg,
            eta_seconds=eta,
            processing_mode=processing_mode,
            batch_done=extra.get("batch_done", batch_done),
            batch_total=extra.get("batch_total", batch_total),
            step_done=extra.get("step_done"),
            step_total=extra.get("step_total"),
        )
        if on_progress:
            on_progress(phase, run, page, total, eta)

    report("rendering", 0, 0, 0, None, "Converting PDF pages to images…")
    work_dir, work_pages, page_range, state = _init_transcription_job(
        pdf_path,
        max_pages=max_pages,
        skip_front_pages=skip_front_pages,
        work_dir=work_dir,
        language=language,
        source_id=source_id,
        source_name=source_name,
        script=script,
        explicit_pages=explicit_pages,
    )
    total_pages = page_range.job_page_count
    image_by_page = {n: p for n, p in work_pages}
    report(
        "rendering",
        0,
        0,
        total_pages,
        None,
        f"PDF pages {page_range.first_page}–{page_range.last_page} ({total_pages} pages)…",
    )
    apply_local_skips(work_dir, state, work_pages, skip_front_pages=skip_front_pages)

    if skip_detection:
        if not (state.get("detected_source_profile") or {}).get("confirmed"):
            raise RuntimeError("Source profile not confirmed. Run detection and confirm before transcribing.")
    else:
        from pdf_transcribe_detect import slugify_source_name

        ensure_source_profile_confirmed(
            api_key,
            work_dir,
            state,
            page_range.page_numbers,
            source_name or state.get("source_name") or slugify_source_name(pdf_path.stem),
            auto_confirm=auto_confirm,
            language=language,
            script=script,
            report=report,
        )

    user_prompt = transcription_user_prompt(work_dir, state)
    failed_retries: list[PageWorkItem] = []

    for run_pass in (1, 2):
        pending = collect_api_work(
            state, work_pages, skip_front_pages=skip_front_pages, run_only=run_pass
        )
        batch_total = len(pending)
        batch_done = 0
        if not pending:
            continue
        if run_pass == 2:
            user_prompt = transcription_user_prompt(work_dir, state)
        report(
            "batch_submitting",
            run_pass,
            0,
            total_pages,
            None,
            f"Run {run_pass} — {batch_total} pages queued (batch 50% off)…",
            batch_done=batch_done,
            batch_total=batch_total,
        )
        fr, batch_done = _run_batch_chunks(
            api_key,
            work_dir,
            state,
            work_pages,
            pending,
            model,
            total_pages,
            report,
            processing_mode=processing_mode,
            batch_done=batch_done,
            batch_total=batch_total,
            user_prompt=user_prompt,
            label=f"Run {run_pass}",
            run_only=run_pass,
        )
        failed_retries.extend(fr)
        if run_pass == 1:
            from pdf_transcribe_detect import run_post_run1_term_pipeline

            report("extracting", 1, total_pages, total_pages, None, "Extracting hard terms from run 1…")
            run_post_run1_term_pipeline(api_key, work_dir, state)
            save_state(work_dir, state)

    if failed_retries:
        prompt = transcription_user_prompt(work_dir, state)
        report(
            "transcribing",
            0,
            batch_done,
            total_pages,
            None,
            f"Retrying {len(failed_retries)} failed pages on live API…",
            batch_done=batch_done,
            batch_total=batch_total,
        )
        for item in failed_retries:
            try:
                text = transcribe_page(
                    api_key,
                    item.image_path,
                    page_num=item.page_num,
                    skip_front_pages=skip_front_pages,
                    user_prompt=prompt,
                )
            except RuntimeError as exc:
                text = f"[Transcription failed: {exc}]"
            write_page_result(work_dir, state, item.run, item.page_num, text)
            time.sleep(API_DELAY_SEC)

    from pdf_transcribe_sanity import run_batch_content_sanity_pass

    sanity_prompt = transcription_user_prompt(work_dir, state)
    collisions = run_batch_content_sanity_pass(
        api_key,
        work_dir,
        state,
        page_range.page_numbers,
        image_by_page,
        script=state.get("script", "latin"),
        skip_front_pages=skip_front_pages,
        is_skip_fn=is_skip_body,
        read_page_fn=read_run_page_body,
        write_page_fn=write_page_result,
        transcribe_fn=transcribe_page,
        report=report,
        api_delay_sec=API_DELAY_SEC,
        user_prompt=sanity_prompt,
    )
    if collisions:
        state["batch_collisions"] = list(state.get("batch_collisions") or []) + collisions
        save_state(work_dir, state)

    return _finish_transcription(
        work_dir, state, total_pages, report, api_key=api_key, use_batch=True
    )


def _apply_batch_results(
    api_key: str,
    work_dir: Path,
    state: dict,
    batch_id: str,
    batch_info: dict,
    *,
    pending_items: list[PageWorkItem] | None,
) -> list[PageWorkItem]:
    """Write batch results to disk; return items that need a live retry."""
    by_id = {
        batch_custom_id(item.run, item.page_num): item
        for item in (pending_items or [])
    }
    failed: list[PageWorkItem] = []
    for line in iter_batch_results(api_key, batch_id, batch_info):
        custom_id, text, err = parse_batch_result_line(line)
        if not custom_id:
            continue
        try:
            kind, run, page_num = parse_batch_custom_id(custom_id)
        except ValueError:
            continue
        if text is not None:
            if kind == "transcribe":
                write_page_result(work_dir, state, run, page_num, text)
            elif kind == "reconcile":
                reconcile_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
                reconcile_page_path(work_dir, page_num).write_text(text, encoding="utf-8")
            elif kind == "spot":
                spot_check_page_path(work_dir, page_num).parent.mkdir(parents=True, exist_ok=True)
                spot_check_page_path(work_dir, page_num).write_text(text, encoding="utf-8")
            continue
        item = by_id.get(custom_id)
        if item:
            failed.append(item)
        elif kind == "transcribe":
            write_page_result(
                work_dir,
                state,
                run,
                page_num,
                f"[Batch error: {err or 'unknown'}]",
            )
    return failed


def run_transcription(
    pdf_path: Path,
    api_key: str,
    *,
    max_pages: int | None = None,
    work_dir: Path | None = None,
    on_progress: ProgressCallback | None = None,
    skip_front_pages: int = DEFAULT_SKIP_FRONT_PAGES,
    processing_mode: str | None = None,
    language: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    script: str | None = None,
    explicit_pages: list[int] | None = None,
    auto_confirm: bool = False,
    skip_detection: bool = False,
) -> Path:
    mode = resolve_processing_mode(processing_mode, max_pages=max_pages)
    label = processing_mode_label(processing_mode or load_settings().get("processing_mode"), max_pages=max_pages)
    common = dict(
        max_pages=max_pages,
        work_dir=work_dir,
        on_progress=on_progress,
        skip_front_pages=skip_front_pages,
        language=language,
        source_id=source_id,
        source_name=source_name,
        script=script,
        explicit_pages=explicit_pages,
        auto_confirm=auto_confirm,
        skip_detection=skip_detection,
    )
    if mode == "batch":
        return run_transcription_batch(
            pdf_path,
            api_key,
            processing_mode=f"batch — {label}",
            **common,
        )
    return run_transcription_realtime(
        pdf_path,
        api_key,
        processing_mode=f"live — {label}",
        **common,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe a scanned PDF with Claude Opus (2 passes + reconcile + spot-check)."
    )
    parser.add_argument("pdf", type=Path, help="Path to your PDF file")
    parser.add_argument(
        "api_key",
        nargs="?",
        default=None,
        help="Claude API key (optional if already saved)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N pages (e.g. 10 for a test run)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to save results (default: <pdf_name>_transcribe_output next to the PDF)",
    )
    parser.add_argument(
        "--skip-front-pages",
        type=int,
        default=DEFAULT_SKIP_FRONT_PAGES,
        metavar="N",
        help="Skip first N pages (default 2 for Google Books notice in EN+ES)",
    )
    parser.add_argument(
        "--processing",
        choices=PROCESSING_MODES,
        default=None,
        help="auto = live API for --max-pages tests, batch (50%% off) for full book",
    )
    parser.add_argument(
        "--no-spot-check",
        action="store_true",
        help="Disable sentence-level spot patches on hard-name pages",
    )
    parser.add_argument(
        "--source-name",
        default=None,
        metavar="NAME",
        help="Source id for auto-detected profile and per-source config (default: PDF filename)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Auto-confirm detected source profile without prompting (CLI only)",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language override (default: auto-detect from sample pages)",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        metavar="ID",
        help="Per-source config id (hard_terms_<ID>.txt, default: ixtlilxochitl)",
    )
    parser.add_argument(
        "--pages",
        default=None,
        metavar="LIST",
        help="Explicit PDF page numbers (e.g. 45,60,120-125). Overrides --max-pages.",
    )
    parser.add_argument(
        "--script",
        default=None,
        choices=sorted({"latin", "arabic", "korean", "japanese", "chinese"}),
        help="Expected output script for sanity checks (default: from language)",
    )
    parser.add_argument(
        "--reset-source",
        action="store_true",
        help="Clear work-dir outputs for this source (keeps config); then exit",
    )
    args = parser.parse_args(argv)
    try:
        api_key = resolve_api_key(args.api_key)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (args.api_key or "").strip():
        save_settings(api_key=api_key)
    if args.no_spot_check:
        save_settings(spot_check_enabled=False)
    if args.language or args.source_id or args.script:
        save_settings(
            language=args.language,
            source_id=args.source_id,
            script=args.script,
        )

    def cli_progress(phase: str, run: int, page: int, total: int, eta: float | None) -> None:
        if phase == "rendering":
            print("Converting PDF to images at 300 DPI…")
            return
        if phase == "transcribing":
            eta_s = f" · ~{int(eta)}s left" if eta is not None else ""
            print(f"  [{phase}] Run {run}/2 · page {page}/{total}{eta_s}", flush=True)
        elif phase in ("assembling", "differences", "done"):
            print(f"  {phase}…", flush=True)

    print(
        f"  Processing: {processing_mode_label(args.processing, max_pages=args.max_pages)}",
        flush=True,
    )

    explicit_pages = parse_page_list(args.pages) if args.pages else None
    from pdf_transcribe_detect import slugify_source_name
    from pdf_transcribe_integrity import reset_source_work_dir

    source_name = args.source_name or slugify_source_name(args.pdf.stem)
    out_dir = args.output_dir or work_dir_for_pdf(args.pdf.resolve(), source_name)
    if args.reset_source:
        deleted = reset_source_work_dir(out_dir, source_name)
        print(f"Reset {source_name} in {out_dir}")
        for item in deleted:
            print(f"  deleted: {item}")
        if not deleted:
            print("  (nothing to delete)")
        return 0

    work_dir = run_transcription(
        args.pdf,
        api_key,
        max_pages=None if explicit_pages else args.max_pages,
        work_dir=args.output_dir or out_dir,
        on_progress=cli_progress,
        skip_front_pages=max(0, args.skip_front_pages),
        processing_mode=args.processing,
        language=args.language,
        source_id=args.source_id,
        source_name=source_name,
        script=args.script,
        explicit_pages=explicit_pages,
        auto_confirm=args.yes,
    )
    print(f"\nDone! Your files are here:\n  {work_dir}")
    print("  run1.txt  run2.txt  transcribed.txt  reconcile_log.txt  run_summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
