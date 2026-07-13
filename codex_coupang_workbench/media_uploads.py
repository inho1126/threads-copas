from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Any


DEFAULT_MEDIA_TTL_SECONDS = 24 * 60 * 60
DEFAULT_MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class MediaUploadError(ValueError):
    pass


class TemporaryMediaStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        media_dir: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
        ttl_seconds: int = DEFAULT_MEDIA_TTL_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.media_dir = Path(media_dir) if media_dir is not None else self.db_path.parent / "threads_media"
        self.upload_dir = self.media_dir / "uploads"
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = int(ttl_seconds)
        self._now = now or (lambda: datetime.now(timezone.utc))
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create(self, content: bytes) -> dict[str, Any]:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise MediaUploadError("Temporary media content must be bytes")
        raw_content = bytes(content)
        if len(raw_content) > self.max_bytes:
            raise MediaUploadError("Temporary media is too large")
        mime_type = _sniff_mime_type(raw_content)
        self.cleanup_expired()
        created_at = self._utc_now()
        metadata = {
            "mime_type": mime_type,
            "size": len(raw_content),
            "created_at": created_at.isoformat(timespec="seconds"),
            "expires_at": (created_at + timedelta(seconds=self.ttl_seconds)).isoformat(
                timespec="seconds"
            ),
        }
        for _ in range(5):
            media_id = token_urlsafe(32)
            path = self.media_dir / media_id
            try:
                with path.open("xb") as handle:
                    handle.write(raw_content)
            except FileExistsError:
                continue
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO temporary_media_uploads (
                            media_id, mime_type, size, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            media_id,
                            metadata["mime_type"],
                            metadata["size"],
                            metadata["created_at"],
                            metadata["expires_at"],
                        ),
                    )
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return {"media_id": media_id, **metadata}
        raise MediaUploadError("Could not allocate temporary media id")

    def start_upload(self, total_bytes: int) -> str:
        if not isinstance(total_bytes, int) or not 0 < total_bytes <= self.max_bytes:
            raise MediaUploadError("Temporary media upload size is invalid")
        self.cleanup_expired()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        created_at = self._utc_now()
        for _ in range(5):
            upload_id = token_urlsafe(24)
            path = self.upload_dir / upload_id
            try:
                with path.open("xb"):
                    pass
            except FileExistsError:
                continue
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO temporary_media_upload_sessions (
                        upload_id, total_bytes, received_bytes, next_index, created_at, expires_at
                    ) VALUES (?, ?, 0, 0, ?, ?)
                    """,
                    (
                        upload_id,
                        total_bytes,
                        created_at.isoformat(timespec="seconds"),
                        (created_at + timedelta(seconds=self.ttl_seconds)).isoformat(timespec="seconds"),
                    ),
                )
            return upload_id
        raise MediaUploadError("Could not allocate temporary media upload")

    def append_upload_part(self, upload_id: str, index: int, content: bytes) -> None:
        if not isinstance(content, (bytes, bytearray, memoryview)) or not content:
            raise MediaUploadError("Temporary media part is empty")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT total_bytes, received_bytes, next_index FROM temporary_media_upload_sessions WHERE upload_id = ?",
                (upload_id,),
            ).fetchone()
            if row is None:
                raise MediaUploadError("Temporary media upload not found")
            if int(index) != int(row[2]):
                raise MediaUploadError("Temporary media parts must arrive in order")
            received = int(row[1]) + len(content)
            if received > int(row[0]):
                raise MediaUploadError("Temporary media upload exceeds its declared size")
            with (self.upload_dir / upload_id).open("ab") as handle:
                handle.write(bytes(content))
            connection.execute(
                "UPDATE temporary_media_upload_sessions SET received_bytes = ?, next_index = ? WHERE upload_id = ?",
                (received, int(row[2]) + 1, upload_id),
            )

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT total_bytes, received_bytes FROM temporary_media_upload_sessions WHERE upload_id = ?",
                (upload_id,),
            ).fetchone()
        if row is None or int(row[0]) != int(row[1]):
            raise MediaUploadError("Temporary media upload is incomplete")
        path = self.upload_dir / upload_id
        try:
            return self.create(path.read_bytes())
        finally:
            path.unlink(missing_ok=True)
            with self._connect() as connection:
                connection.execute("DELETE FROM temporary_media_upload_sessions WHERE upload_id = ?", (upload_id,))

    def get(self, media_id: str) -> dict[str, Any] | None:
        clean_media_id = _normalize_media_id(media_id)
        if clean_media_id is None:
            return None
        self.cleanup_expired()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT media_id, mime_type, size, created_at, expires_at
                FROM temporary_media_uploads
                WHERE media_id = ?
                """,
                (clean_media_id,),
            ).fetchone()
        if row is None:
            return None
        if not (self.media_dir / clean_media_id).is_file():
            self.delete(clean_media_id)
            return None
        return {
            "media_id": str(row[0]),
            "mime_type": str(row[1]),
            "size": int(row[2]),
            "created_at": str(row[3]),
            "expires_at": str(row[4]),
        }

    def read(self, media_id: str) -> bytes:
        metadata = self.get(media_id)
        if metadata is None:
            raise MediaUploadError("Temporary media not found")
        try:
            return (self.media_dir / metadata["media_id"]).read_bytes()
        except OSError as exc:
            self.delete(metadata["media_id"])
            raise MediaUploadError("Temporary media not found") from exc

    def delete(self, media_id: str) -> bool:
        clean_media_id = _normalize_media_id(media_id)
        if clean_media_id is None:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM temporary_media_uploads WHERE media_id = ?",
                (clean_media_id,),
            )
        (self.media_dir / clean_media_id).unlink(missing_ok=True)
        return cursor.rowcount > 0

    def cleanup_expired(self) -> int:
        now = self._utc_now().isoformat(timespec="seconds")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT media_id FROM temporary_media_uploads WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM temporary_media_uploads WHERE media_id = ?",
                rows,
            )
        for row in rows:
            (self.media_dir / str(row[0])).unlink(missing_ok=True)
        with self._connect() as connection:
            upload_rows = connection.execute(
                "SELECT upload_id FROM temporary_media_upload_sessions WHERE expires_at <= ?",
                (now,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM temporary_media_upload_sessions WHERE upload_id = ?",
                upload_rows,
            )
        for row in upload_rows:
            (self.upload_dir / str(row[0])).unlink(missing_ok=True)
        return len(rows) + len(upload_rows)

    def _initialize_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS temporary_media_uploads (
                    media_id TEXT PRIMARY KEY,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS temporary_media_upload_sessions (
                    upload_id TEXT PRIMARY KEY,
                    total_bytes INTEGER NOT NULL,
                    received_bytes INTEGER NOT NULL,
                    next_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_temporary_media_uploads_expires_at
                ON temporary_media_uploads(expires_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def _utc_now(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)


def _normalize_media_id(media_id: str) -> str | None:
    clean_media_id = str(media_id).strip()
    if not MEDIA_ID_PATTERN.fullmatch(clean_media_id):
        return None
    return clean_media_id


def _sniff_mime_type(content: bytes) -> str:
    if len(content) >= 4 and content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        box_size = int.from_bytes(content[:4], "big")
        if 8 <= box_size <= len(content):
            return "video/mp4"
    raise MediaUploadError("Temporary media must be a valid JPEG or MP4")
