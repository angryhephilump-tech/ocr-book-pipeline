# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Archive Studios (one-folder layout)."""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPEC).resolve().parent.parent

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "config"), "config"),
]

hiddenimports = [
    "paddle",
    "paddleocr",
    "paddleocr.tools",
    "pytesseract",
    "cv2",
    "flask",
    "werkzeug",
    "PIL",
    "numpy",
    "fpdf",
    "pdf2image",
    "pypdf",
    "pipeline",
    "pipeline.consensus",
    "pipeline.export",
    "pipeline.language",
    "pipeline.layout",
    "pipeline.ocr_engines",
    "pipeline.paths",
    "pipeline.pdf_loader",
    "pipeline.preprocess",
    "pipeline.web_jobs",
    "ocr_book",
    "license",
]

a = Analysis(
    [str(ROOT / "review_ui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Archive Studios",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Archive Studios",
)
