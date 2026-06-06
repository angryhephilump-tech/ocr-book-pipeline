from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from gateway.store import CreditStore


GUMROAD_VERIFY_URL = "https://api.gumroad.com/v2/licenses/verify"
HF_MODEL_URL = "https://api-inference.huggingface.co/models/deepseek-ai/DeepSeek-OCR"
_RATE_STATE: dict[str, tuple[float, int]] = {}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _parse_credits(raw: str) -> int:
    m = re.search(r"(\d+)", raw or "")
    if not m:
        return int(_env("ARCHIVE_DEFAULT_CREDITS", "100"))
    return int(m.group(1))


def _base_prompt(indigenous_mode: bool, primary_language: str, secondary_language: str | None) -> str:
    lang_clause = f"Primary language: {primary_language}."
    if secondary_language:
        lang_clause += f" Secondary language: {secondary_language}."
    indigenous_clause = ""
    if indigenous_mode:
        indigenous_clause = (
            " This document may contain indigenous language text including Nahuatl, Mixtec, "
            "or other Mesoamerican languages written in the Latin alphabet. Extract all text exactly "
            "as written including all indigenous language words. Do not correct unfamiliar words."
        )
    return (
        "Extract all text from this document exactly as written. "
        "Preserve punctuation and accents exactly. "
        "Respond ONLY as JSON with this shape: "
        '{"text":"...", "words":[{"text":"...", "confidence":0-100, "bbox":[x1,y1,x2,y2], "line_id":0}]}. '
        f"{lang_clause}{indigenous_clause}"
    )


class ActivateRequest(BaseModel):
    license_key: str = Field(min_length=3)


class CreditsRequest(BaseModel):
    license_key: str


class OcrRequest(BaseModel):
    license_key: str
    image_base64: str
    temperature: float = 0.3
    prompt: str | None = None
    primary_language: str = "spa"
    secondary_language: str | None = None
    indigenous_mode: bool = False


class CommitRequest(BaseModel):
    license_key: str
    idempotency_key: str
    page_id: str
    credits_used: int = 1
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ServiceConfig:
    gumroad_product_id: str
    hf_token: str
    default_credits: int
    rate_limit_per_minute: int


def _enforce_rate_limit(license_key: str, per_minute: int) -> None:
    now = time.time()
    window_start, count = _RATE_STATE.get(license_key, (now, 0))
    if now - window_start >= 60:
        window_start, count = now, 0
    count += 1
    _RATE_STATE[license_key] = (window_start, count)
    if count > per_minute:
        retry_after = max(1, int(60 - (now - window_start)))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for this license. Retry in ~{retry_after}s.",
        )


def build_app() -> FastAPI:
    app = FastAPI(title="Archive Gateway", version="1.0.0")
    db_path = Path(_env("ARCHIVE_GATEWAY_DB", str(Path.home() / ".archive_gateway" / "credits.db")))
    store = CreditStore(db_path)
    cfg = ServiceConfig(
        gumroad_product_id=_env("ARCHIVE_GUMROAD_PRODUCT_ID"),
        hf_token=_env("ARCHIVE_HF_TOKEN"),
        default_credits=int(_env("ARCHIVE_DEFAULT_CREDITS", "100")),
        rate_limit_per_minute=int(_env("ARCHIVE_RATE_LIMIT_PER_MIN", "60")),
    )
    dev_hf_only = _env("ARCHIVE_DEV_HF_ONLY", "0").lower() in ("1", "true", "yes")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "time": time.time()}

    @app.post("/activate")
    def activate(req: ActivateRequest) -> dict:
        key = req.license_key.strip()
        purchase_email = ""
        if dev_hf_only:
            credits = int(_env("ARCHIVE_DEV_CREDITS", str(cfg.default_credits)))
        else:
            if not cfg.gumroad_product_id:
                raise HTTPException(status_code=500, detail="Gateway missing ARCHIVE_GUMROAD_PRODUCT_ID")
            payload = {
                "product_id": cfg.gumroad_product_id,
                "license_key": key,
                "increment_uses_count": "false",
            }
            try:
                response = requests.post(GUMROAD_VERIFY_URL, data=payload, timeout=30)
                response.raise_for_status()
                body = response.json()
            except requests.RequestException as exc:
                raise HTTPException(status_code=502, detail=f"Gumroad validation failed: {exc}") from exc

            if not body.get("success"):
                raise HTTPException(status_code=400, detail=body.get("message") or "Invalid license")

            purchase = body.get("purchase") or {}
            if purchase.get("refunded") or purchase.get("chargebacked"):
                raise HTTPException(status_code=400, detail="License has been refunded or charged back")
            purchase_email = str(purchase.get("email") or "")
            credits_hint = purchase.get("variants") or purchase.get("variant") or ""
            credits = _parse_credits(str(credits_hint))
            if credits <= 0:
                credits = cfg.default_credits

        rec = store.upsert_activation(
            license_key=key,
            product_id=cfg.gumroad_product_id,
            email=purchase_email,
            credits=credits,
        )
        return {
            "ok": True,
            "remaining_credits": rec.remaining_credits,
            "total_credits": rec.total_credits,
            "email": rec.email,
        }

    @app.post("/credits")
    def credits(req: CreditsRequest) -> dict:
        try:
            rec = store.get_license(req.license_key.strip())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "ok": True,
            "remaining_credits": rec.remaining_credits,
            "total_credits": rec.total_credits,
            "email": rec.email,
        }

    @app.post("/ocr/page")
    def ocr_page(req: OcrRequest) -> dict:
        _enforce_rate_limit(req.license_key.strip(), cfg.rate_limit_per_minute)
        try:
            rec = store.get_license(req.license_key.strip())
        except KeyError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if rec.remaining_credits <= 0:
            raise HTTPException(status_code=402, detail="No credits remaining")
        if not cfg.hf_token:
            raise HTTPException(status_code=500, detail="Gateway missing ARCHIVE_HF_TOKEN")

        prompt = req.prompt or _base_prompt(
            indigenous_mode=req.indigenous_mode,
            primary_language=req.primary_language,
            secondary_language=req.secondary_language,
        )
        body = {
            "inputs": f"<image>{req.image_base64}</image>\n{prompt}",
            "parameters": {"temperature": req.temperature, "max_new_tokens": 2048},
        }
        headers = {"Authorization": f"Bearer {cfg.hf_token}"}
        try:
            response = requests.post(HF_MODEL_URL, headers=headers, json=body, timeout=120)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Inference request failed: {exc}") from exc

        payload = response.json()
        text = ""
        words: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("generated_text") or "")
            maybe_words = payload.get("words")
            if isinstance(maybe_words, list):
                words = [w for w in maybe_words if isinstance(w, dict)]
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            sample = payload[0]
            text = str(sample.get("generated_text") or sample.get("text") or "")
            maybe_words = sample.get("words")
            if isinstance(maybe_words, list):
                words = [w for w in maybe_words if isinstance(w, dict)]
        else:
            text = str(payload)

        if not words and text:
            # Fallback for models that only return plain text.
            line_id = 0
            for line in text.splitlines():
                for token in line.split():
                    words.append({"text": token, "confidence": 70.0, "bbox": [0, 0, 0, 0], "line_id": line_id})
                line_id += 1

        return {
            "ok": True,
            "text": text,
            "words": words,
            "remaining_credits": rec.remaining_credits,
            "raw": payload,
        }

    @app.post("/ocr")
    def ocr_alias(req: OcrRequest) -> dict:
        return ocr_page(req)

    @app.post("/credits/commit")
    def commit(req: CommitRequest) -> dict:
        try:
            rec = store.commit_page_credit(
                license_key=req.license_key.strip(),
                idempotency_key=req.idempotency_key.strip(),
                page_id=req.page_id,
                credits_used=req.credits_used,
                details=req.details,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc
        return {
            "ok": True,
            "remaining_credits": rec.remaining_credits,
            "total_credits": rec.total_credits,
        }

    @app.post("/credits/deduct")
    def deduct_alias(req: CommitRequest) -> dict:
        return commit(req)

    @app.post("/credits/status")
    def credits_status_alias(req: CreditsRequest) -> dict:
        return credits(req)

    return app


app = build_app()

