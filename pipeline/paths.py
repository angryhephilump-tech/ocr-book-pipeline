"""Resolve bundled tool paths for dev, vendor/, and installed layouts."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Installed: Archive Studios/Archive Studios.exe with sibling tesseract/, poppler/, models/
# Dev repo: vendor/tesseract/, vendor/poppler/, vendor/models/
# Frozen (PyInstaller one-folder): same as installed — exe dir is APP_ROOT.


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Templates/static for PyInstaller (_internal) vs dev repo root."""
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        internal = exe_dir / "_internal"
        if internal.is_dir():
            return internal
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return exe_dir
    return app_root()


def _bundle_dir(name: str) -> Path | None:
    root = app_root()
    for candidate in (root / name, root / "vendor" / name):
        if candidate.is_dir():
            return candidate
    return None


def tesseract_exe() -> Path | None:
    base = _bundle_dir("tesseract")
    if base:
        for rel in ("tesseract.exe", "bin/tesseract.exe"):
            path = base / rel
            if path.is_file():
                return path
    for fallback in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ):
        if fallback.is_file():
            return fallback
    found = shutil.which("tesseract")
    return Path(found) if found else None


def tessdata_dir() -> Path | None:
    base = _bundle_dir("tesseract")
    if base:
        for rel in ("tessdata", "share/tessdata", "tessdata_best"):
            path = base / rel
            if path.is_dir() and any(path.glob("*.traineddata")):
                return path
    prefix = os.environ.get("TESSDATA_PREFIX", "").strip()
    if prefix:
        path = Path(prefix)
        if path.is_dir():
            return path
    for fallback in (
        Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
        Path(os.environ.get("APPDATA", "")) / "tesseract",
    ):
        if fallback.is_dir() and any(fallback.glob("*.traineddata")):
            return fallback
    return None


def poppler_bin_dir() -> Path | None:
    base = _bundle_dir("poppler")
    if base:
        for rel in ("bin", "Library/bin", "poppler/bin"):
            path = base / rel
            if path.is_dir() and any(path.glob("pdftoppm*")):
                return path
        if any(base.glob("pdftoppm*")):
            return base
    found = shutil.which("pdftoppm")
    if found:
        return Path(found).parent
    return None


def paddlex_home() -> Path | None:
    root = app_root()
    for candidate in (
        root / "paddlex",
        root / "vendor" / "paddlex",
        root / "models" / "paddlex",
    ):
        official = candidate / "official_models"
        if official.is_dir() and any(official.iterdir()):
            return candidate
        if candidate.is_dir() and any(candidate.iterdir()):
            return candidate
    return None


def paddlex_models_root() -> Path | None:
    home = paddlex_home()
    if not home:
        return None
    official = home / "official_models"
    return official if official.is_dir() else home


def paddle_model_dirs() -> dict[str, Path | None]:
    root = app_root()
    for models_root in (root / "models", root / "vendor" / "models"):
        if not models_root.is_dir():
            continue
        det = models_root / "det"
        rec = models_root / "rec"
        cls = models_root / "cls"
        if det.is_dir() and rec.is_dir():
            return {
                "det_model_dir": det,
                "rec_model_dir": rec,
                "cls_model_dir": cls if cls.is_dir() else None,
            }
    return {"det_model_dir": None, "rec_model_dir": None, "cls_model_dir": None}


def bundled_tools_status() -> dict[str, bool]:
    models = paddle_model_dirs()
    return {
        "tesseract_bundled": _bundle_dir("tesseract") is not None,
        "poppler_bundled": _bundle_dir("poppler") is not None,
        "models_prebundled": (
            paddlex_models_root() is not None
            or (models["det_model_dir"] is not None and models["rec_model_dir"] is not None)
        ),
    }


def configure_runtime() -> None:
    """Point OCR libraries at bundled tools when present."""
    tess_exe = tesseract_exe()
    if tess_exe:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = str(tess_exe)

    tessdata = tessdata_dir()
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = str(tessdata)

    pdx = paddlex_home()
    if pdx:
        os.environ["PADDLE_PDX_HOME"] = str(pdx)

    poppler = poppler_bin_dir()
    if poppler:
        os.environ["POPPLER_PATH"] = str(poppler)
        path_entries = [str(poppler)]
        existing = os.environ.get("PATH", "")
        if str(poppler) not in existing:
            os.environ["PATH"] = os.pathsep.join(path_entries + ([existing] if existing else []))
