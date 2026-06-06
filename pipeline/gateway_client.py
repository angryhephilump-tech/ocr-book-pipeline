from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


def gateway_url() -> str:
    env = os.environ.get("ARCHIVE_GATEWAY_URL", "").strip()
    if env:
        return env.rstrip("/")
    cfg = Path(__file__).resolve().parent.parent / "config" / "gateway.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            return str(data.get("gateway_url") or "http://127.0.0.1:8787").rstrip("/")
        except json.JSONDecodeError:
            pass
    return "http://127.0.0.1:8787"


@dataclass
class GatewayActivation:
    license_key: str
    remaining_credits: int
    total_credits: int
    email: str = ""


class GatewayError(RuntimeError):
    pass


def _raise_for_error(resp: requests.Response, default: str) -> None:
    if resp.ok:
        return
    try:
        body = resp.json()
        detail = body.get("detail") or body.get("error") or default
    except Exception:
        detail = f"{default} ({resp.status_code})"
    raise GatewayError(str(detail))


def activate_license(license_key: str) -> GatewayActivation:
    response = requests.post(
        f"{gateway_url()}/activate",
        json={"license_key": license_key},
        timeout=30,
    )
    _raise_for_error(response, "Activation failed")
    body = response.json()
    return GatewayActivation(
        license_key=license_key,
        remaining_credits=int(body.get("remaining_credits", 0)),
        total_credits=int(body.get("total_credits", 0)),
        email=str(body.get("email") or ""),
    )


def fetch_credits(license_key: str) -> dict[str, Any]:
    response = requests.post(
        f"{gateway_url()}/credits/status",
        json={"license_key": license_key},
        timeout=20,
    )
    _raise_for_error(response, "Credit lookup failed")
    return response.json()


def request_ocr(
    *,
    license_key: str,
    image_bytes: bytes,
    temperature: float,
    primary_language: str,
    secondary_language: str | None,
    indigenous_mode: bool,
    prompt: str | None = None,
) -> dict[str, Any]:
    payload = {
        "license_key": license_key,
        "image_base64": base64.b64encode(image_bytes).decode("utf-8"),
        "temperature": float(temperature),
        "primary_language": primary_language,
        "secondary_language": secondary_language,
        "indigenous_mode": bool(indigenous_mode),
    }
    if prompt:
        payload["prompt"] = prompt
    response = requests.post(
        f"{gateway_url()}/ocr",
        json=payload,
        timeout=180,
    )
    _raise_for_error(response, "OCR request failed")
    return response.json()


def commit_page_credit(
    *,
    license_key: str,
    page_id: str,
    idempotency_key: str,
    credits_used: int = 1,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = requests.post(
        f"{gateway_url()}/credits/deduct",
        json={
            "license_key": license_key,
            "page_id": page_id,
            "idempotency_key": idempotency_key,
            "credits_used": int(credits_used),
            "details": details or {},
        },
        timeout=20,
    )
    _raise_for_error(response, "Credit decrement failed")
    return response.json()

