"""Gumroad license verification and local activation storage."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.paths import app_root, is_frozen

GUMROAD_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"
ACTIVATION_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Verbatim Studio"
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
    """Verify with Gumroad API once; returns (success, message)."""
    pid = product_id()
    if not pid:
        return False, "Product ID not configured. Set gumroad_product_id in config/product.json."

    key = license_key.strip()
    if not key:
        return False, "Enter your license key from your Gumroad receipt email."

    body = urllib.parse.urlencode(
        {
            "product_id": pid,
            "license_key": key,
            "increment_uses_count": "false",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        GUMROAD_VERIFY_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            msg = err_body.get("message") or str(exc)
        except Exception:
            msg = str(exc)
        return False, msg
    except urllib.error.URLError as exc:
        return False, f"Could not reach Gumroad: {exc.reason}"

    if not payload.get("success"):
        return False, payload.get("message") or "License key is not valid."

    purchase = payload.get("purchase") or {}
    if purchase.get("refunded") or purchase.get("chargebacked"):
        return False, "This license has been refunded or charged back."

    ACTIVATION_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVATION_FILE.write_text(
        json.dumps(
            {
                "verified": True,
                "product_id": pid,
                "license_key": key,
                "email": purchase.get("email", ""),
                "frozen_build": is_frozen(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return True, "License activated. This app works offline from now on."
