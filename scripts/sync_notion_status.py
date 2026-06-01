#!/usr/bin/env python3
"""
Sync status.json to a Notion page (replace page body with current build status).

Requires environment variables:
  NOTION_TOKEN   - Notion integration secret
  NOTION_PAGE_ID - Page ID (32 chars, with or without hyphens)

Run locally or from GitHub Actions after push to main.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NOTION_VERSION = "2022-06-28"
ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / "status.json"

BOOL_FIELDS = [
    ("installer_ready", "One-click Windows installer"),
    ("paddle_bundled", "PaddleOCR bundled in vendor folder"),
    ("tesseract_bundled", "Tesseract bundled in vendor folder"),
    ("poppler_bundled", "Poppler bundled in vendor folder"),
    ("models_prebundled", "Paddle models pre-downloaded at build time"),
    ("relative_paths_fixed", "All paths use APP_DIR (no system PATH)"),
    ("license_key_validation", "Gumroad license key screen"),
    ("review_ui_working", "Verbatim Studio review UI"),
    ("pdf_import_working", "PDF import works offline"),
    ("clean_machine_tested", "Tested on clean Windows (no Python)"),
    ("gumroad_listing_ready", "Gumroad listing live"),
    ("offline_ui_no_cdn", "UI works without internet (no Google Fonts CDN)"),
]


def notion_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_page_id(page_id: str) -> str:
    pid = page_id.strip().replace("-", "")
    if len(pid) != 32:
        raise ValueError("NOTION_PAGE_ID must be a 32-character Notion page ID")
    return f"{pid[:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:]}"


def list_child_block_ids(page_id: str, token: str) -> list[str]:
    ids: list[str] = []
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    while url:
        data = notion_request("GET", url, token)
        for block in data.get("results", []):
            ids.append(block["id"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100&start_cursor={cursor}"
    return ids


def delete_blocks(block_ids: list[str], token: str) -> None:
    for bid in block_ids:
        try:
            notion_request("DELETE", f"https://api.notion.com/v1/blocks/{bid}", token)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise


def build_status_blocks(status: dict) -> list[dict]:
    note = status.get("last_cursor_note", "")
    updated = status.get("last_updated", "")
    try:
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        updated_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        updated_display = updated or "unknown"

    blocks: list[dict] = [
        {
            "object": "block",
            "type": "heading_1",
            "heading_1": {
                "rich_text": [{"type": "text", "text": {"content": "Verbatim Studio — Build Status"}}],
            },
        },
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📖"},
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"Last updated: {updated_display}\n\n{note}",
                        },
                    }
                ],
            },
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Checklist"}}],
            },
        },
    ]

    for key, label in BOOL_FIELDS:
        checked = bool(status.get(key, False))
        blocks.append(
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": label}}],
                    "checked": checked,
                },
            }
        )

    done = sum(1 for k, _ in BOOL_FIELDS if status.get(k))
    total = len(BOOL_FIELDS)
    blocks.append(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"Progress: {done}/{total} complete"},
                    }
                ],
            },
        }
    )
    blocks.append(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "Repo: github.com/angryhephilump-tech/ocr-book-pipeline — synced on push to main.",
                        },
                    }
                ],
            },
        }
    )
    return blocks


def append_blocks(page_id: str, token: str, blocks: list[dict]) -> None:
    chunk_size = 100
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i : i + chunk_size]
        notion_request(
            "PATCH",
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            token,
            {"children": chunk},
        )


def main() -> int:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    page_id_raw = os.environ.get("NOTION_PAGE_ID", "").strip()
    if not token or not page_id_raw:
        print("Missing NOTION_TOKEN or NOTION_PAGE_ID", file=sys.stderr)
        return 1

    page_id = normalize_page_id(page_id_raw)

    if not STATUS_PATH.exists():
        print(f"Missing {STATUS_PATH}", file=sys.stderr)
        return 1

    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    status["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    STATUS_PATH.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")

    blocks = build_status_blocks(status)
    child_ids = list_child_block_ids(page_id, token)
    if child_ids:
        delete_blocks(child_ids, token)
    append_blocks(page_id, token, blocks)

    print(f"Notion page updated: {page_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
