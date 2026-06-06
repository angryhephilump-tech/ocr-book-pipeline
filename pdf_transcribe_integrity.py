"""Startup validation, work-dir isolation, pilot gate, and self-healing config."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from pdf_transcribe_detect import slugify_source_name, source_config_path
from pdf_transcribe_lang import CONFIG_DIR
from pdf_transcribe_source import direction_for_script, resolve_source_slug

SOURCE_HEADER_RE = re.compile(
    r"^#\s*source:\s*(\S+)\s*\|\s*run_date:\s*(\S+)",
    re.IGNORECASE,
)
SOURCE_LOCK_FILE = "source_lock.txt"
PILOT_PAGE_THRESHOLD = 10


class WorkDirSourceMismatchError(RuntimeError):
    """Work directory belongs to a different source."""


@dataclass
class IntegrityReport:
    ok: bool
    fixes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        if self.blocking:
            return "Config issues — cannot proceed"
        if self.fixes:
            return "Config issues found and auto-fixed"
        return "Config OK"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok and not self.blocking,
            "status": self.status_label,
            "fixes": self.fixes,
            "warnings": self.warnings,
            "blocking": self.blocking,
        }


def source_config_dir(slug: str) -> Path:
    return CONFIG_DIR / "sources" / slugify_source_name(slug)


def impossible_strings_file(slug: str) -> Path:
    return source_config_dir(slug) / "impossible_strings.txt"


def hard_terms_file_in_source_dir(slug: str) -> Path:
    return source_config_dir(slug) / "hard_terms.txt"


def source_lock_path(work_dir: Path) -> Path:
    return work_dir / SOURCE_LOCK_FILE


def read_source_lock(work_dir: Path) -> str:
    path = source_lock_path(work_dir)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()


def write_source_lock(work_dir: Path, source_name: str) -> None:
    slug = slugify_source_name(source_name)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_lock_path(work_dir).write_text(f"{slug}\n", encoding="utf-8")


def check_source_lock(work_dir: Path, source_name: str) -> str | None:
    """Return locked source slug if it differs from current source."""
    locked = read_source_lock(work_dir)
    if not locked:
        return None
    current = slugify_source_name(source_name)
    if locked.lower() != current.lower():
        return locked
    return None


def work_dir_for_source(
    pdf_path: Path,
    source_name: str | None = None,
    *,
    uploads_root: Path | None = None,
    custom_work_dir: str | None = None,
) -> Path:
    if custom_work_dir:
        return Path(custom_work_dir).resolve()
    pdf_path = pdf_path.resolve()
    if source_name:
        slug = slugify_source_name(source_name)
        root = uploads_root or pdf_path.parent
        return root / slug / f"{pdf_path.stem}_output"
    return pdf_path.parent / f"{pdf_path.stem}_transcribe_output"


def work_dir_contains_source_name(work_dir: Path | str, source_name: str) -> bool:
    slug = slugify_source_name(source_name).lower()
    parts = Path(work_dir).resolve().parts
    return any(slug in p.lower() for p in parts)


def source_file_header(slug: str, run_date: str | None = None) -> str:
    d = run_date or date.today().isoformat()
    return f"# source: {slugify_source_name(slug)} | run_date: {d}"


def parse_source_header(text: str) -> tuple[str, str] | None:
    for line in text.splitlines()[:3]:
        m = SOURCE_HEADER_RE.match(line.strip())
        if m:
            return m.group(1).lower(), m.group(2)
    return None


def write_sourced_text(path: Path, slug: str, body: str, run_date: str | None = None) -> None:
    header = source_file_header(slug, run_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}\n\n{body.lstrip()}", encoding="utf-8")


def verify_run_file_headers(work_dir: Path) -> list[str]:
    """Return human-readable mismatches between run1/run2/transcribed headers."""
    files = {
        "run1.txt": work_dir / "run1.txt",
        "run2.txt": work_dir / "run2.txt",
        "transcribed.txt": work_dir / "transcribed.txt",
    }
    parsed: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    for label, path in files.items():
        if not path.is_file():
            continue
        header = parse_source_header(path.read_text(encoding="utf-8"))
        if not header:
            missing.append(f"{label}: missing source header")
            continue
        parsed[label] = header
    if len(parsed) < 2:
        return []
    sources = {v[0] for v in parsed.values()}
    if len(sources) > 1:
        detail = ", ".join(f"{k}={v[0]}" for k, v in parsed.items())
        return [f"Source header mismatch: {detail}"]
    return []


def _hard_terms_auto_path(slug: str) -> Path:
    return CONFIG_DIR / f"hard_terms_auto_{slug}.txt"


def _impossible_auto_legacy(slug: str) -> Path:
    return CONFIG_DIR / f"impossible_auto_{slug}.txt"


def read_impossible_strings_file(path: Path, expected_slug: str) -> list[str] | None:
    """Load impossible strings; None if header wrong or file missing."""
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    first = lines[0].strip()
    m = re.match(r"^#\s*source:\s*(\S+)", first, re.IGNORECASE)
    if m:
        if m.group(1).lower() != slugify_source_name(expected_slug).lower():
            return None
        data_lines = lines[1:]
    else:
        return None
    terms: list[str] = []
    for line in data_lines:
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def write_impossible_strings_file(slug: str, terms: list[str], *, note: str = "") -> Path:
    path = impossible_strings_file(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        source_file_header(slug),
        f"# {note}" if note else "# Auto-generated corrupted variants (this source only)",
    ]
    lines.extend(terms)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_impossible_strings_for_source(slug: str, state: dict | None = None) -> list[str]:
    """Strict per-source load — never cross-load; zero if missing or header wrong."""
    from pdf_transcribe_source import load_impossible_extra

    out: list[str] = []
    seen: set[str] = set()

    def add(items: list[str]) -> None:
        for t in items:
            k = t.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(t.strip())

    if state and state.get("impossible_strings") is not None:
        add(list(state["impossible_strings"]))

    primary = impossible_strings_file(slug)
    loaded = read_impossible_strings_file(primary, slug)
    if loaded is not None:
        add(loaded)
    add(load_impossible_extra(slug))
    return out


def _file_date(path: Path) -> str | None:
    if not path.is_file():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()


def _check_term_file_dates(slug: str, report: IntegrityReport) -> bool:
    """True if impossible/hard terms look contaminated (different generation dates)."""
    hard = _hard_terms_auto_path(slug)
    imp_new = impossible_strings_file(slug)
    imp_old = _impossible_auto_legacy(slug)
    imp = imp_new if imp_new.is_file() else imp_old
    if not hard.is_file() or not imp.is_file():
        return False
    hd = _file_date(hard)
    id_ = _file_date(imp)
    if hd and id_ and hd != id_:
        report.fixes.append(
            f"Hard terms ({hd}) and impossible strings ({id_}) generated on different dates — "
            "cleared for regeneration"
        )
        hard.unlink(missing_ok=True)
        imp.unlink(missing_ok=True)
        if imp_new.is_file() and imp != imp_new:
            imp_new.unlink(missing_ok=True)
        return True
    return False


def heal_stale_state(work_dir: Path, source_name: str) -> IntegrityReport:
    """Delete state.json when source_id != source_name."""
    report = IntegrityReport(ok=True)
    path = work_dir / "state.json"
    if not path.is_file():
        return report
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        report.fixes.append("Corrupt state.json deleted — starting fresh.")
        return report
    name = (state.get("source_name") or "").strip()
    sid = (state.get("source_id") or "").strip()
    if name and sid and name != sid:
        path.unlink(missing_ok=True)
        report.fixes.append(
            f"Stale state from different source ({sid!r} vs {name!r}) — cleared automatically. Starting fresh."
        )
    return report


def run_startup_integrity(
    work_dir: Path,
    source_name: str,
    *,
    state: dict | None = None,
) -> tuple[IntegrityReport, dict]:
    """
    Run before any API calls. Heals what it can; returns (report, state_or_empty).
    """
    slug = slugify_source_name(source_name)
    report = IntegrityReport(ok=True)

    locked = check_source_lock(work_dir, source_name)
    if locked:
        report.blocking.append(
            f"This folder belongs to '{locked}'. Choose a different output folder or "
            f"click Reset to clear it for '{slug}'."
        )
        report.ok = False
        return report, state or {}

    report.fixes.extend(heal_stale_state(work_dir, source_name).fixes)

    path = work_dir / "state.json"
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = state or {}

    if state:
        script = (state.get("script") or "latin").strip().lower()
        direction = (state.get("direction") or "ltr").strip().lower()
        fixed_dir = direction_for_script(script, direction)
        if direction != fixed_dir:
            state["direction"] = fixed_dir
            report.fixes.append("RTL overridden to LTR — Latin script sources are always LTR")
            save_state(work_dir, state)

        name = (state.get("source_name") or "").strip()
        sid = (state.get("source_id") or "").strip()
        if name and sid and name != sid:
            path.unlink(missing_ok=True)
            state = {}
            report.fixes.append("Stale state from different source — cleared automatically. Starting fresh.")

    contaminated = _check_term_file_dates(slug, report)
    if contaminated and state:
        state.pop("impossible_strings", None)
        state.pop("hard_terms", None)
        save_state(work_dir, state)

    imp_path = impossible_strings_file(slug)
    if imp_path.is_file():
        if read_impossible_strings_file(imp_path, slug) is None:
            imp_path.unlink(missing_ok=True)
            report.fixes.append(
                f"Impossible strings file header did not match {slug} — deleted for regeneration"
            )
            if state:
                state.pop("impossible_strings", None)
                save_state(work_dir, state)

    write_source_lock(work_dir, source_name)
    report.ok = not report.blocking
    return report, state


def save_state(work_dir: Path, state: dict) -> None:
    from pdf_transcribe import save_state as _save

    _save(work_dir, state)


def reset_source_work_dir(work_dir: Path, source_name: str, *, keep_config: bool = True) -> list[str]:
    """Delete run outputs; keep config. Returns log of deleted paths."""
    slug = slugify_source_name(source_name)
    deleted: list[str] = []
    if not work_dir.is_dir():
        return deleted

    for rel in (
        "state.json",
        "batch_state.json",
        "progress.json",
        "run1.txt",
        "run2.txt",
        "transcribed.txt",
        "differences.txt",
        "reconcile_log.txt",
        "spot_patch_log.txt",
        "run_summary.txt",
        "run_summary.json",
    ):
        p = work_dir / rel
        if p.is_file():
            p.unlink()
            deleted.append(str(p))

    for sub in ("run1", "run2", "reconcile", "spot_check", "images"):
        p = work_dir / sub
        if p.is_dir():
            shutil.rmtree(p)
            deleted.append(str(p))

    write_source_lock(work_dir, source_name)
    return deleted


def load_source_pages_processed(slug: str) -> int:
    path = source_config_path(slug)
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("pages_processed") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def pilot_gate_status(source_name: str) -> dict:
    slug = slugify_source_name(source_name)
    pages = load_source_pages_processed(slug)
    return {
        "source_name": slug,
        "pages_processed": pages,
        "pilot_complete": pages >= PILOT_PAGE_THRESHOLD,
        "full_book_unlocked": pages >= PILOT_PAGE_THRESHOLD,
    }


@dataclass
class PilotCheck:
    name: str
    passed: bool
    detail: str
    auto_fix: str | None = None


def evaluate_pilot_checks(work_dir: Path, state: dict, stats: dict | None = None) -> dict:
    """Green/red checklist after a pilot run."""
    slug = resolve_source_slug(state) if state else ""
    checks: list[PilotCheck] = []

    collisions = (state or {}).get("batch_collisions") or []
    checks.append(
        PilotCheck(
            "No batch collisions",
            len(collisions) == 0,
            f"{len(collisions)} collision(s)" if collisions else "OK",
        )
    )

    script = (state or {}).get("script", "latin")
    direction = (state or {}).get("direction", "ltr")
    dir_ok = direction_for_script(script, direction) == direction
    checks.append(
        PilotCheck(
            "Direction detected correctly",
            dir_ok,
            f"{direction.upper()} ({script} script)",
            "fix_direction" if not dir_ok else None,
        )
    )

    name = (state or {}).get("source_name", "")
    sid = (state or {}).get("source_id", "")
    config_ok = not name or not sid or name == sid
    checks.append(
        PilotCheck(
            "Source config matches",
            config_ok,
            f"source_name={name!r}, source_id={sid!r}",
            "reset_state" if not config_ok else None,
        )
    )

    imp_count = len(load_impossible_strings_for_source(slug, state)) if slug else 0
    imp_ok = imp_count <= 200
    checks.append(
        PilotCheck(
            "Impossible strings count reasonable",
            imp_ok,
            f"{imp_count} entries" + (" (>200)" if not imp_ok else ""),
            "regenerate_impossible" if not imp_ok else None,
        )
    )

    applied = int((stats or {}).get("spot_patches_applied") or 0)
    rejected = int((stats or {}).get("spot_patches_rejected") or 0)
    total_patches = applied + rejected
    reject_rate = (rejected / total_patches) if total_patches else 0.0
    patch_ok = total_patches == 0 or reject_rate <= 0.8
    checks.append(
        PilotCheck(
            "Spot patch rejection rate reasonable",
            patch_ok,
            f"{rejected}/{total_patches} rejected ({reject_rate:.0%})" if total_patches else "No patches",
        )
    )

    human = (stats or {}).get("human_review_pages") or []
    checks.append(
        PilotCheck(
            "Human review pages: 0",
            len(human) == 0,
            f"{len(human)} page(s): {human}" if human else "OK",
        )
    )

    header_issues = verify_run_file_headers(work_dir) if work_dir.is_dir() else []
    checks.append(
        PilotCheck(
            "Run file source headers match",
            len(header_issues) == 0,
            "; ".join(header_issues) if header_issues else "OK",
        )
    )

    all_pass = all(c.passed for c in checks)
    pages_this_run = int((stats or {}).get("total_pages") or 0)
    historical = load_source_pages_processed(slug)
    pilot_pages = max(pages_this_run, historical)
    return {
        "all_passed": all_pass,
        "pilot_complete": pilot_pages >= PILOT_PAGE_THRESHOLD,
        "full_book_unlocked": all_pass and pilot_pages >= PILOT_PAGE_THRESHOLD,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
                "auto_fix": c.auto_fix,
            }
            for c in checks
        ],
    }


def log_soft_term_promotions(work_dir: Path, slug: str, before: set[str], after: list[str]) -> None:
    new_terms = [t for t in after if t.lower() not in before]
    if not new_terms:
        return
    msg = f"Promoted to soft terms (10+ patches, 0 corrections): {', '.join(new_terms)}."
    log_path = work_dir / "run_summary.txt"
    if log_path.is_file():
        log_path.write_text(log_path.read_text(encoding="utf-8") + f"\n{msg}\n", encoding="utf-8")
