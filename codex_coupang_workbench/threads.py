from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


Transport = Callable[
    [str, str],
    dict[str, Any],
]

THREADS_AUTH_SCOPE = "threads_basic,threads_content_publish,threads_manage_replies,threads_manage_insights"
THREADS_INSIGHT_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")


class ThreadsApiError(RuntimeError):
    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = bool(outcome_unknown)


class ThreadsApiClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        *,
        transport: Callable[..., dict[str, Any]] | None = None,
        auth_base_url: str = "https://graph.threads.net",
        api_base_url: str = "https://graph.threads.net/v1.0",
    ) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.auth_base_url = auth_base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")
        self._transport = transport or _urlopen_transport

    def build_authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.app_id,
                "redirect_uri": self.redirect_uri,
                "scope": THREADS_AUTH_SCOPE,
                "response_type": "code",
                "state": state,
            }
        )
        return f"https://threads.net/oauth/authorize?{query}"

    def exchange_code_for_short_token(self, code: str) -> dict[str, Any]:
        return self._transport(
            "POST",
            f"{self.auth_base_url}/oauth/access_token",
            data={
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
                "code": code,
            },
        )

    def exchange_for_long_lived_token(self, short_lived_token: str) -> dict[str, Any]:
        return self._transport(
            "GET",
            f"{self.auth_base_url}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": self.app_secret,
                "access_token": short_lived_token,
            },
        )

    def refresh_long_lived_token(self, access_token: str) -> dict[str, Any]:
        return self._transport(
            "GET",
            f"{self.auth_base_url}/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": access_token,
            },
        )

    def fetch_me(self, access_token: str) -> dict[str, Any]:
        return self._transport(
            "GET",
            f"{self.api_base_url}/me",
            params={
                "fields": "id,username,name,threads_profile_picture_url",
                "access_token": access_token,
            },
        )

    def publish_text(self, threads_user_id: str, access_token: str, text: str) -> dict[str, Any]:
        return self._publish_text_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            text=text,
        )

    def publish_image(
        self,
        threads_user_id: str,
        access_token: str,
        text: str,
        image_url: str,
    ) -> dict[str, Any]:
        return self._publish_media_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data={
                "media_type": "IMAGE",
                "image_url": image_url.strip(),
                "text": text,
            },
        )

    def publish_video(
        self,
        threads_user_id: str,
        access_token: str,
        text: str,
        video_url: str,
    ) -> dict[str, Any]:
        return self._publish_media_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data={
                "media_type": "VIDEO",
                "video_url": video_url.strip(),
                "text": text,
            },
        )

    def publish_image_carousel(
        self,
        threads_user_id: str,
        access_token: str,
        text: str,
        image_urls: list[str],
    ) -> dict[str, Any]:
        clean_urls = [url.strip() for url in image_urls if url.strip()]
        if len(clean_urls) < 2:
            raise ThreadsApiError("Threads image carousel requires at least two images")
        child_ids = [
            self._create_container(
                threads_user_id=threads_user_id,
                access_token=access_token,
                data={
                    "media_type": "IMAGE",
                    "image_url": image_url,
                    "is_carousel_item": "true",
                },
            )
            for image_url in clean_urls
        ]
        return self._publish_media_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "text": text,
            },
        )

    def wait_for_container(
        self,
        container_id: str,
        access_token: str,
        *,
        timeout: float = 120,
        interval: float = 2,
    ) -> dict[str, Any]:
        clean_container_id = container_id.strip()
        if not clean_container_id:
            raise ThreadsApiError("Threads container id is required")
        deadline = time.monotonic() + max(0.0, float(timeout))
        poll_interval = max(0.0, float(interval))
        params = {
            "fields": "status,error_message",
            "access_token": access_token,
        }
        while True:
            response = self._call_transport(
                "GET",
                f"{self.api_base_url}/{clean_container_id}",
                params=params,
            )
            status = str(response.get("status") or "").strip().upper()
            if status == "FINISHED":
                return response
            if status == "ERROR":
                detail = str(response.get("error_message") or "processing failed")
                safe_detail = _redact_sensitive_detail(detail, params=params)
                raise ThreadsApiError(
                    f"Threads container {clean_container_id} failed: {safe_detail}"
                )
            if time.monotonic() >= deadline:
                raise ThreadsApiError(
                    f"Threads container {clean_container_id} timed out before FINISHED"
                )
            if poll_interval:
                remaining = max(0.0, deadline - time.monotonic())
                time.sleep(min(poll_interval, remaining))

    def publish_creation(
        self,
        threads_user_id: str,
        access_token: str,
        creation_id: str,
    ) -> dict[str, Any]:
        clean_creation_id = creation_id.strip()
        if not clean_creation_id:
            raise ThreadsApiError("Threads creation id is required")
        return self._call_transport(
            "POST",
            f"{self.api_base_url}/{threads_user_id}/threads_publish",
            data={
                "creation_id": clean_creation_id,
                "access_token": access_token,
            },
        )

    def publish_reply(
        self,
        threads_user_id: str,
        access_token: str,
        text: str,
        reply_to_id: str,
    ) -> dict[str, Any]:
        return self._publish_text_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            text=text,
            reply_to_id=reply_to_id,
        )

    def fetch_media_insights(
        self,
        media_id: str,
        access_token: str,
        metrics: tuple[str, ...] = THREADS_INSIGHT_METRICS,
    ) -> dict[str, int]:
        clean_media_id = media_id.strip()
        if not clean_media_id:
            raise ThreadsApiError("Threads media id is required")
        response = self._transport(
            "GET",
            f"{self.api_base_url}/{clean_media_id}/insights",
            params={
                "metric": ",".join(metrics),
                "access_token": access_token,
            },
        )
        return _normalize_insights_response(response, metrics)

    def fetch_media_permalink(self, media_id: str, access_token: str) -> str:
        clean_media_id = media_id.strip()
        if not clean_media_id:
            raise ThreadsApiError("Threads media id is required")
        response = self._transport(
            "GET",
            f"{self.api_base_url}/{clean_media_id}",
            params={
                "fields": "permalink",
                "access_token": access_token,
            },
        )
        return str(response.get("permalink") or "").strip()

    def _publish_text_container(
        self,
        threads_user_id: str,
        access_token: str,
        text: str,
        reply_to_id: str = "",
    ) -> dict[str, Any]:
        data = {
            "media_type": "TEXT",
            "text": text,
        }
        if reply_to_id.strip():
            data["reply_to_id"] = reply_to_id.strip()
        return self._publish_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data=data,
        )

    def _publish_container(
        self,
        threads_user_id: str,
        access_token: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        creation_id = self._create_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data=data,
        )
        return self.publish_creation(threads_user_id, access_token, creation_id)

    def _publish_media_container(
        self,
        threads_user_id: str,
        access_token: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        creation_id = self._create_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data=data,
        )
        self.wait_for_container(creation_id, access_token)
        return self.publish_creation(threads_user_id, access_token, creation_id)

    def _create_container(
        self,
        threads_user_id: str,
        access_token: str,
        data: dict[str, Any],
    ) -> str:
        container_data = dict(data)
        container_data["access_token"] = access_token
        container = self._call_transport(
            "POST",
            f"{self.api_base_url}/{threads_user_id}/threads",
            data=container_data,
        )
        creation_id = str(container.get("id", "")).strip()
        if not creation_id:
            raise ThreadsApiError("Threads container response did not include an id")
        return creation_id

    def _call_transport(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, dict[str, Any]] = {}
        if data is not None:
            kwargs["data"] = data
        if params is not None:
            kwargs["params"] = params
        try:
            return self._transport(method, url, **kwargs)
        except ThreadsApiError as exc:
            safe_detail = _redact_sensitive_detail(str(exc), data=data, params=params)
            raise ThreadsApiError(
                safe_detail,
                outcome_unknown=exc.outcome_unknown,
            ) from None
        except Exception as exc:
            detail = str(exc)
            safe_detail = _redact_sensitive_detail(detail, data=data, params=params)
            if safe_detail != detail:
                raise ThreadsApiError(safe_detail) from None
            raise


def _urlopen_transport(
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    is_publish_commit = method.upper() == "POST" and url.rstrip("/").endswith(
        "/threads_publish"
    )
    clean_url = url
    if params:
        clean_url = f"{clean_url}?{urlencode(params)}"
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        body = urlencode(data).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(clean_url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        safe_detail = _redact_sensitive_detail(detail, data=data, params=params)
        raise ThreadsApiError(
            f"Threads API HTTP {exc.code}: {safe_detail}",
            outcome_unknown=is_publish_commit and int(exc.code) >= 500,
        ) from exc
    except (OSError, URLError) as exc:
        safe_detail = _redact_sensitive_detail(str(exc), data=data, params=params)
        raise ThreadsApiError(
            f"Threads API request failed: {safe_detail}",
            outcome_unknown=is_publish_commit,
        ) from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ThreadsApiError(
            "Threads API returned invalid JSON",
            outcome_unknown=is_publish_commit,
        ) from exc
    if isinstance(parsed, dict) and parsed.get("error"):
        raise ThreadsApiError(_redact_sensitive_detail(str(parsed["error"]), data=data, params=params))
    if not isinstance(parsed, dict):
        raise ThreadsApiError(
            "Threads API returned an unexpected response",
            outcome_unknown=is_publish_commit,
        )
    return parsed


def _normalize_insights_response(response: dict[str, Any], metrics: tuple[str, ...]) -> dict[str, int]:
    normalized = {metric: 0 for metric in metrics}
    items = response.get("data")
    if not isinstance(items, list):
        raise ThreadsApiError("Threads insights response did not include data")
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name not in normalized:
            continue
        values = item.get("values")
        value = 0
        if isinstance(values, list) and values:
            first = values[0]
            if isinstance(first, dict):
                value = _safe_int(first.get("value"))
        else:
            value = _safe_int(item.get("value"))
        normalized[name] = value
    return normalized


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _redact_sensitive_detail(
    detail: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> str:
    redacted = detail
    for source in (data or {}, params or {}):
        for key in ("client_secret", "access_token"):
            value = str(source.get(key, "")).strip()
            if value:
                redacted = redacted.replace(value, "[redacted]")
    redacted = re.sub(
        r"(Invalid client_secret:\s*)[^\"\\n,}]+",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(
        r'("(?:client_secret|access_token)"\s*:\s*")[^"]+(")',
        r"\1[redacted]\2",
        redacted,
    )
    return redacted
