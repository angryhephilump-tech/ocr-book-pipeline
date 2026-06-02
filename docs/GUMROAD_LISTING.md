# Gumroad listing checklist — Verbatim Studio

## Before you publish

1. Set `gumroad_product_id` in `config/product.json` (from Gumroad product settings).
2. Set `dev_skip_license` to `false` for release builds.
3. Rebuild installer: `.\scripts\build_installer.ps1`
4. Test activation with a real purchase key on a clean PC.

## Product copy (template)

**Title:** Verbatim Studio — Offline Book OCR & Review

**Tagline:** Turn page photos into publisher-grade text. 100% local. Human-in-the-loop review.

**Description bullets:**
- 4-pass OCR (PaddleOCR + Tesseract)
- Side-by-side review UI — nothing auto-corrected without your approval
- PDF and image import, export to PDF + plain text
- Works offline after one-time license activation
- No Python, no command line, no extra downloads for buyers

**Price:** (your choice — suggest $29–79 for pro OCR niche)

**Files to upload:** `dist/VerbatimStudio-Setup.exe`

## Screenshots to capture

1. Home screen — drop zone + book title
2. OCR progress ring
3. Review studio — photo + flagged words
4. Export confirmation

## License flow

Buyers receive a license key in Gumroad email → enter on first launch → app stores activation locally → works offline forever.

## Support blurb

> Need help? Email [your support email]. Include your Gumroad order email and a short description of the issue.
