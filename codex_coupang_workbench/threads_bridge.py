from __future__ import annotations

import base64
import ipaddress
import json
import os
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


BridgeTransport = Callable[
    [str, str],
    dict[str, Any] | list[dict[str, Any]],
]

BRIDGE_USER_AGENT = "ThreadsCopasBridge/1.0 (+https://sinabro-ai.com)"
DEFAULT_BRIDGE_TIMEOUT = 20
PUBLISH_BRIDGE_TIMEOUT = 120
MEDIA_UPLOAD_BRIDGE_TIMEOUT = 120
MEDIA_PUBLISH_BRIDGE_TIMEOUT = 180
MEDIA_UPLOAD_CHUNK_BYTES = 512 * 1024
ALLOW_INSECURE_LOOPBACK_ENV = "THREADS_BRIDGE_ALLOW_INSECURE_LOOPBACK"


class ThreadsBridgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validated_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ThreadsBridgeError("Threads service URL is required")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ThreadsBridgeError("Threads service URL must use a safe HTTPS origin") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ThreadsBridgeError("Threads service URL must use a safe HTTPS origin")
    if parsed.scheme == "https":
        return raw
    allow_loopback = os.environ.get(ALLOW_INSECURE_LOOPBACK_ENV, "").strip() == "1"
    if parsed.scheme == "http" and allow_loopback and _is_loopback_host(parsed.hostname):
        return raw
    raise ThreadsBridgeError("Threads service URL must use HTTPS")


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class ThreadsBridgeClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        transport: Callable[..., dict[str, Any] | list[dict[str, Any]]] | None = None,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        self.api_key = api_key.strip()
        self._transport = transport or _urlopen_json_transport

    def list_profiles(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/api/threads/profiles")
        if not isinstance(response, list):
            raise ThreadsBridgeError("Threads service returned an unexpected profiles response")
        return response

    def list_publish_records(self, refresh_insights: bool = False) -> list[dict[str, Any]]:
        params = {"refresh_insights": "true", "_": str(int(time.time() * 1000))} if refresh_insights else None
        response = self._request("GET", "/api/threads/publish-records", params=params)
        if not isinstance(response, list):
            raise ThreadsBridgeError("Threads service returned an unexpected records response")
        return response

    def refresh_record_insights(self, job_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/api/threads/publish-records/{quote(job_id, safe='')}/insights",
        )
        return _ensure_dict(response)

    def get_record_permalink(self, job_id: str) -> dict[str, Any]:
        response = self._request("POST", f"/api/threads/publish-records/{quote(job_id, safe='')}/permalink")
        return _ensure_dict(response)

    def delete_publish_record(self, job_id: str) -> dict[str, Any]:
        response = self._request("DELETE", f"/api/threads/publish-records/{quote(job_id, safe='')}")
        return _ensure_dict(response)

    def upsert_profile(self, profile_key: str, display_name: str, notes: str = "") -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/threads/profiles",
            data={
                "profile_key": profile_key,
                "display_name": display_name,
                "notes": notes,
            },
        )
        return _ensure_dict(response)

    def start_auth(self, profile_key: str) -> dict[str, str]:
        response = self._request(
            "GET",
            "/api/threads/auth/start",
            params={"profile_key": profile_key},
        )
        return _string_dict(response)

    def start_import(self) -> dict[str, str]:
        response = self._request("GET", "/api/threads/auth/import/start")
        return _string_dict(response)

    def upload_media(self, media_bytes: bytes) -> dict[str, Any]:
        if not isinstance(media_bytes, (bytes, bytearray, memoryview)):
            raise ThreadsBridgeError("Temporary media content must be bytes")
        content = bytes(media_bytes)
        if not content:
            raise ThreadsBridgeError("Temporary media content is empty")
        started = _ensure_dict(
            self._request(
                "POST",
                "/api/threads/media-uploads/start",
                data={"total_bytes": len(content)},
                timeout=MEDIA_UPLOAD_BRIDGE_TIMEOUT,
            )
        )
        upload_id = str(started.get("upload_id") or "").strip()
        if not upload_id:
            raise ThreadsBridgeError("Threads service did not return a media upload id")
        for index, offset in enumerate(range(0, len(content), MEDIA_UPLOAD_CHUNK_BYTES)):
            chunk = content[offset : offset + MEDIA_UPLOAD_CHUNK_BYTES]
            self._request(
                "POST",
                f"/api/threads/media-uploads/{quote(upload_id, safe='')}/parts",
                data={
                    "index": index,
                    "content_base64": base64.b64encode(chunk).decode("ascii"),
                },
                timeout=MEDIA_UPLOAD_BRIDGE_TIMEOUT,
            )
        response = self._request(
            "POST",
            f"/api/threads/media-uploads/{quote(upload_id, safe='')}/complete",
            data={},
            timeout=MEDIA_UPLOAD_BRIDGE_TIMEOUT,
        )
        return _ensure_dict(response)

    def delete_media(self, media_id: str) -> dict[str, Any]:
        clean_media_id = media_id.strip()
        if not clean_media_id:
            raise ThreadsBridgeError("Temporary media id is required")
        response = self._request(
            "DELETE",
            f"/api/threads/media/{quote(clean_media_id, safe='')}",
        )
        return _ensure_dict(response)

    def publish_media(
        self,
        *,
        idempotency_key: str,
        profile_key: str,
        product_url: str,
        product_name: str,
        text: str,
        comment_text: str,
        media_mode: str,
        media_urls: list[str],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/threads/remote-media-publish",
            data={
                "idempotency_key": idempotency_key,
                "profile_key": profile_key,
                "product_url": product_url,
                "product_name": product_name,
                "text": text,
                "comment_text": comment_text,
                "media_mode": media_mode,
                "media_urls": list(media_urls),
            },
            timeout=MEDIA_PUBLISH_BRIDGE_TIMEOUT,
        )
        return _ensure_dict(response)

    def publish(
        self,
        *,
        profile_key: str,
        product_url: str,
        product_name: str,
        text: str,
        comment_text: str = "",
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/threads/remote-publish",
            data={
                "profile_key": profile_key,
                "product_url": product_url,
                "product_name": product_name,
                "text": text,
                "comment_text": comment_text,
            },
            timeout=PUBLISH_BRIDGE_TIMEOUT,
        )
        return _ensure_dict(response)

    def refresh_profile(self, profile_key: str) -> dict[str, Any]:
        response = self._request("POST", f"/api/threads/profiles/{quote(profile_key, safe='')}/refresh")
        return _ensure_dict(response)

    def disconnect_profile(self, profile_key: str) -> dict[str, Any]:
        response = self._request("POST", f"/api/threads/profiles/{quote(profile_key, safe='')}/disconnect")
        return _ensure_dict(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = DEFAULT_BRIDGE_TIMEOUT,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": BRIDGE_USER_AGENT,
        }
        if self.api_key:
            headers["X-Threads-Bridge-Key"] = self.api_key
        return self._transport(method, url, data=data, headers=headers, timeout=timeout)


def _urlopen_json_transport(
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_BRIDGE_TIMEOUT,
) -> dict[str, Any] | list[dict[str, Any]]:
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method.upper())
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        parsed_detail = _extract_error_detail(detail)
        raise ThreadsBridgeError(
            f"Threads service HTTP {exc.code}: {parsed_detail}",
            status_code=exc.code,
            detail=parsed_detail,
        ) from exc
    except (OSError, URLError) as exc:
        raise ThreadsBridgeError(f"Threads service request failed: {exc}") from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ThreadsBridgeError("Threads service returned invalid JSON") from exc
    if not isinstance(parsed, (dict, list)):
        raise ThreadsBridgeError("Threads service returned an unexpected response")
    return parsed


def _extract_error_detail(detail: str) -> Any:
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return detail
    if isinstance(parsed, dict):
        return parsed.get("detail") or parsed
    return detail


def _ensure_dict(response: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ThreadsBridgeError("Threads service returned an unexpected response")
    return response


def _string_dict(response: dict[str, Any] | list[dict[str, Any]]) -> dict[str, str]:
    raw = _ensure_dict(response)
    return {key: str(value) for key, value in raw.items()}
