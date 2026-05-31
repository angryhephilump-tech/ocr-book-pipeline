# OCR Book Pipeline

Local OCR extraction and human review for physical book photos and online PDFs.
Built for **100% accurate final output** — nothing is auto-corrected, and any engine
disagreement or low confidence goes to human review.

## Quick start

```powershell
cd ocr-book-pipeline
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Install Tesseract (required)

1. Download installer: https://github.com/UB-Mannheim/tesseract/wiki
2. Install with **Spanish** language data
3. Add Tesseract to PATH

### Optional: PDF input

```powershell
pip install pdf2image
```

Install Poppler for Windows and add poppler bin to PATH.

## Usage

### 1. Put photos in `photos/` (or point to any folder)

Original files are **never modified**.

### 2. Run the pipeline (4 OCR passes per page)

```powershell
python ocr_book.py .\photos .\output
```

| Run | Image | Engine |
|-----|-------|--------|
| A | Original | PaddleOCR |
| B | Light preprocess | PaddleOCR |
| C | Original | Tesseract |
| D | Light preprocess | Tesseract |

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
