"""Set Paddle env vars before any paddle import (must run first)."""

from __future__ import annotations

import os


def configure_paddle_env() -> None:
    for key, val in (
        ("FLAGS_use_mkldnn", "0"),
        ("FLAGS_enable_mkldnn", "0"),
        ("FLAGS_enable_onednn", "0"),
        ("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True"),
    ):
        os.environ.setdefault(key, val)
