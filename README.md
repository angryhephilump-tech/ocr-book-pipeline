# OCR Book Pipeline

Local review workflow with DeepSeek OCR (via Archive Gateway) for physical book photos and PDFs.
Built for **100% accurate final output** — nothing is auto-corrected, and any engine
disagreement or low confidence goes to human review.

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — full system map (Archive Studios, Gateway, PDF Transcribe, all modules)
- **[docs/PDF_TRANSCRIBE_PIPELINE.md](docs/PDF_TRANSCRIBE_PIPELINE.md)** — PDF Transcribe v3 step-by-step operator guide

## Quick start

```powershell
cd ocr-book-pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Start Archive Gateway (required)

Set environment variables:

- `ARCHIVE_GUMROAD_PRODUCT_ID`
- `ARCHIVE_HF_TOKEN`

Then run:

```powershell
.\scripts\run_gateway.ps1
```

### Optional: PDF input

```powershell
pip install pdf2image
```

Install Poppler for Windows and add poppler bin to PATH.

## Usage

### 1. Put photos in `photos/` (or point to any folder)

Original files are **never modified**.

### 2. Run the pipeline (3 DeepSeek passes per page)

```powershell
python ocr_book.py .\photos .\output
```

| Run | Image | Engine |
|-----|-------|--------|
| A | Original | DeepSeek |
| B | Original (sampling variation) | DeepSeek |
| C | Light preprocess | DeepSeek |

### 3. Launch review UI

```powershell
python review_ui.py .\output
```

Open http://127.0.0.1:5050

**Keyboard shortcuts:** J/K flags, Enter accept suggestion, N/P pages, S save

### 4. Export

Click **Export** in the UI → `book_final.pdf` and `book_final.txt`

## Absolute rules

- Never modify originals
- Never auto-correct any language
- Never silently drop content — `[IMAGE]` placeholders
- Never auto-accept by majority vote
- Footnotes and images always flagged

## Language config

Edit `config/languages.json` or `output/project.json`.
