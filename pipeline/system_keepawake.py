"""Keep the system awake during long OCR jobs (Windows)."""

from __future__ import annotations

import contextlib
import sys


@contextlib.contextmanager
def keep_system_awake():
    """Prevent system sleep while OCR runs. Restores previous state on exit."""
    if sys.platform != "win32":
        yield
        return

    import ctypes

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    ctypes.windll.kernel32.SetThreadExecutionState(flags)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
