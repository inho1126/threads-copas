from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class SettingsPayload(BaseModel):
    naver_blog_id: str = ""
    coupang_sub_id: str = ""
    coupang_channel_ids: str = ""
    writer_persona: str = ""
    coupang_access_key: str = ""
    coupang_secret_key: str = ""
    codex_model: str = ""
    codex_threads_prompt: str = ""
    threads_app_id: str = ""
    threads_app_secret: str = ""
    threads_redirect_uri: str = ""
    threads_service_url: str = ""
    threads_service_api_key: str = ""


class JobCreatePayload(BaseModel):
    product_url: str = Field(min_length=1)
    product_name: str = ""
    image_url: str = ""
    memo: str = ""


class MediaCandidatePayload(BaseModel):
    source: str = Field(min_length=1)
    source_url: str = ""
    image_url: str = ""
    timestamp_label: str = ""
    title: str = ""
    creator: str = ""
    notes: str = ""
    no_captions: bool = False
    no_tts: bool = False
    product_visible: bool = False
    permission_reviewed: bool = False


class PublishHandoff(BaseModel):
    status: str
    message: str


class ThreadsProfilePayload(BaseModel):
    profile_key: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    notes: str = ""


class ThreadsMediaUploadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_base64: str = Field(min_length=4)


class ThreadsMediaUploadStartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_bytes: int = Field(gt=0, le=100 * 1024 * 1024)


class ThreadsMediaUploadPartPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    content_base64: str = Field(min_length=4)


class ThreadsDraftPayload(BaseModel):
    job_id: str = ""
    product_url: str = Field(min_length=1)
    partner_url: str = ""
    profile_key: str = ""
    coupang_channel_id: str = ""
    product_name: str = ""
    facts: list[str] = Field(default_factory=list)
    codex_threads_prompt: str = ""
    custom_persona: str = Field(default="", max_length=300)
    regenerate_persona_keys: list[str] = Field(default_factory=list, max_length=7)
    memo: str = ""

    @field_validator("custom_persona", mode="before")
    @classmethod
    def normalize_custom_persona(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        clean = "".join(
            character
            for character in value
            if not unicodedata.category(character).startswith("C")
        )
        return clean.strip()[:300]

    @field_validator("regenerate_persona_keys")
    @classmethod
    def normalize_regenerate_persona_keys(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for key in value:
            clean_key = key.strip()
            if clean_key and clean_key not in normalized:
                normalized.append(clean_key)
        return normalized


class RedNoteQueryPayload(BaseModel):
    source_keyword: str = Field(default="", max_length=50)
    product_facts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("source_keyword", mode="before")
    @classmethod
    def normalize_source_keyword(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()[:50]
        return value

    @field_validator("product_facts")
    @classmethod
    def normalize_product_facts(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for fact in value:
            clean_fact = fact.strip()[:200]
            if clean_fact and clean_fact not in normalized:
                normalized.append(clean_fact)
        return normalized


class RedNoteSearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RedNoteDownloadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    result_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    note_id: str = Field(pattern=r"^[0-9a-f]{24}$")


class RedNoteCompletePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["video", "images", "mixed"]
    asset_ids: list[str] = Field(min_length=1, max_length=5)

    @field_validator("asset_ids")
    @classmethod
    def normalize_asset_ids(cls, value: list[str]) -> list[str]:
        normalized = [asset_id.strip() for asset_id in value]
        if any(not asset_id for asset_id in normalized):
            raise ValueError("asset_ids must not contain empty values")
        return normalized


class CopySelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_id: str = Field(min_length=1, max_length=200)


class CopyVariantEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=500)

    @field_validator("body", mode="before")
    @classmethod
    def normalize_body(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ThreadsMediaPublishActionPayload(BaseModel):
    """The local server intentionally ignores all mutable publish fields."""

    pass


class CoupangProductPreviewPayload(BaseModel):
    product_url: str = Field(min_length=1)
    product_name: str = ""
    sub_id: str = ""


class CoupangDeeplinkPayload(BaseModel):
    product_url: str = Field(min_length=1)
    sub_id: str = ""


class CoupangProductSearchPayload(BaseModel):
    keyword: str = Field(min_length=1, max_length=50)
    sub_id: str = ""
    limit: int = Field(default=10, ge=1, le=10)

    @field_validator("keyword", mode="before")
    @classmethod
    def normalize_keyword(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()[:50]
        return value


class ThreadsPublishPayload(BaseModel):
    profile_key: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    comment_text: str = ""


class ThreadsRemotePublishPayload(BaseModel):
    profile_key: str = Field(min_length=1)
    product_url: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    text: str = Field(min_length=1)
    comment_text: str = ""


class ThreadsRemoteMediaPublishPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=16, max_length=128)
    profile_key: str = Field(min_length=1)
    product_url: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=500)
    comment_text: str = Field(min_length=1, max_length=500)
    media_mode: Literal["video", "images", "mixed"]
    media_urls: list[HttpUrl] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_media_cardinality(self) -> "ThreadsRemoteMediaPublishPayload":
        count = len(self.media_urls)
        if self.media_mode == "video" and count != 1:
            raise ValueError("video mode requires exactly one media URL")
        if self.media_mode == "images" and not 2 <= count <= 5:
            raise ValueError("images mode requires 2 to 5 media URLs")
        if self.media_mode == "mixed" and count != 2:
            raise ValueError("mixed mode requires one video and one image URL")
        return self
