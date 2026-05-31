#!/usr/bin/env python3
"""
Local review UI for OCR book pipeline.

Usage:
  python review_ui.py ./output
  python review_ui.py ./output --port 5050
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
from flask import Flask, jsonify, render_template, request, send_from_directory

from pipeline.export import export_pdf, export_plain_text, load_pages_for_export

ROOT = Path(__file__).resolve().parent


def create_app(output_dir: Path) -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))
    app.config["OUTPUT_DIR"] = output_dir
    state_path = output_dir / "review_state.json"

    def load_state() -> dict:
        if state_path.exists():
            return json.loads(state_path.read_text(encoding="utf-8"))
        return {"resolved_flags": {}, "page_texts": {}}

    def save_state(state: dict) -> None:
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def load_manifest() -> dict:
        mp = output_dir / "manifest.json"
        if not mp.exists():
            return {"pages": []}
        return json.loads(mp.read_text(encoding="utf-8"))

    def flagged_page_ids() -> list[str]:
        manifest = load_manifest()
        return [p["page_id"] for p in manifest.get("pages", []) if p.get("needs_review")]

    @app.route("/")
    def index():
        return render_template("review.html")

    @app.route("/api/manifest")
    def api_manifest():
        manifest = load_manifest()
        flagged = flagged_page_ids()
        return jsonify({**manifest, "flagged_queue": flagged})

    @app.route("/api/page/<page_id>")
    def api_page(page_id: str):
        consensus_path = output_dir / f"{page_id}_consensus.json"
        draft_path = output_dir / f"{page_id}_draft.txt"
        reviewed_path = output_dir / f"{page_id}_reviewed.txt"
        image_path = output_dir / f"{page_id}_source.jpg"

        if not consensus_path.exists():
            return jsonify({"error": "Page not found"}), 404

        consensus = json.loads(consensus_path.read_text(encoding="utf-8"))
        state = load_state()
        text = state.get("page_texts", {}).get(page_id)
        if text is None and reviewed_path.exists():
            text = reviewed_path.read_text(encoding="utf-8")
        elif text is None and draft_path.exists():
            text = draft_path.read_text(encoding="utf-8")
        else:
            text = text or ""

        flags = consensus.get("flags", [])
        resolved = state.get("resolved_flags", {})
        for f in flags:
            f["resolved"] = resolved.get(f["span_id"], False)

        return jsonify({
            "page_id": page_id,
            "text": text,
            "flags": flags,
            "stats": consensus.get("stats", {}),
            "layout": consensus.get("layout", {}),
            "image_url": f"/images/{page_id}_source.jpg" if image_path.exists() else None,
        })

    @app.route("/images/<path:filename>")
    def serve_image(filename: str):
        return send_from_directory(output_dir, filename)

    @app.route("/api/crop/<page_id>/<int:flag_index>")
    def api_crop(page_id: str, flag_index: int):
        consensus_path = output_dir / f"{page_id}_consensus.json"
        image_path = output_dir / f"{page_id}_source.jpg"
        if not consensus_path.exists() or not image_path.exists():
            return jsonify({"error": "Not found"}), 404

        flags = json.loads(consensus_path.read_text(encoding="utf-8")).get("flags", [])
        if flag_index < 0 or flag_index >= len(flags):
            return jsonify({"error": "Flag index out of range"}), 404

        flag = flags[flag_index]
        bbox = flag.get("bbox")
        img = cv2.imread(str(image_path))
        if img is None:
            return jsonify({"error": "Image read failed"}), 500

        if bbox:
            x1, y1, x2, y2 = bbox
            pad = 10
            h, w = img.shape[:2]
            x1, y1 = max(0, x1 - pad), max(0, y1 - pad)
            x2, y2 = min(w, x2 + pad), min(h, y2 + pad)
            crop = img[y1:y2, x1:x2]
        else:
            crop = img

        crop_dir = output_dir / "_crops"
        crop_dir.mkdir(exist_ok=True)
        crop_name = f"{page_id}_flag_{flag_index}.jpg"
        cv2.imwrite(str(crop_dir / crop_name), crop)
        return jsonify({"crop_url": f"/crops/{crop_name}"})

    @app.route("/crops/<path:filename>")
    def serve_crop(filename: str):
        return send_from_directory(output_dir / "_crops", filename)

    @app.route("/api/save", methods=["POST"])
    def api_save():
        data = request.get_json(force=True)
        page_id = data.get("page_id")
        text = data.get("text", "")
        resolved_flag = data.get("resolved_flag")
        resolution = data.get("resolution", "")

        if not page_id:
            return jsonify({"error": "page_id required"}), 400

        state = load_state()
        state.setdefault("page_texts", {})[page_id] = text
        if resolved_flag:
            state.setdefault("resolved_flags", {})[resolved_flag] = True
            state.setdefault("resolutions", {})[resolved_flag] = resolution
        save_state(state)

        reviewed_path = output_dir / f"{page_id}_reviewed.txt"
        reviewed_path.write_text(text, encoding="utf-8")
        return jsonify({"ok": True})

    @app.route("/api/export", methods=["POST"])
    def api_export():
        manifest = load_manifest()
        pages = load_pages_for_export(output_dir)
        state = load_state()
        for p in pages:
            pid = p["page_id"]
            if pid in state.get("page_texts", {}):
                p["reviewed_text"] = state["page_texts"][pid]

        title = manifest.get("book_title", "OCR Book Export")
        pdf_out = output_dir / "book_final.pdf"
        txt_out = output_dir / "book_final.txt"
        export_pdf(pages, pdf_out, title=title)
        export_plain_text(pages, txt_out)
        return jsonify({"pdf": str(pdf_out), "txt": str(txt_out)})

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch OCR review UI")
    parser.add_argument("output", help="Output folder from ocr_book.py")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()
    if not (output_dir / "manifest.json").exists():
        raise SystemExit(f"No manifest.json in {output_dir}. Run ocr_book.py first.")

    app = create_app(output_dir)
    print(f"Review UI: http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
