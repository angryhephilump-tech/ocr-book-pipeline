"""Gateway-backed license + credit activation storage."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline.paths import app_root, is_frozen
from pipeline.gateway_client import GatewayError, activate_license, fetch_credits

ACTIVATION_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Archive Studios"
ACTIVATION_FILE = ACTIVATION_DIR / "activation.json"


def _product_config() -> dict:
    path = app_root() / "config" / "product.json"
    if not path.is_file():
        path = Path(__file__).resolve().parent / "config" / "product.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def license_required() -> bool:
    cfg = _product_config()
    if os.environ.get("ARCHIVE_DEV_HF_ONLY", "").strip().lower() in ("1", "true", "yes"):
        return True
    if os.environ.get("VERBATIM_DEV", "").strip() in ("1", "true", "yes"):
        return False
    if cfg.get("dev_skip_license") and not (cfg.get("gumroad_product_id") or "").strip():
        return False
    if not cfg.get("license_required", True):
        return False
    product_id = (cfg.get("gumroad_product_id") or "").strip()
    if not product_id or product_id.lower() in ("dev", "test", "skip"):
        return False
    return True


def product_id() -> str:
    return (_product_config().get("gumroad_product_id") or "").strip()


def is_activated() -> bool:
    if not license_required():
        return True
    if not ACTIVATION_FILE.is_file():
        return False
    try:
        data = json.loads(ACTIVATION_FILE.read_text(encoding="utf-8"))
        return bool(data.get("verified")) and data.get("product_id") == product_id()
    except (json.JSONDecodeError, OSError):
        return False


def load_activation() -> dict | None:
    if not ACTIVATION_FILE.is_file():
        return None
    try:
        return json.loads(ACTIVATION_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def verify_license_key(license_key: str) -> tuple[bool, str]:
    """Verify with Archive Gateway once; returns (success, message)."""
    key = license_key.strip()
    if not key:
        return False, "Enter your license key from your Gumroad receipt email."
    try:
        act = activate_license(key)
    except GatewayError as exc:
        return False, str(exc)

    ACTIVATION_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVATION_FILE.write_text(
        json.dumps(
            {
                "verified": True,
                "product_id": product_id(),
                "license_key": key,
                "email": act.email,
                "remaining_credits": act.remaining_credits,
                "total_credits": act.total_credits,
                "frozen_build": is_frozen(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return True, f"Activated — {act.remaining_credits} pages remaining."


def credits_status() -> dict:
    data = load_activation() or {}
    if not data.get("license_key"):
        return {"remaining_credits": 0, "total_credits": 0}
    try:
        live = fetch_credits(str(data["license_key"]))
        data["remaining_credits"] = int(live.get("remaining_credits", 0))
        data["total_credits"] = int(live.get("total_credits", 0))
        ACTIVATION_DIR.mkdir(parents=True, exist_ok=True)
        ACTIVATION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except GatewayError:
        pass
    return {
        "remaining_credits": int(data.get("remaining_credits", 0)),
        "total_credits": int(data.get("total_credits", 0)),
    }
