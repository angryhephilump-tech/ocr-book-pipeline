#!/usr/bin/env python3
"""Legacy — use save_claude_key.py instead."""
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "save_claude_key.py"), run_name="__main__")
