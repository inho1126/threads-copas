from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import socket
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_OPERATION_TIMEOUT = 45
DOWNLOAD_TIMEOUT = 180
SIDECAR_USER_AGENT = "CoupangRedNoteStudio/1.0"

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_NOTE_ID = re.compile(r"^[0-9a-f]{24}$")
_RANGE_HEADER = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")


@dataclass(frozen=True)
class SidecarBinaryResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class RedNoteSidecarError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


SidecarTransport = Callable[..., dict[str, Any] | list[Any] | SidecarBinaryResponse]


class RedNoteSidecarClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        transport: SidecarTransport | None = None,
    ) -> None:
        self.base_url = _validated_loopback_base_url(base_url)
        self.api_key = api_key.strip()
        self._transport = transport or _urlopen_transport

    def health(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/health", timeout=DEFAULT_CONNECT_TIMEOUT)

    def search(self, query: str) -> dict[str, Any]:
        clean_query = str(query or "").strip()[:80]
        if not clean_query:
            raise RedNoteSidecarError("RedNote search query is required")
        return self._request_json("POST", "/api/search", data={"query": clean_query})

    def resolve(self, url: str) -> dict[str, Any]:
        canonical_url = _validated_canonical_rednote_url(url)
        return self._request_json("POST", "/api/resolve", data={"url": canonical_url})

    def resolve_search_result(self, search_id: str, result_id: str) -> dict[str, Any]:
        clean_search_id = _validated_opaque_id(search_id, "search")
        clean_result_id = _validated_opaque_id(result_id, "result")
        return self._request_json(
            "POST",
            f"/api/searches/{quote(clean_search_id, safe='')}/results/"
            f"{quote(clean_result_id, safe='')}/resolve",
            data={},
        )

    def create_job(self, session_id: str, candidate: int = 0) -> dict[str, Any]:
        clean_session_id = _validated_opaque_id(session_id, "session")
        if candidate != 0:
            raise RedNoteSidecarError("RedNote media candidate is invalid")
        return self._request_json(
            "POST",
            "/api/jobs",
            data={"sessionId": clean_session_id},
            timeout=DOWNLOAD_TIMEOUT,
        )

    def get_video(self, sidecar_job_id: str, *, range_header: str = "") -> SidecarBinaryResponse:
        clean_job_id = _validated_opaque_id(sidecar_job_id, "job")
        headers: dict[str, str] = {}
        clean_range = range_header.strip()
        if clean_range:
            if not _RANGE_HEADER.fullmatch(clean_range):
                raise RedNoteSidecarError("RedNote video range is invalid")
            headers["Range"] = clean_range
        response = self._request(
            "GET",
            f"/api/jobs/{quote(clean_job_id, safe='')}/video",
            headers=headers,
            timeout=DOWNLOAD_TIMEOUT,
            expect_json=False,
        )
        if not isinstance(response, SidecarBinaryResponse):
            raise RedNoteSidecarError("RedNote local service returned an unexpected response")
        return response

    def upload_frame(
        self,
        sidecar_job_id: str,
        index: int,
        time_ms: int | float,
        jpeg: bytes,
    ) -> dict[str, Any]:
        clean_job_id = _validated_opaque_id(sidecar_job_id, "job")
        if index not in range(1, 6):
            raise RedNoteSidecarError("RedNote frame index is invalid")
        if isinstance(time_ms, bool) or not isinstance(time_ms, (int, float)):
            raise RedNoteSidecarError("RedNote frame timestamp is invalid")
        if not math.isfinite(float(time_ms)) or float(time_ms) < 0:
            raise RedNoteSidecarError("RedNote frame timestamp is invalid")
        if not isinstance(jpeg, bytes) or not jpeg:
            raise RedNoteSidecarError("RedNote JPEG body is required")
        timestamp = str(int(time_ms)) if float(time_ms).is_integer() else str(float(time_ms))
        return self._request_json(
            "PUT",
            f"/api/jobs/{quote(clean_job_id, safe='')}/frames/{index}?{urlencode({'timeMs': timestamp})}",
            data=jpeg,
            headers={"Content-Type": "image/jpeg", "Content-Length": str(len(jpeg))},
            timeout=DEFAULT_OPERATION_TIMEOUT,
        )

    def complete(self, sidecar_job_id: str) -> dict[str, Any]:
        clean_job_id = _validated_opaque_id(sidecar_job_id, "job")
        return self._request_json(
            "POST",
            f"/api/jobs/{quote(clean_job_id, safe='')}/complete",
            data={},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_OPERATION_TIMEOUT,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            path,
            data=data,
            headers=headers,
            timeout=timeout,
            expect_json=True,
        )
        if not isinstance(response, dict):
            raise RedNoteSidecarError("RedNote local service returned an unexpected response")
        return response

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int,
        expect_json: bool,
    ) -> dict[str, Any] | list[Any] | SidecarBinaryResponse:
        request_headers = {
            "Accept": "application/json" if expect_json else "video/mp4",
            "Cache-Control": "no-store",
            "User-Agent": SIDECAR_USER_AGENT,
            **(headers or {}),
        }
        if self.api_key:
            request_headers["X-RedNote-Sidecar-Key"] = self.api_key
        try:
            return self._transport(
                method,
                f"{self.base_url}{path}",
                data=data,
                headers=request_headers,
                timeout=timeout,
                expect_json=expect_json,
            )
        except RedNoteSidecarError:
            raise
        except Exception as exc:
            raise RedNoteSidecarError(
                "RedNote local service request failed",
                retryable=True,
            ) from exc


def _validated_loopback_base_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise RedNoteSidecarError("RedNote local service URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RedNoteSidecarError("RedNote local service URL must be loopback-only")
    return raw.rstrip("/")


def _validated_opaque_id(value: str, label: str) -> str:
    clean = str(value or "").strip()
    if not _OPAQUE_ID.fullmatch(clean):
        raise RedNoteSidecarError(f"RedNote {label} identifier is invalid")
    return clean


def _validated_canonical_rednote_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise RedNoteSidecarError("RedNote URL is invalid") from exc
    note_id = parsed.path.removeprefix("/explore/")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.rednote.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != f"/explore/{note_id}"
        or not _NOTE_ID.fullmatch(note_id)
        or parsed.query
        or parsed.fragment
    ):
        raise RedNoteSidecarError("RedNote canonical URL is invalid")
    return f"https://www.rednote.com/explore/{note_id}"


def _urlopen_transport(
    method: str,
    url: str,
    *,
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_OPERATION_TIMEOUT,
    expect_json: bool = True,
) -> dict[str, Any] | list[Any] | SidecarBinaryResponse:
    body: bytes | None = None
    request_headers = dict(headers or {})
    if isinstance(data, dict):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif isinstance(data, bytes):
        body = data
    request = Request(url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status_code = int(getattr(response, "status", 200))
            response_headers = {
                str(key).lower(): str(value)
                for key, value in getattr(response, "headers", {}).items()
            }
    except HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:
            payload = b""
        code, retryable = _safe_sidecar_error_metadata(payload)
        raise RedNoteSidecarError(
            "RedNote local service returned an error",
            status_code=exc.code,
            code=code,
            retryable=retryable or exc.code >= 500,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RedNoteSidecarError(
            "RedNote local service request timed out",
            retryable=True,
        ) from exc
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise RedNoteSidecarError(
                "RedNote local service request timed out",
                retryable=True,
            ) from exc
        raise RedNoteSidecarError(
            "RedNote local service is unavailable",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise RedNoteSidecarError(
            "RedNote local service is unavailable",
            retryable=True,
        ) from exc

    if not expect_json:
        return SidecarBinaryResponse(
            status_code=status_code,
            headers=response_headers,
            body=payload,
        )
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RedNoteSidecarError("RedNote local service returned invalid JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise RedNoteSidecarError("RedNote local service returned an unexpected response")
    return parsed


def _safe_sidecar_error_metadata(payload: bytes) -> tuple[str, bool]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", False
    if not isinstance(parsed, dict):
        return "", False
    raw_error = parsed.get("error")
    if not isinstance(raw_error, dict):
        return "", False
    code = raw_error.get("code")
    retryable = raw_error.get("retryable")
    return (str(code)[:64] if isinstance(code, str) else "", retryable is True)
