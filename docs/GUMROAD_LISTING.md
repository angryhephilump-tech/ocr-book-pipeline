# Gumroad listing checklist — Archive Studios

## Before you publish

1. Set `gumroad_product_id` in `config/product.json` (from Gumroad product settings).
2. Set `dev_skip_license` to `false` for release builds.
3. Rebuild installer: `.\scripts\build_installer.ps1`
4. Test activation with a real purchase key on a clean PC.

## Product copy (template)

**Title:** Archive Studios — DeepSeek OCR & Review

**Tagline:** Turn page photos into publisher-grade text with human-in-the-loop review.

**Description (use in Gumroad body):**

Supports mixed-language archival documents with strict review-first OCR and structured disagreement checks. Includes special handling for indigenous or minority language text.

**Description bullets:**
- 3-run DeepSeek OCR with variation (sampling + enhanced-image pass)
- Any disagreement or low confidence is flagged for human review (no majority auto-accept)
- Indigenous/minority language mode — lower confidence threshold and no forced normalization
- Side-by-side review UI — nothing auto-corrected without your approval
- PDF and image import, export to PDF + plain text
- Managed credits with Gumroad activation
- Requires internet connection for document processing (source files remain on your machine; page images are sent to secure processing server)

**Price:** (your choice — suggest $29–79 for pro OCR niche)

**Files to upload:** `dist/ArchiveStudios-Setup.exe`

## Screenshots to capture

1. Home screen — drop zone + book title
2. OCR progress ring
3. Review studio — photo + flagged words
4. Export confirmation

## License flow

Buyers receive a license key in Gumroad email → enter on first launch → app activates page credits and begins processing through managed OCR gateway.

## Support blurb

> Need help? Email [your support email]. Include your Gumroad order email and a short description of the issue.
