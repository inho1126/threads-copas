from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


Transport = Callable[
    [str, str],
    dict[str, Any],
]


class ThreadsApiError(RuntimeError):
    pass


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
                "scope": "threads_basic,threads_content_publish,threads_manage_replies",
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
        return self._publish_container(
            threads_user_id=threads_user_id,
            access_token=access_token,
            data={
                "media_type": "IMAGE",
                "image_url": image_url.strip(),
                "text": text,
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
        container_data = dict(data)
        container_data["access_token"] = access_token
        container = self._transport(
            "POST",
            f"{self.api_base_url}/{threads_user_id}/threads",
            data=container_data,
        )
        creation_id = str(container.get("id", "")).strip()
        if not creation_id:
            raise ThreadsApiError("Threads container response did not include an id")
        return self._transport(
            "POST",
            f"{self.api_base_url}/{threads_user_id}/threads_publish",
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
        )


def _urlopen_transport(
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
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
        raise ThreadsApiError(f"Threads API HTTP {exc.code}: {safe_detail}") from exc
    except (OSError, URLError) as exc:
        raise ThreadsApiError(f"Threads API request failed: {exc}") from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ThreadsApiError("Threads API returned invalid JSON") from exc
    if isinstance(parsed, dict) and parsed.get("error"):
        raise ThreadsApiError(_redact_sensitive_detail(str(parsed["error"]), data=data, params=params))
    if not isinstance(parsed, dict):
        raise ThreadsApiError("Threads API returned an unexpected response")
    return parsed


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
            if len(value) >= 4:
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
