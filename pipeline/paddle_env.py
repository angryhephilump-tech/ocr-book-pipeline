"""Set Paddle env vars before any paddle import (must run first)."""

from __future__ import annotations

import os


def configure_paddle_env() -> None:
    # Paddle 3.x + oneDNN often crashes on Windows CPU; disable before import.
    for key, val in (
        ("FLAGS_use_mkldnn", "0"),
        ("FLAGS_enable_mkldnn", "0"),
        ("FLAGS_enable_onednn", "0"),
        ("FLAGS_enable_pir_in_executor", "0"),
        ("FLAGS_enable_new_executor", "0"),
        ("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True"),
    ):
        os.environ.setdefault(key, val)

    try:
        import paddle

        paddle.set_flags({"FLAGS_use_mkldnn": False})
    except Exception:
        pass
