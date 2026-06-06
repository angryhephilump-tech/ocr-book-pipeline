from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
  license_key TEXT PRIMARY KEY,
  email TEXT,
  product_id TEXT,
  total_credits INTEGER NOT NULL DEFAULT 0,
  remaining_credits INTEGER NOT NULL DEFAULT 0,
  activated_at REAL NOT NULL,
  last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  license_key TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  page_id TEXT NOT NULL,
  credits_used INTEGER NOT NULL,
  created_at REAL NOT NULL,
  details_json TEXT NOT NULL,
  UNIQUE(license_key, idempotency_key)
);
"""


@dataclass
class LicenseRecord:
    license_key: str
    remaining_credits: int
    total_credits: int
    email: str = ""
    product_id: str = ""


class CreditStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def upsert_activation(
        self,
        *,
        license_key: str,
        product_id: str,
        email: str,
        credits: int,
    ) -> LicenseRecord:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM licenses WHERE license_key = ?",
                (license_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO licenses (
                      license_key, email, product_id, total_credits, remaining_credits, activated_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (license_key, email, product_id, credits, credits, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE licenses
                    SET email = ?, product_id = ?, last_seen_at = ?
                    WHERE license_key = ?
                    """,
                    (email or row["email"], product_id or row["product_id"], now, license_key),
                )
            conn.commit()
            return self.get_license(license_key)

    def get_license(self, license_key: str) -> LicenseRecord:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM licenses WHERE license_key = ?",
                (license_key,),
            ).fetchone()
            if row is None:
                raise KeyError("License not activated")
            return LicenseRecord(
                license_key=row["license_key"],
                remaining_credits=int(row["remaining_credits"]),
                total_credits=int(row["total_credits"]),
                email=row["email"] or "",
                product_id=row["product_id"] or "",
            )

    def commit_page_credit(
        self,
        *,
        license_key: str,
        idempotency_key: str,
        page_id: str,
        credits_used: int = 1,
        details: dict | None = None,
    ) -> LicenseRecord:
        now = time.time()
        payload = json.dumps(details or {}, ensure_ascii=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM licenses WHERE license_key = ?",
                (license_key,),
            ).fetchone()
            if row is None:
                raise KeyError("License not activated")

            existing = conn.execute(
                """
                SELECT id FROM credit_events
                WHERE license_key = ? AND idempotency_key = ?
                """,
                (license_key, idempotency_key),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return self.get_license(license_key)

            if int(row["remaining_credits"]) < credits_used:
                raise ValueError("Insufficient credits")

            conn.execute(
                """
                INSERT INTO credit_events (
                  license_key, idempotency_key, page_id, credits_used, created_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (license_key, idempotency_key, page_id, credits_used, now, payload),
            )
            conn.execute(
                """
                UPDATE licenses
                SET remaining_credits = remaining_credits - ?, last_seen_at = ?
                WHERE license_key = ?
                """,
                (credits_used, now, license_key),
            )
            conn.commit()
            return self.get_license(license_key)

