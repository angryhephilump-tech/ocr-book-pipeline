# Vendor folder (dev layout)

Place offline-bundled binaries here for local testing before they ship inside the installer.

```
vendor/
├── tesseract/
│   ├── tesseract.exe          (or bin/tesseract.exe)
│   └── tessdata/              (*.traineddata — spa, eng, etc.)
├── poppler/
│   └── bin/                   (pdftoppm.exe, pdfinfo.exe, …)
└── models/
    ├── det/                   Paddle detection model
    ├── rec/                   Paddle recognition model
    └── cls/                   Paddle angle classifier (optional)
```

Installed customer layout uses the same names at the app root (sibling of `Archive Studios.exe`), not under `vendor/`.

## Populate for dev

**Tesseract** — copy from `C:\Program Files\Tesseract-OCR\` into `vendor/tesseract/`.

**Poppler** — copy the `Library\bin` folder from your winget Poppler install into `vendor/poppler/bin/`.

**Paddle models** — run from repo root:

```powershell
.\.venv\Scripts\python.exe scripts\download_paddle_models.py
```

This pre-downloads models and copies them into `vendor/models/`.
