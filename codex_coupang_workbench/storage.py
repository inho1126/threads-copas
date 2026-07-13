from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Any


_REDNOTE_NOTE_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_CHINESE_QUERY_PATTERN = re.compile(r"^[\u3400-\u9fff]{2,24}$")
_OPAQUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REDNOTE_ASSET_TYPES = frozenset({"video", "frame"})
_REDNOTE_ASSET_METRICS = ("width", "height", "duration_ms", "timestamp_ms")
_STUDIO_JOB_FIELDS = frozenset(
    {
        "selected_profile_key",
        "rednote_query",
        "rednote_note_id",
        "rednote_canonical_url",
        "rednote_sidecar_job_id",
        "rednote_search_id",
        "publish_idempotency_key",
    }
)
_PUBLISH_STAGES = frozenset(
    {
        "draft",
        "uploading_media",
        "media_uploaded",
        "creating_container",
        "container_created",
        "container_ready",
        "publishing_main",
        "publishing_main_inflight",
        "main_published",
        "publishing_reply",
        "publishing_reply_inflight",
        "published",
        "failed",
        "outcome_unknown",
    }
)
_PUBLISH_ORIGINS = frozenset({"local", "remote"})
_PUBLISH_JSON_FIELDS = (
    "publish_locked_asset_ids",
    "publish_media_ids",
    "publish_media_urls",
    "publish_child_container_ids",
)
_PUBLISH_CHECKPOINT_FIELDS = frozenset(
    {
        "publish_media_ids",
        "publish_media_urls",
        "publish_child_container_ids",
        "publish_container_id",
        "threads_post_id",
        "threads_reply_id",
        "threads_permalink",
        "threads_profile_key",
        "threads_published_at",
        "status",
    }
)
_PUBLISH_CHECKPOINT_ALIASES = {
    "media_ids": "publish_media_ids",
    "media_urls": "publish_media_urls",
    "child_container_ids": "publish_child_container_ids",
    "container_id": "publish_container_id",
}
_COPY_VARIANT_ORDER = {
    "curiosity": 0,
    "relatable": 1,
    "problem_solution": 2,
    "honest_discovery": 3,
    "story": 4,
    "conversion": 5,
    "custom": 6,
}
THREADS_OAUTH_STATE_TTL_SECONDS = 600
REMOTE_PUBLISH_LEASE_TTL_SECONDS = 300


class PublishPayloadLockedError(ValueError):
    """Raised when a claimed publish payload can no longer be edited."""


def _raise_if_publish_payload_locked(row: sqlite3.Row) -> None:
    if str(row["publish_idempotency_key"] or "").strip():
        raise PublishPayloadLockedError("publish payload is locked")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_metric(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class WorkbenchStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    product_url TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    memo TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    draft TEXT NOT NULL DEFAULT '',
                    sns_draft TEXT NOT NULL DEFAULT '',
                    image_brief TEXT NOT NULL DEFAULT '',
                    blog_final TEXT NOT NULL DEFAULT '',
                    sns_final TEXT NOT NULL DEFAULT '',
                    generated_image_url TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    publish_url TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_candidates (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    image_url TEXT NOT NULL DEFAULT '',
                    timestamp_label TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    creator TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    no_captions INTEGER NOT NULL DEFAULT 0,
                    no_tts INTEGER NOT NULL DEFAULT 0,
                    product_visible INTEGER NOT NULL DEFAULT 0,
                    permission_reviewed INTEGER NOT NULL DEFAULT 0,
                    review_status TEXT NOT NULL DEFAULT 'CANDIDATE',
                    approved_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS threads_profiles (
                    profile_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    threads_user_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS threads_oauth_states (
                    state TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    profile_key TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS remote_publish_leases (
                    job_id TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS copy_variants (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    persona_key TEXT NOT NULL,
                    persona_label TEXT NOT NULL DEFAULT '',
                    custom_instruction TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    selected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id),
                    UNIQUE(job_id, persona_key)
                );

                CREATE TABLE IF NOT EXISTS rednote_assets (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    note_id TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    timestamp_ms INTEGER NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );

                """
            )
            self._ensure_column(conn, "jobs", "image_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "sns_draft", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "image_brief", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "blog_final", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "sns_final", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "generated_image_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_profile_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_post_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_reply_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_permalink", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_published_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_views", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_likes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_replies", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_reposts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_quotes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_shares", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "threads_insights_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "threads_insights_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "selected_profile_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "selected_copy_variant_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "rednote_query", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "rednote_query_generation", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "rednote_note_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "rednote_canonical_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "rednote_sidecar_job_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "rednote_search_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "media_mode", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_stage", "TEXT NOT NULL DEFAULT 'draft'")
            self._ensure_column(conn, "jobs", "publish_idempotency_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "jobs", "publish_last_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_origin", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_profile_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_threads_user_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_body", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_comment", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_media_mode", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_locked_asset_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "publish_media_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "publish_media_urls", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "publish_child_container_ids", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "jobs", "publish_container_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "jobs", "publish_resume_stage", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_publish_idempotency_key
                ON jobs(publish_idempotency_key)
                WHERE publish_idempotency_key != ''
                """
            )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if column_name not in {row["name"] for row in rows}:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def get_settings(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_settings(self, settings: dict[str, Any]) -> dict[str, str]:
        now = utc_now()
        with self._connect() as conn:
            for key, value in settings.items():
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (key, "" if value is None else str(value), now),
                )
        return self.get_settings()

    def add_job(
        self,
        product_url: str,
        product_name: str,
        memo: str = "",
        image_url: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, product_url, product_name, image_url, memo, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'READY', ?, ?)
                """,
                (
                    job_id,
                    product_url.strip(),
                    product_name.strip(),
                    image_url.strip(),
                    memo.strip(),
                    now,
                    now,
                ),
            )
        self.add_log(job_id, "INFO", "Job queued")
        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError("Queued job could not be loaded")
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def upsert_copy_variants(
        self,
        job_id: str,
        variants: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.get_job(job_id) is None:
            raise KeyError(job_id)

        normalized: list[dict[str, Any]] = []
        persona_keys: set[str] = set()
        for raw in variants:
            if not isinstance(raw, dict):
                raise ValueError("Each copy variant must be an object")
            persona_key = str(raw.get("persona_key") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not persona_key:
                raise ValueError("persona_key is required")
            if not body:
                raise ValueError("copy variant body is required")
            if persona_key in persona_keys:
                raise ValueError("duplicate persona_key in copy variant batch")
            persona_keys.add(persona_key)
            try:
                generation = int(raw.get("generation", 1))
            except (TypeError, ValueError) as exc:
                raise ValueError("generation must be a positive integer") from exc
            if generation < 1:
                raise ValueError("generation must be a positive integer")
            normalized.append(
                {
                    "id": str(raw.get("id") or uuid.uuid4()).strip(),
                    "persona_key": persona_key,
                    "persona_label": str(raw.get("persona_label") or "").strip(),
                    "custom_instruction": str(raw.get("custom_instruction") or "").strip(),
                    "body": body,
                    "generation": generation,
                }
            )
            if not normalized[-1]["id"]:
                raise ValueError("copy variant id is required")

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            _raise_if_publish_payload_locked(job_row)
            for variant in normalized:
                conn.execute(
                    """
                    INSERT INTO copy_variants (
                        id, job_id, persona_key, persona_label, custom_instruction,
                        body, generation, selected, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(job_id, persona_key) DO UPDATE SET
                        persona_label = excluded.persona_label,
                        custom_instruction = excluded.custom_instruction,
                        body = excluded.body,
                        generation = excluded.generation,
                        updated_at = excluded.updated_at
                    """,
                    (
                        variant["id"],
                        job_id,
                        variant["persona_key"],
                        variant["persona_label"],
                        variant["custom_instruction"],
                        variant["body"],
                        variant["generation"],
                        now,
                        now,
                    ),
                )
        if normalized:
            self.add_log(job_id, "INFO", "Threads copy variants saved")
        return self.list_copy_variants(job_id)

    def list_copy_variants(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM copy_variants
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchall()
        variants = [self._row_to_copy_variant(row) for row in rows]
        return sorted(variants, key=_copy_variant_sort_key)

    def select_copy_variant(self, job_id: str, variant_id: str) -> dict[str, Any]:
        clean_variant_id = variant_id.strip()
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            _raise_if_publish_payload_locked(job_row)
            row = conn.execute(
                "SELECT * FROM copy_variants WHERE id = ? AND job_id = ?",
                (clean_variant_id, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("copy variant does not belong to this job")
            conn.execute(
                "UPDATE copy_variants SET selected = 0, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            conn.execute(
                "UPDATE copy_variants SET selected = 1, updated_at = ? WHERE id = ?",
                (now, clean_variant_id),
            )
            conn.execute(
                """
                UPDATE jobs
                SET selected_copy_variant_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (clean_variant_id, now, job_id),
            )
            selected = conn.execute(
                "SELECT * FROM copy_variants WHERE id = ?",
                (clean_variant_id,),
            ).fetchone()
        self.add_log(job_id, "INFO", "Threads copy variant selected")
        if selected is None:
            raise RuntimeError("Selected copy variant could not be loaded")
        return self._row_to_copy_variant(selected)

    def update_copy_variant_body(self, job_id: str, variant_id: str, body: str) -> dict[str, Any]:
        clean_variant_id = variant_id.strip()
        clean_body = body.strip()
        if not clean_body:
            raise ValueError("copy variant body is required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            _raise_if_publish_payload_locked(job_row)
            row = conn.execute(
                "SELECT * FROM copy_variants WHERE id = ? AND job_id = ?",
                (clean_variant_id, job_id),
            ).fetchone()
            if row is None:
                raise ValueError("copy variant does not belong to this job")
            conn.execute(
                "UPDATE copy_variants SET body = ?, updated_at = ? WHERE id = ?",
                (clean_body, now, clean_variant_id),
            )
            updated = conn.execute("SELECT * FROM copy_variants WHERE id = ?", (clean_variant_id,)).fetchone()
        self.add_log(job_id, "INFO", "Threads copy variant edited")
        if updated is None:
            raise RuntimeError("Edited copy variant could not be loaded")
        return self._row_to_copy_variant(updated)

    def replace_rednote_assets(
        self,
        job_id: str,
        assets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.get_job(job_id) is None:
            raise KeyError(job_id)

        normalized: list[dict[str, Any]] = []
        asset_ids: set[str] = set()
        batch_note_id = ""
        batch_canonical_url = ""
        for sort_order, raw in enumerate(assets):
            if not isinstance(raw, dict):
                raise ValueError("Each RedNote asset must be an object")
            asset_id = str(raw.get("id") or uuid.uuid4()).strip()
            if not asset_id:
                raise ValueError("RedNote asset id is required")
            if asset_id in asset_ids:
                raise ValueError("duplicate RedNote asset id")
            asset_ids.add(asset_id)

            note_id = str(raw.get("note_id") or "").strip()
            canonical_url = str(raw.get("canonical_url") or "").strip()
            if not _is_canonical_rednote_url(note_id, canonical_url):
                raise ValueError("RedNote canonical_url or note_id is invalid")
            if batch_note_id and (note_id != batch_note_id or canonical_url != batch_canonical_url):
                raise ValueError("All RedNote assets must belong to one note")
            batch_note_id = note_id
            batch_canonical_url = canonical_url

            asset_type = str(raw.get("asset_type") or "").strip().lower()
            if asset_type not in _REDNOTE_ASSET_TYPES:
                raise ValueError("RedNote asset_type must be video or frame")
            local_path = str(raw.get("local_path") or "").strip()
            if not local_path:
                raise ValueError("RedNote asset local_path is required")

            metrics: dict[str, int] = {}
            for field in _REDNOTE_ASSET_METRICS:
                try:
                    value = int(raw.get(field, 0) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"RedNote asset {field} must be a non-negative integer") from exc
                if value < 0:
                    raise ValueError(f"RedNote asset {field} must be a non-negative integer")
                metrics[field] = value

            normalized.append(
                {
                    "id": asset_id,
                    "note_id": note_id,
                    "canonical_url": canonical_url,
                    "asset_type": asset_type,
                    "local_path": local_path,
                    "mime_type": str(raw.get("mime_type") or "").strip().lower(),
                    "sort_order": sort_order,
                    **metrics,
                }
            )

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            _raise_if_publish_payload_locked(job_row)
            if asset_ids:
                placeholders = ",".join("?" for _ in asset_ids)
                foreign = conn.execute(
                    f"SELECT id FROM rednote_assets WHERE id IN ({placeholders}) AND job_id != ?",
                    (*asset_ids, job_id),
                ).fetchone()
                if foreign is not None:
                    raise ValueError("RedNote asset id already belongs to another job")
            conn.execute("DELETE FROM rednote_assets WHERE job_id = ?", (job_id,))
            for asset in normalized:
                conn.execute(
                    """
                    INSERT INTO rednote_assets (
                        id, job_id, note_id, canonical_url, asset_type, local_path,
                        mime_type, width, height, duration_ms, timestamp_ms,
                        sort_order, selected, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        asset["id"],
                        job_id,
                        asset["note_id"],
                        asset["canonical_url"],
                        asset["asset_type"],
                        asset["local_path"],
                        asset["mime_type"],
                        asset["width"],
                        asset["height"],
                        asset["duration_ms"],
                        asset["timestamp_ms"],
                        asset["sort_order"],
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE jobs
                SET rednote_note_id = ?, rednote_canonical_url = ?, media_mode = '',
                    updated_at = ?
                WHERE id = ?
                """,
                (batch_note_id, batch_canonical_url, now, job_id),
            )
        self.add_log(job_id, "INFO", "RedNote media assets replaced")
        return self.list_rednote_assets(job_id)

    def list_rednote_assets(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM rednote_assets
                WHERE job_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (job_id,),
            ).fetchall()
        return [self._row_to_rednote_asset(row) for row in rows]

    def select_rednote_assets(
        self,
        job_id: str,
        asset_ids: list[str],
        media_mode: str,
    ) -> list[dict[str, Any]]:
        mode = media_mode.strip().lower()
        clean_ids = [asset_id.strip() for asset_id in asset_ids]
        if len(clean_ids) != len(set(clean_ids)):
            raise ValueError("duplicate RedNote asset ids are not allowed")
        if mode == "video":
            if len(clean_ids) != 1:
                raise ValueError("video mode requires exactly one video")
        elif mode == "images":
            if not 2 <= len(clean_ids) <= 5:
                raise ValueError("images mode requires 2 to 5 frames")
        elif mode == "mixed":
            if len(clean_ids) != 2:
                raise ValueError("mixed mode requires one video and one frame")
        else:
            raise ValueError("media_mode must be video, images, or mixed")

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            _raise_if_publish_payload_locked(job_row)
            placeholders = ",".join("?" for _ in clean_ids)
            rows = conn.execute(
                f"SELECT id, asset_type FROM rednote_assets WHERE job_id = ? AND id IN ({placeholders})",
                (job_id, *clean_ids),
            ).fetchall()
            if len(rows) != len(clean_ids):
                raise ValueError("All RedNote assets must belong to this job")
            asset_types = {row["asset_type"] for row in rows}
            if mode == "video" and asset_types != {"video"}:
                raise ValueError("video mode accepts only one video asset")
            if mode == "images" and asset_types != {"frame"}:
                raise ValueError("images mode accepts only frame assets")
            if mode == "mixed" and asset_types != {"video", "frame"}:
                raise ValueError("mixed mode requires one video asset and one frame asset")

            conn.execute(
                "UPDATE rednote_assets SET selected = 0, updated_at = ? WHERE job_id = ?",
                (now, job_id),
            )
            conn.execute(
                f"UPDATE rednote_assets SET selected = 1, updated_at = ? WHERE job_id = ? AND id IN ({placeholders})",
                (now, job_id, *clean_ids),
            )
            conn.execute(
                "UPDATE jobs SET media_mode = ?, updated_at = ? WHERE id = ?",
                (mode, now, job_id),
            )
        self.add_log(job_id, "INFO", f"RedNote {mode} media selected")
        return self.list_rednote_assets(job_id)

    def update_studio_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        unsupported = sorted(set(fields) - _STUDIO_JOB_FIELDS)
        if unsupported:
            raise ValueError(f"Unsupported studio job field: {unsupported[0]}")
        normalized = {
            field: "" if value is None else str(value).strip()
            for field, value in fields.items()
        }
        for field in ("rednote_search_id", "rednote_sidecar_job_id"):
            value = normalized.get(field, "")
            if value and not _OPAQUE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"{field} must be an opaque identifier")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if "publish_idempotency_key" in normalized:
                current_key = str(row["publish_idempotency_key"] or "")
                new_key = normalized["publish_idempotency_key"]
                if current_key and new_key != current_key:
                    raise ValueError("publish idempotency key cannot be replaced")

            candidate_note_id = normalized.get("rednote_note_id", row["rednote_note_id"])
            candidate_url = normalized.get("rednote_canonical_url", row["rednote_canonical_url"])
            if candidate_url and not _is_canonical_rednote_url(candidate_note_id, candidate_url):
                raise ValueError("RedNote canonical_url or note_id is invalid")
            if candidate_note_id and not _REDNOTE_NOTE_ID_PATTERN.fullmatch(candidate_note_id):
                raise ValueError("RedNote note_id is invalid")

            if normalized:
                assignments = ", ".join(f"{field} = ?" for field in normalized)
                conn.execute(
                    f"UPDATE jobs SET {assignments}, updated_at = ? WHERE id = ?",
                    (*normalized.values(), now, job_id),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if normalized:
            self.add_log(job_id, "INFO", "Studio job updated")
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def save_rednote_query(self, job_id: str, query: str) -> dict[str, Any]:
        clean_query = query.strip()
        if not _CHINESE_QUERY_PATTERN.fullmatch(clean_query):
            raise ValueError("RedNote query must contain 2 to 24 Chinese characters")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                """
                UPDATE jobs
                SET rednote_query = ?,
                    rednote_query_generation = rednote_query_generation + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (clean_query, now, job_id),
            )
            if result.rowcount == 0:
                raise KeyError(job_id)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.add_log(job_id, "INFO", "RedNote search query generated")
        if row is None:
            raise RuntimeError("RedNote query job could not be loaded")
        return self._row_to_job(row)

    def advance_publish_stage(
        self,
        job_id: str,
        expected_stage: str,
        next_stage: str,
        *,
        error: str = "",
    ) -> bool:
        clean_expected = expected_stage.strip()
        clean_next = next_stage.strip()
        if clean_expected not in _PUBLISH_STAGES or clean_next not in _PUBLISH_STAGES:
            raise ValueError("Unknown publish stage")
        clean_error = error.strip()[:1000]
        now = utc_now()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE jobs
                SET publish_stage = ?,
                    publish_retry_count = publish_retry_count + CASE WHEN ? != '' THEN 1 ELSE 0 END,
                    publish_last_error = ?,
                    updated_at = ?
                WHERE id = ? AND publish_stage = ?
                """,
                (clean_next, clean_error, clean_error, now, job_id, clean_expected),
            )
            if result.rowcount == 0:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                    raise KeyError(job_id)
                return False
        self.add_log(job_id, "ERROR" if clean_error else "INFO", f"Publish stage: {clean_next}")
        return True

    def lock_local_media_publish(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        profile_key: str,
        threads_user_id: str,
        body: str,
        comment_text: str,
        media_mode: str,
        asset_ids: list[str],
    ) -> dict[str, Any]:
        locked = _normalize_publish_lock(
            origin="local",
            idempotency_key=idempotency_key,
            profile_key=profile_key,
            threads_user_id=threads_user_id,
            body=body,
            comment_text=comment_text,
            media_mode=media_mode,
            asset_ids=asset_ids,
            media_urls=[],
        )
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["publish_idempotency_key"]:
                _assert_locked_publish_matches(row, locked)
                return self._row_to_job(row)
            try:
                conn.execute(
                    """
                    UPDATE jobs
                    SET publish_idempotency_key = ?,
                        publish_origin = ?,
                        publish_locked_profile_key = ?,
                        publish_locked_threads_user_id = ?,
                        publish_locked_body = ?,
                        publish_locked_comment = ?,
                        publish_locked_media_mode = ?,
                        publish_locked_asset_ids = ?,
                        publish_media_ids = '[]',
                        publish_media_urls = '[]',
                        publish_child_container_ids = '[]',
                        publish_container_id = '',
                        publish_resume_stage = '',
                        publish_stage = 'draft',
                        publish_last_error = '',
                        updated_at = ?
                    WHERE id = ? AND publish_idempotency_key = ''
                    """,
                    (
                        locked["publish_idempotency_key"],
                        locked["publish_origin"],
                        locked["publish_locked_profile_key"],
                        locked["publish_locked_threads_user_id"],
                        locked["publish_locked_body"],
                        locked["publish_locked_comment"],
                        locked["publish_locked_media_mode"],
                        json.dumps(locked["publish_locked_asset_ids"]),
                        now,
                        job_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("publish idempotency key is already in use") from exc
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.add_log(job_id, "INFO", "Threads media publish payload locked")
        if row is None:
            raise RuntimeError("Locked publish job could not be loaded")
        return self._row_to_job(row)

    def get_or_create_remote_media_publish(
        self,
        *,
        idempotency_key: str,
        profile_key: str,
        threads_user_id: str,
        product_url: str,
        product_name: str,
        body: str,
        comment_text: str,
        media_mode: str,
        media_urls: list[str],
    ) -> tuple[dict[str, Any], bool]:
        clean_product_url = product_url.strip()
        clean_product_name = product_name.strip()
        if not clean_product_url or not clean_product_name:
            raise ValueError("product URL and name are required")
        locked = _normalize_publish_lock(
            origin="remote",
            idempotency_key=idempotency_key,
            profile_key=profile_key,
            threads_user_id=threads_user_id,
            body=body,
            comment_text=comment_text,
            media_mode=media_mode,
            asset_ids=[],
            media_urls=media_urls,
        )
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE publish_idempotency_key = ?",
                (locked["publish_idempotency_key"],),
            ).fetchone()
            if row is not None:
                _assert_locked_publish_matches(
                    row,
                    locked,
                    product_url=clean_product_url,
                    product_name=clean_product_name,
                )
                return self._row_to_job(row), False
            job_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO jobs (
                    id, product_url, product_name, status, title, sns_draft,
                    sns_final, tags, selected_profile_key, media_mode,
                    publish_stage, publish_idempotency_key, publish_origin,
                    publish_locked_profile_key, publish_locked_threads_user_id,
                    publish_locked_body,
                    publish_locked_comment, publish_locked_media_mode,
                    publish_locked_asset_ids, publish_media_ids,
                    publish_media_urls, publish_child_container_ids,
                    publish_container_id, publish_resume_stage,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, ?, 'THREADS_DRAFT_READY', ?, ?, ?, ?, ?, ?,
                    'draft', ?, 'remote', ?, ?, ?, ?, ?, '[]', '[]', ?, '[]', '', '', ?, ?
                )
                """,
                (
                    job_id,
                    clean_product_url,
                    clean_product_name,
                    f"{clean_product_name} Threads",
                    locked["publish_locked_body"],
                    locked["publish_locked_comment"],
                    json.dumps(["쿠팡파트너스", "Threads"], ensure_ascii=False),
                    locked["publish_locked_profile_key"],
                    locked["publish_locked_media_mode"],
                    locked["publish_idempotency_key"],
                    locked["publish_locked_profile_key"],
                    locked["publish_locked_threads_user_id"],
                    locked["publish_locked_body"],
                    locked["publish_locked_comment"],
                    locked["publish_locked_media_mode"],
                    json.dumps(locked["publish_media_urls"]),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.add_log(job_id, "INFO", "Remote Threads media publish claimed")
        if row is None:
            raise RuntimeError("Remote publish job could not be loaded")
        return self._row_to_job(row), True

    def get_publish_job_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        clean_key = idempotency_key.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE publish_idempotency_key = ?",
                (clean_key,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def acquire_remote_publish_lease(
        self,
        job_id: str,
        owner_token: str,
        ttl_seconds: int = REMOTE_PUBLISH_LEASE_TTL_SECONDS,
    ) -> int | None:
        clean_owner = _normalize_publish_lease_owner(owner_token)
        now, expires_at = _publish_lease_window(ttl_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT publish_origin FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if str(job["publish_origin"] or "") != "remote":
                raise ValueError("Remote publish lease requires a remote publish job")
            lease = conn.execute(
                "SELECT * FROM remote_publish_leases WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if lease is not None and str(lease["expires_at"] or "") > now:
                if str(lease["owner_token"] or "") != clean_owner:
                    return None
                conn.execute(
                    """
                    UPDATE remote_publish_leases
                    SET expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND owner_token = ? AND fence = ?
                    """,
                    (expires_at, now, job_id, clean_owner, int(lease["fence"])),
                )
                return int(lease["fence"])

            next_fence = int(lease["fence"] if lease is not None else 0) + 1
            conn.execute(
                """
                INSERT INTO remote_publish_leases (
                    job_id, owner_token, fence, expires_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    fence = excluded.fence,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (job_id, clean_owner, next_fence, expires_at, now),
            )
        return next_fence

    def renew_remote_publish_lease(
        self,
        job_id: str,
        owner_token: str,
        fence: int,
        ttl_seconds: int = REMOTE_PUBLISH_LEASE_TTL_SECONDS,
    ) -> bool:
        clean_owner, clean_fence = _normalize_publish_lease_identity(
            owner_token,
            fence,
        )
        now, expires_at = _publish_lease_window(ttl_seconds)
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE remote_publish_leases
                SET expires_at = ?, updated_at = ?
                WHERE job_id = ?
                  AND owner_token = ?
                  AND fence = ?
                  AND expires_at > ?
                """,
                (expires_at, now, job_id, clean_owner, clean_fence, now),
            )
            if result.rowcount == 0 and conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise KeyError(job_id)
        return result.rowcount == 1

    def release_remote_publish_lease(
        self,
        job_id: str,
        owner_token: str,
        fence: int,
    ) -> bool:
        clean_owner, clean_fence = _normalize_publish_lease_identity(
            owner_token,
            fence,
        )
        now = utc_now()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE remote_publish_leases
                SET owner_token = '', expires_at = '', updated_at = ?
                WHERE job_id = ? AND owner_token = ? AND fence = ?
                """,
                (now, job_id, clean_owner, clean_fence),
            )
            if result.rowcount == 0 and conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone() is None:
                raise KeyError(job_id)
        return result.rowcount == 1

    def checkpoint_publish(
        self,
        job_id: str,
        expected_stage: str,
        next_stage: str,
        *,
        lease_owner: str = "",
        lease_fence: int | None = None,
        **fields: Any,
    ) -> bool:
        clean_expected = expected_stage.strip()
        clean_next = next_stage.strip()
        if clean_expected not in _PUBLISH_STAGES or clean_next not in _PUBLISH_STAGES:
            raise ValueError("Unknown publish stage")
        normalized_fields = {
            _PUBLISH_CHECKPOINT_ALIASES.get(field, field): value
            for field, value in fields.items()
        }
        unsupported = sorted(set(normalized_fields) - _PUBLISH_CHECKPOINT_FIELDS)
        if unsupported:
            raise ValueError(f"Unsupported publish checkpoint field: {unsupported[0]}")
        normalized: dict[str, Any] = {}
        for field, value in normalized_fields.items():
            if field in _PUBLISH_JSON_FIELDS:
                if not isinstance(value, list):
                    raise ValueError(f"{field} must be a list")
                normalized[field] = json.dumps([str(item).strip() for item in value])
            else:
                normalized[field] = "" if value is None else str(value).strip()
        now = utc_now()
        assignments = ["publish_stage = ?", "publish_last_error = ''", "publish_resume_stage = ''"]
        values: list[Any] = [clean_next]
        for field, value in normalized.items():
            assignments.append(f"{field} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.extend((now, job_id, clean_expected))
        lease_clause = ""
        if lease_owner or lease_fence is not None:
            clean_owner, clean_fence = _normalize_publish_lease_identity(
                lease_owner,
                lease_fence,
            )
            lease_clause = (
                " AND EXISTS ("
                "SELECT 1 FROM remote_publish_leases AS lease "
                "WHERE lease.job_id = jobs.id "
                "AND lease.owner_token = ? AND lease.fence = ? "
                "AND lease.expires_at > ?"
                ")"
            )
            values.extend((clean_owner, clean_fence, now))
        with self._connect() as conn:
            result = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                f"WHERE id = ? AND publish_stage = ?{lease_clause}",
                values,
            )
            if result.rowcount == 0:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                    raise KeyError(job_id)
                return False
        self.add_log(job_id, "INFO", f"Publish stage: {clean_next}")
        return True

    def fail_publish(
        self,
        job_id: str,
        expected_stage: str,
        *,
        resume_stage: str,
        error: str,
        lease_owner: str = "",
        lease_fence: int | None = None,
    ) -> bool:
        clean_expected = expected_stage.strip()
        clean_resume = resume_stage.strip()
        if clean_expected not in _PUBLISH_STAGES or clean_resume not in _PUBLISH_STAGES:
            raise ValueError("Unknown publish stage")
        clean_error = error.strip()[:1000]
        if not clean_error:
            raise ValueError("Publish failure error is required")
        now = utc_now()
        lease_clause = ""
        lease_values: tuple[Any, ...] = ()
        if lease_owner or lease_fence is not None:
            clean_owner, clean_fence = _normalize_publish_lease_identity(
                lease_owner,
                lease_fence,
            )
            lease_clause = (
                " AND EXISTS ("
                "SELECT 1 FROM remote_publish_leases AS lease "
                "WHERE lease.job_id = jobs.id "
                "AND lease.owner_token = ? AND lease.fence = ? "
                "AND lease.expires_at > ?"
                ")"
            )
            lease_values = (clean_owner, clean_fence, now)
        with self._connect() as conn:
            result = conn.execute(
                f"""
                UPDATE jobs
                SET publish_stage = 'failed',
                    publish_resume_stage = ?,
                    publish_retry_count = publish_retry_count + 1,
                    publish_last_error = ?,
                    updated_at = ?
                WHERE id = ? AND publish_stage = ?{lease_clause}
                """,
                (
                    clean_resume,
                    clean_error,
                    now,
                    job_id,
                    clean_expected,
                    *lease_values,
                ),
            )
            if result.rowcount == 0:
                if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                    raise KeyError(job_id)
                return False
        self.add_log(job_id, "ERROR", clean_error)
        return True

    def mark_publish_outcome_unknown(
        self,
        job_id: str,
        expected_stage: str,
        *,
        error: str,
        lease_owner: str = "",
        lease_fence: int | None = None,
        **fields: Any,
    ) -> bool:
        clean_expected = expected_stage.strip()
        if clean_expected not in _PUBLISH_STAGES:
            raise ValueError("Unknown publish stage")
        clean_error = error.strip()[:1000]
        if not clean_error:
            raise ValueError("Unknown publish outcome error is required")
        normalized_fields = {
            _PUBLISH_CHECKPOINT_ALIASES.get(field, field): value
            for field, value in fields.items()
        }
        unsupported = sorted(set(normalized_fields) - _PUBLISH_CHECKPOINT_FIELDS)
        if unsupported:
            raise ValueError(f"Unsupported publish checkpoint field: {unsupported[0]}")
        normalized: dict[str, Any] = {}
        for field, value in normalized_fields.items():
            if field in _PUBLISH_JSON_FIELDS:
                if not isinstance(value, list):
                    raise ValueError(f"{field} must be a list")
                normalized[field] = json.dumps([str(item).strip() for item in value])
            else:
                normalized[field] = "" if value is None else str(value).strip()
        now = utc_now()
        assignments = [
            "publish_stage = 'outcome_unknown'",
            "publish_resume_stage = ''",
            "publish_last_error = ?",
        ]
        values: list[Any] = [clean_error]
        for field, value in normalized.items():
            assignments.append(f"{field} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.extend((now, job_id, clean_expected))
        lease_clause = ""
        if lease_owner or lease_fence is not None:
            clean_owner, clean_fence = _normalize_publish_lease_identity(
                lease_owner,
                lease_fence,
            )
            lease_clause = (
                " AND EXISTS ("
                "SELECT 1 FROM remote_publish_leases AS lease "
                "WHERE lease.job_id = jobs.id "
                "AND lease.owner_token = ? AND lease.fence = ? "
                "AND lease.expires_at > ?"
                ")"
            )
            values.extend((clean_owner, clean_fence, now))
        with self._connect() as conn:
            result = conn.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                f"WHERE id = ? AND publish_stage = ?{lease_clause}",
                values,
            )
            if result.rowcount == 0:
                if conn.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
                ).fetchone() is None:
                    raise KeyError(job_id)
                return False
        self.add_log(job_id, "ERROR", clean_error)
        return True

    def resume_failed_publish(
        self,
        job_id: str,
        *,
        lease_owner: str = "",
        lease_fence: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        lease_clause = ""
        lease_values: tuple[Any, ...] = ()
        if lease_owner or lease_fence is not None:
            clean_owner, clean_fence = _normalize_publish_lease_identity(
                lease_owner,
                lease_fence,
            )
            lease_clause = (
                " AND EXISTS ("
                "SELECT 1 FROM remote_publish_leases AS lease "
                "WHERE lease.job_id = jobs.id "
                "AND lease.owner_token = ? AND lease.fence = ? "
                "AND lease.expires_at > ?"
                ")"
            )
            lease_values = (clean_owner, clean_fence, now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["publish_stage"] != "failed":
                raise ValueError("Publish job is not failed")
            resume_stage = str(row["publish_resume_stage"] or "").strip()
            if resume_stage not in _PUBLISH_STAGES or resume_stage == "failed":
                raise ValueError("Publish resume stage is invalid")
            result = conn.execute(
                f"""
                UPDATE jobs
                SET publish_stage = ?, publish_resume_stage = '',
                    publish_last_error = '', updated_at = ?
                WHERE id = ? AND publish_stage = 'failed'{lease_clause}
                """,
                (resume_stage, now, job_id, *lease_values),
            )
            if result.rowcount == 0:
                raise PermissionError("Remote publish lease changed")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.add_log(job_id, "INFO", f"Publish resumed at: {resume_stage}")
        if row is None:
            raise RuntimeError("Resumed publish job could not be loaded")
        return self._row_to_job(row)

    def get_studio_preview(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            variant_rows = conn.execute(
                """
                SELECT * FROM copy_variants
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchall()
            asset_rows = conn.execute(
                """
                SELECT * FROM rednote_assets
                WHERE job_id = ?
                ORDER BY sort_order ASC, id ASC
                """,
                (job_id,),
            ).fetchall()

        job = self._row_to_job(job_row)
        variants = sorted(
            (self._row_to_copy_variant(row) for row in variant_rows),
            key=_copy_variant_sort_key,
        )
        assets = [self._row_to_rednote_asset(row) for row in asset_rows]
        selected_variant_id = job["selected_copy_variant_id"]
        selected_variant = next(
            (
                variant
                for variant in variants
                if variant["id"] == selected_variant_id and variant["selected"]
            ),
            None,
        )
        if job["publish_idempotency_key"]:
            assets_by_id = {asset["id"]: asset for asset in assets}
            selected_assets = [
                assets_by_id[asset_id]
                for asset_id in job["publish_locked_asset_ids"]
                if asset_id in assets_by_id
            ]
        else:
            selected_assets = [asset for asset in assets if asset["selected"]]
        return {
            "job": job,
            "copy_variants": variants,
            "selected_copy_variant": selected_variant,
            "rednote_assets": assets,
            "selected_rednote_assets": selected_assets,
        }

    def get_known_product_context(self, product_url: str) -> dict[str, str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT product_name, image_url
                FROM jobs
                WHERE product_url = ?
                  AND product_name != ''
                  AND product_name != '상품명 자동 확인 필요'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (product_url.strip(),),
            ).fetchone()
        if row is None:
            return {}
        return {"product_name": row["product_name"], "image_url": row["image_url"]}

    def get_known_campaign_context(self, product_url: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT title, sns_draft, image_brief, blog_final, sns_final, tags, image_url
                FROM jobs
                WHERE product_url = ?
                  AND status = 'CAMPAIGN_READY'
                  AND sns_draft != ''
                  AND sns_draft NOT LIKE '%상품 상세를 자동으로 충분히 읽지 못했습니다%'
                  AND product_name != '상품명 자동 확인 필요'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (product_url.strip(),),
            ).fetchone()
        if row is None:
            return {}
        context = dict(row)
        try:
            context["tags"] = json.loads(context.get("tags") or "[]")
        except json.JSONDecodeError:
            context["tags"] = []
        return context

    def update_job_draft(
        self,
        job_id: str,
        title: str,
        draft: str,
        tags: list[str] | None = None,
        image_url: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            if image_url is None:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'DRAFTED',
                        title = ?,
                        draft = ?,
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (title, draft, json.dumps(tags or [], ensure_ascii=False), now, job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'DRAFTED',
                        title = ?,
                        draft = ?,
                        tags = ?,
                        image_url = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (title, draft, json.dumps(tags or [], ensure_ascii=False), image_url, now, job_id),
                )
        self.add_log(job_id, "INFO", "Draft generated")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_job_campaign(
        self,
        job_id: str,
        sns_draft: str,
        image_brief: str,
        blog_final: str,
        sns_final: str,
        title: str,
        tags: list[str] | None = None,
        image_url: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            if image_url is None:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'CAMPAIGN_READY',
                        title = ?,
                        draft = ?,
                        sns_draft = ?,
                        image_brief = ?,
                        blog_final = ?,
                        sns_final = ?,
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        blog_final,
                        sns_draft,
                        image_brief,
                        blog_final,
                        sns_final,
                        json.dumps(tags or [], ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'CAMPAIGN_READY',
                        title = ?,
                        draft = ?,
                        sns_draft = ?,
                        image_brief = ?,
                        blog_final = ?,
                        sns_final = ?,
                        image_url = ?,
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        blog_final,
                        sns_draft,
                        image_brief,
                        blog_final,
                        sns_final,
                        image_url.strip(),
                        json.dumps(tags or [], ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
        self.add_log(job_id, "INFO", "Campaign generated")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_job_generated_image(self, job_id: str, generated_image_url: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET generated_image_url = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (generated_image_url.strip(), now, job_id),
            )
        self.add_log(job_id, "INFO", "Generated ad image saved")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_job_threads_draft(
        self,
        job_id: str,
        text: str,
        comment_text: str = "",
        title: str = "",
        tags: list[str] | None = None,
        image_url: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            if image_url is None:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'THREADS_DRAFT_READY',
                        title = ?,
                        sns_draft = ?,
                        sns_final = ?,
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title.strip(),
                        text,
                        comment_text.strip() or text,
                        json.dumps(tags or [], ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'THREADS_DRAFT_READY',
                        title = ?,
                        sns_draft = ?,
                        sns_final = ?,
                        image_url = ?,
                        tags = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title.strip(),
                        text,
                        comment_text.strip() or text,
                        image_url.strip(),
                        json.dumps(tags or [], ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
        self.add_log(job_id, "INFO", "Threads draft generated")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def add_media_candidate(
        self,
        job_id: str,
        source: str,
        source_url: str = "",
        image_url: str = "",
        timestamp_label: str = "",
        title: str = "",
        creator: str = "",
        notes: str = "",
        no_captions: bool = False,
        no_tts: bool = False,
        product_visible: bool = False,
        permission_reviewed: bool = False,
    ) -> dict[str, Any]:
        if self.get_job(job_id) is None:
            raise KeyError(job_id)
        now = utc_now()
        candidate_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO media_candidates (
                    id, job_id, source, source_url, image_url, timestamp_label,
                    title, creator, notes, no_captions, no_tts, product_visible,
                    permission_reviewed, review_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CANDIDATE', ?, ?)
                """,
                (
                    candidate_id,
                    job_id,
                    source.strip().lower(),
                    source_url.strip(),
                    image_url.strip(),
                    timestamp_label.strip(),
                    title.strip(),
                    creator.strip(),
                    notes.strip(),
                    int(no_captions),
                    int(no_tts),
                    int(product_visible),
                    int(permission_reviewed),
                    now,
                    now,
                ),
            )
        self.add_log(job_id, "INFO", "Media candidate added")
        candidate = self.get_media_candidate(candidate_id)
        if candidate is None:
            raise RuntimeError("Media candidate could not be loaded")
        return candidate

    def get_media_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM media_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        return self._row_to_media_candidate(row) if row else None

    def list_media_candidates(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM media_candidates
                WHERE job_id = ?
                ORDER BY created_at DESC
                """,
                (job_id,),
            ).fetchall()
        return [self._row_to_media_candidate(row) for row in rows]

    def approve_media_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_media_candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        image_url = candidate["image_url"].strip()
        if not image_url:
            raise ValueError("image_url is required before approving a media candidate")
        if candidate["product_visible"]:
            raise ValueError("상품이 보이는 이미지는 사용할 수 없습니다")
        if not candidate["permission_reviewed"]:
            raise ValueError("무료/오픈 이미지 권한 검토가 필요합니다")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE media_candidates
                SET review_status = CASE WHEN id = ? THEN 'APPROVED' ELSE 'REJECTED' END,
                    approved_at = CASE WHEN id = ? THEN ? ELSE approved_at END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (candidate_id, candidate_id, now, now, candidate["job_id"]),
            )
            conn.execute(
                """
                UPDATE jobs
                SET image_url = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (image_url, now, candidate["job_id"]),
            )
        self.add_log(candidate["job_id"], "INFO", "Media candidate approved")
        approved = self.get_media_candidate(candidate_id)
        if approved is None:
            raise KeyError(candidate_id)
        return approved

    def reject_media_candidate(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_media_candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE media_candidates
                SET review_status = 'REJECTED',
                    updated_at = ?
                WHERE id = ?
                """,
                (now, candidate_id),
            )
        self.add_log(candidate["job_id"], "INFO", "Media candidate rejected")
        rejected = self.get_media_candidate(candidate_id)
        if rejected is None:
            raise KeyError(candidate_id)
        return rejected

    def upsert_threads_profile(
        self,
        profile_key: str,
        display_name: str,
        notes: str = "",
    ) -> dict[str, Any]:
        clean_key = profile_key.strip()
        clean_name = display_name.strip()
        if not clean_key or not clean_name:
            raise ValueError("profile_key and display_name are required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads_profiles (
                    profile_key, display_name, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (clean_key, clean_name, notes.strip(), now, now),
            )
        profile = self.get_threads_profile(clean_key)
        if profile is None:
            raise RuntimeError("Threads profile could not be loaded")
        return profile

    def issue_threads_oauth_state(
        self,
        profile_key: str | None,
        ttl_seconds: int = THREADS_OAUTH_STATE_TTL_SECONDS,
    ) -> str:
        try:
            clean_ttl = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("OAuth state ttl_seconds must be a positive integer") from exc
        if clean_ttl <= 0:
            raise ValueError("OAuth state ttl_seconds must be a positive integer")

        if profile_key is None:
            purpose = "import"
            clean_profile_key = ""
        else:
            purpose = "profile"
            clean_profile_key = profile_key.strip()
            if not clean_profile_key:
                raise ValueError("OAuth state profile_key is required")

        now_dt = datetime.now(timezone.utc)
        created_at = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(seconds=clean_ttl)).isoformat(timespec="seconds")
        state = token_urlsafe(32)
        with self._connect() as conn:
            conn.execute("DELETE FROM threads_oauth_states WHERE expires_at <= ?", (created_at,))
            conn.execute(
                """
                INSERT INTO threads_oauth_states (
                    state, purpose, profile_key, expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (state, purpose, clean_profile_key, expires_at, created_at),
            )
        return state

    def consume_threads_oauth_state(self, state: str) -> dict[str, str] | None:
        clean_state = state.strip()
        if not clean_state:
            return None
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT purpose, profile_key, expires_at
                FROM threads_oauth_states
                WHERE state = ?
                """,
                (clean_state,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM threads_oauth_states WHERE state = ?", (clean_state,))
            if row["expires_at"] <= now:
                return None
            purpose = str(row["purpose"] or "")
            profile_key = str(row["profile_key"] or "")
            if purpose not in {"profile", "import"}:
                return None
            if purpose == "profile" and not profile_key:
                return None
        return {"purpose": purpose, "profile_key": profile_key}

    def list_threads_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM threads_profiles
                WHERE threads_user_id != ''
                  AND access_token != ''
                ORDER BY display_name, profile_key
                """
            ).fetchall()
        return [self._row_to_threads_profile(row) for row in rows]

    def list_threads_publish_records(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    jobs.id AS job_id,
                    jobs.product_name,
                    jobs.product_url,
                    jobs.threads_profile_key AS profile_key,
                    jobs.threads_post_id,
                    jobs.threads_reply_id,
                    jobs.threads_permalink,
                    jobs.threads_published_at,
                    jobs.threads_views,
                    jobs.threads_likes,
                    jobs.threads_replies,
                    jobs.threads_reposts,
                    jobs.threads_quotes,
                    jobs.threads_shares,
                    jobs.threads_insights_at,
                    jobs.threads_insights_error,
                    CASE
                        WHEN TRIM(jobs.publish_locked_body) != '' THEN
                            '본문:' || CHAR(10) || jobs.publish_locked_body ||
                            CASE
                                WHEN TRIM(jobs.publish_locked_comment) != '' THEN
                                    CHAR(10) || CHAR(10) || '댓글:' || CHAR(10) ||
                                    jobs.publish_locked_comment
                                ELSE ''
                            END
                        ELSE jobs.sns_final
                    END AS published_text,
                    threads_profiles.display_name,
                    threads_profiles.username
                FROM jobs
                LEFT JOIN threads_profiles
                  ON threads_profiles.profile_key = jobs.threads_profile_key
                WHERE jobs.status = 'THREADS_PUBLISHED'
                  AND jobs.threads_post_id != ''
                ORDER BY jobs.threads_published_at DESC, jobs.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_threads_publish_record(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    jobs.id AS job_id,
                    jobs.product_name,
                    jobs.product_url,
                    jobs.threads_profile_key AS profile_key,
                    jobs.threads_post_id,
                    jobs.threads_reply_id,
                    jobs.threads_permalink,
                    jobs.threads_published_at,
                    jobs.threads_views,
                    jobs.threads_likes,
                    jobs.threads_replies,
                    jobs.threads_reposts,
                    jobs.threads_quotes,
                    jobs.threads_shares,
                    jobs.threads_insights_at,
                    jobs.threads_insights_error,
                    CASE
                        WHEN TRIM(jobs.publish_locked_body) != '' THEN
                            '본문:' || CHAR(10) || jobs.publish_locked_body ||
                            CASE
                                WHEN TRIM(jobs.publish_locked_comment) != '' THEN
                                    CHAR(10) || CHAR(10) || '댓글:' || CHAR(10) ||
                                    jobs.publish_locked_comment
                                ELSE ''
                            END
                        ELSE jobs.sns_final
                    END AS published_text,
                    threads_profiles.display_name,
                    threads_profiles.username
                FROM jobs
                LEFT JOIN threads_profiles
                  ON threads_profiles.profile_key = jobs.threads_profile_key
                WHERE jobs.id = ?
                  AND jobs.status = 'THREADS_PUBLISHED'
                  AND jobs.threads_post_id != ''
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_threads_publish_record(self, job_id: str) -> bool:
        clean_job_id = job_id.strip()
        if not clean_job_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM jobs
                WHERE id = ?
                  AND status = 'THREADS_PUBLISHED'
                  AND threads_post_id != ''
                """,
                (clean_job_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM copy_variants WHERE job_id = ?", (clean_job_id,))
            conn.execute("DELETE FROM rednote_assets WHERE job_id = ?", (clean_job_id,))
            conn.execute("DELETE FROM media_candidates WHERE job_id = ?", (clean_job_id,))
            conn.execute("DELETE FROM logs WHERE job_id = ?", (clean_job_id,))
            conn.execute("DELETE FROM jobs WHERE id = ?", (clean_job_id,))
        return True

    def get_threads_profile(
        self,
        profile_key: str,
        include_token: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads_profiles WHERE profile_key = ?",
                (profile_key.strip(),),
            ).fetchone()
        return self._row_to_threads_profile(row, include_token=include_token) if row else None

    def save_threads_profile_token(
        self,
        profile_key: str,
        threads_user_id: str,
        username: str,
        access_token: str,
        expires_in: int | None = None,
    ) -> dict[str, Any]:
        clean_key = profile_key.strip()
        expires_at = ""
        if expires_in:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO threads_profiles (
                    profile_key, display_name, threads_user_id, username,
                    access_token, expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_key) DO UPDATE SET
                    threads_user_id = excluded.threads_user_id,
                    username = excluded.username,
                    access_token = excluded.access_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_key,
                    clean_key,
                    threads_user_id.strip(),
                    username.strip(),
                    access_token.strip(),
                    expires_at,
                    now,
                    now,
                ),
            )
        profile = self.get_threads_profile(clean_key)
        if profile is None:
            raise RuntimeError("Threads profile could not be loaded")
        return profile

    def disconnect_threads_profile(self, profile_key: str) -> dict[str, Any] | None:
        clean_key = profile_key.strip()
        now = utc_now()
        with self._connect() as conn:
            result = conn.execute(
                """
                UPDATE threads_profiles
                SET threads_user_id = '',
                    username = '',
                    access_token = '',
                    expires_at = '',
                    updated_at = ?
                WHERE profile_key = ?
                """,
                (now, clean_key),
            )
            if result.rowcount == 0:
                return None
        profile = self.get_threads_profile(clean_key)
        if profile is None:
            raise RuntimeError("Threads profile could not be loaded")
        return profile

    def mark_threads_published(
        self,
        job_id: str,
        profile_key: str,
        threads_post_id: str,
        threads_reply_id: str = "",
        threads_permalink: str = "",
        published_text: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'THREADS_PUBLISHED',
                    threads_profile_key = ?,
                    threads_post_id = ?,
                    threads_reply_id = ?,
                    threads_permalink = ?,
                    threads_published_at = ?,
                    sns_final = CASE WHEN ? != '' THEN ? WHEN sns_final = '' THEN sns_draft ELSE sns_final END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    profile_key.strip(),
                    threads_post_id.strip(),
                    threads_reply_id.strip(),
                    threads_permalink.strip(),
                    now,
                    published_text.strip(),
                    published_text.strip(),
                    now,
                    job_id,
                ),
            )
        self.add_log(job_id, "INFO", f"Threads published via {profile_key.strip()}")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_threads_permalink(self, job_id: str, permalink: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET threads_permalink = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'THREADS_PUBLISHED'
                """,
                (permalink.strip(), now, job_id),
            )
        self.add_log(job_id, "INFO", "Threads permalink refreshed")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def update_threads_insights(
        self,
        job_id: str,
        insights: dict[str, Any],
        error: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        clean_error = error.strip()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET threads_views = ?,
                    threads_likes = ?,
                    threads_replies = ?,
                    threads_reposts = ?,
                    threads_quotes = ?,
                    threads_shares = ?,
                    threads_insights_at = ?,
                    threads_insights_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    _safe_metric(insights.get("views")),
                    _safe_metric(insights.get("likes")),
                    _safe_metric(insights.get("replies")),
                    _safe_metric(insights.get("reposts")),
                    _safe_metric(insights.get("quotes")),
                    _safe_metric(insights.get("shares")),
                    now if not clean_error else "",
                    clean_error,
                    now,
                    job_id,
                ),
            )
        self.add_log(job_id, "ERROR" if clean_error else "INFO", clean_error or "Threads insights refreshed")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def mark_publish_handoff(self, job_id: str, message: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'NEEDS_BROWSER_REVIEW',
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (message, now, job_id),
            )
        self.add_log(job_id, "INFO", message)
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def add_log(self, job_id: str | None, level: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO logs (id, job_id, level, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), job_id, level, message, utc_now()),
            )

    def list_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _row_to_job(self, row: sqlite3.Row) -> dict[str, Any]:
        job = dict(row)
        try:
            job["tags"] = json.loads(job.get("tags") or "[]")
        except json.JSONDecodeError:
            job["tags"] = []
        for field in _PUBLISH_JSON_FIELDS:
            try:
                value = json.loads(job.get(field) or "[]")
            except json.JSONDecodeError:
                value = []
            job[field] = value if isinstance(value, list) else []
        return job

    def _row_to_media_candidate(self, row: sqlite3.Row) -> dict[str, Any]:
        candidate = dict(row)
        for key in ("no_captions", "no_tts", "product_visible", "permission_reviewed"):
            candidate[key] = bool(candidate[key])
        return candidate

    def _row_to_copy_variant(self, row: sqlite3.Row) -> dict[str, Any]:
        variant = dict(row)
        variant["selected"] = bool(variant["selected"])
        return variant

    def _row_to_rednote_asset(self, row: sqlite3.Row) -> dict[str, Any]:
        asset = dict(row)
        asset["selected"] = bool(asset["selected"])
        return asset

    def _row_to_threads_profile(
        self,
        row: sqlite3.Row,
        include_token: bool = False,
    ) -> dict[str, Any]:
        profile = dict(row)
        token = profile.get("access_token", "")
        profile["is_connected"] = bool(profile.get("threads_user_id") and token)
        profile["token_preview"] = _token_preview(token)
        if not include_token:
            profile.pop("access_token", None)
        return profile


def _token_preview(token: str) -> str:
    clean = token.strip()
    if not clean:
        return ""
    if len(clean) <= 8:
        return "****"
    return f"{clean[:4]}...{clean[-4:]}"


def _normalize_publish_lease_owner(owner_token: str) -> str:
    clean_owner = str(owner_token or "").strip()
    if not _OPAQUE_ID_PATTERN.fullmatch(clean_owner):
        raise ValueError("Remote publish lease owner must be an opaque identifier")
    return clean_owner


def _normalize_publish_lease_identity(
    owner_token: str,
    fence: int | None,
) -> tuple[str, int]:
    clean_owner = _normalize_publish_lease_owner(owner_token)
    try:
        clean_fence = int(fence) if fence is not None else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("Remote publish lease fence must be a positive integer") from exc
    if clean_fence <= 0:
        raise ValueError("Remote publish lease fence must be a positive integer")
    return clean_owner, clean_fence


def _publish_lease_window(ttl_seconds: int) -> tuple[str, str]:
    try:
        clean_ttl = int(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Remote publish lease ttl must be a positive integer") from exc
    if clean_ttl <= 0:
        raise ValueError("Remote publish lease ttl must be a positive integer")
    now_dt = datetime.now(timezone.utc)
    return (
        now_dt.isoformat(timespec="seconds"),
        (now_dt + timedelta(seconds=clean_ttl)).isoformat(timespec="seconds"),
    )


def _copy_variant_sort_key(variant: dict[str, Any]) -> tuple[int, str, str]:
    persona_key = str(variant.get("persona_key") or "")
    return (
        _COPY_VARIANT_ORDER.get(persona_key, len(_COPY_VARIANT_ORDER)),
        persona_key,
        str(variant.get("id") or ""),
    )


def _normalize_publish_lock(
    *,
    origin: str,
    idempotency_key: str,
    profile_key: str,
    threads_user_id: str,
    body: str,
    comment_text: str,
    media_mode: str,
    asset_ids: list[str],
    media_urls: list[str],
) -> dict[str, Any]:
    clean_origin = origin.strip().lower()
    clean_key = idempotency_key.strip()
    clean_profile = profile_key.strip()
    clean_threads_user_id = threads_user_id.strip()
    clean_body = body.strip()
    clean_comment = comment_text.strip()
    clean_mode = media_mode.strip().lower()
    clean_asset_ids = [str(asset_id).strip() for asset_id in asset_ids]
    clean_media_urls = [str(url).strip() for url in media_urls]
    if clean_origin not in _PUBLISH_ORIGINS:
        raise ValueError("publish origin is invalid")
    if not 16 <= len(clean_key) <= 128:
        raise ValueError("publish idempotency key must be 16 to 128 characters")
    if not clean_profile or not clean_threads_user_id or not clean_body:
        raise ValueError("publish profile identity and body are required")
    selected_values = clean_asset_ids if clean_origin == "local" else clean_media_urls
    if any(not value for value in selected_values) or len(selected_values) != len(
        set(selected_values)
    ):
        raise ValueError("publish media values must be unique and non-empty")
    if clean_mode == "video":
        if len(selected_values) != 1:
            raise ValueError("video mode requires exactly one media item")
    elif clean_mode == "images":
        if not 2 <= len(selected_values) <= 5:
            raise ValueError("images mode requires 2 to 5 media items")
    elif clean_mode == "mixed":
        if len(selected_values) != 2:
            raise ValueError("mixed mode requires one video and one image")
    else:
        raise ValueError("publish media mode must be video, images, or mixed")
    return {
        "publish_origin": clean_origin,
        "publish_idempotency_key": clean_key,
        "publish_locked_profile_key": clean_profile,
        "publish_locked_threads_user_id": clean_threads_user_id,
        "publish_locked_body": clean_body,
        "publish_locked_comment": clean_comment,
        "publish_locked_media_mode": clean_mode,
        "publish_locked_asset_ids": clean_asset_ids,
        "publish_media_urls": clean_media_urls,
    }


def _assert_locked_publish_matches(
    row: sqlite3.Row,
    locked: dict[str, Any],
    *,
    product_url: str = "",
    product_name: str = "",
) -> None:
    comparisons = {
        "publish_origin": locked["publish_origin"],
        "publish_idempotency_key": locked["publish_idempotency_key"],
        "publish_locked_profile_key": locked["publish_locked_profile_key"],
        "publish_locked_threads_user_id": locked["publish_locked_threads_user_id"],
        "publish_locked_body": locked["publish_locked_body"],
        "publish_locked_comment": locked["publish_locked_comment"],
        "publish_locked_media_mode": locked["publish_locked_media_mode"],
    }
    if product_url:
        comparisons["product_url"] = product_url
    if product_name:
        comparisons["product_name"] = product_name
    for field, expected in comparisons.items():
        if str(row[field] or "") != expected:
            raise ValueError("publish idempotency key is bound to a different locked payload")
    json_comparisons = {
        "publish_locked_asset_ids": locked["publish_locked_asset_ids"],
        "publish_media_urls": locked["publish_media_urls"],
    }
    for field, expected in json_comparisons.items():
        try:
            actual = json.loads(row[field] or "[]")
        except json.JSONDecodeError:
            actual = []
        if actual != expected:
            raise ValueError("publish idempotency key is bound to a different locked payload")


def _is_canonical_rednote_url(note_id: str, canonical_url: str) -> bool:
    return bool(
        _REDNOTE_NOTE_ID_PATTERN.fullmatch(note_id)
        and canonical_url == f"https://www.rednote.com/explore/{note_id}"
    )
