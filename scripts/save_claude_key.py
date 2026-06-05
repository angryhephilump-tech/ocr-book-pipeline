#!/usr/bin/env python3
"""Save Claude API key — no extra packages."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_transcribe import save_settings  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print('Usage: python scripts/save_claude_key.py "sk-ant-your-key"')
        return 1
    key = sys.argv[1].strip()
    if not key:
        print("Error: key was empty.")
        return 1
    if not key.startswith("sk-ant"):
        print("Warning: Claude keys usually start with sk-ant-")
    path = save_settings(api_key=key)
    print("Saved! You're ready to transcribe.")
    print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
