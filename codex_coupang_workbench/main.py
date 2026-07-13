from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
from secrets import compare_digest
import stat
from threading import Lock
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .coupang_partners import (
    CoupangPartnerProduct,
    CoupangPartnersClient,
    CoupangPartnersError,
    extract_coupang_ids,
    fetch_partner_product_context,
    normalize_search_product,
    resolve_coupang_redirect,
)
from .codex_threads import (
    PERSONAS,
    CodexThreadsError,
    generate_codex_threads_post,
)
from .codex_rednote import generate_rednote_query
from .local_chrome import LocalChromeError, fetch_chrome_product_context
from .naver import publish_handoff_message
from .product_research import fetch_best_product_context
from .rednote_sidecar import RedNoteSidecarClient, RedNoteSidecarError
from .schemas import (
    CoupangDeeplinkPayload,
    CoupangProductPreviewPayload,
    CoupangProductSearchPayload,
    CopyVariantEditPayload,
    CopySelectionPayload,
    JobCreatePayload,
    MediaCandidatePayload,
    MediaSelectionPayload,
    PublishHandoff,
    RedNoteCompletePayload,
    RedNoteDownloadPayload,
    RedNoteQueryPayload,
    RedNoteSearchPayload,
    SettingsPayload,
    ThreadsDraftPayload,
    ThreadsMediaPublishActionPayload,
    ThreadsProfilePayload,
    ThreadsPublishPayload,
    ThreadsRemotePublishPayload,
)
from .storage import PublishPayloadLockedError, WorkbenchStore, utc_now
from .threads import ThreadsApiClient, ThreadsApiError
from .threads_bridge import ThreadsBridgeClient, ThreadsBridgeError
from .writer import generate_campaign, generate_draft, generate_threads_comment, generate_threads_post

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PACKAGE_DIR.parent / "workbench_data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "workbench.sqlite3"
STATIC_DIR = PACKAGE_DIR / "static"
ICON_PATH = PACKAGE_DIR.parent / "assets" / "appicon.ico"
SECRET_SETTING_KEYS = {
    "coupang_access_key",
    "coupang_secret_key",
    "threads_app_secret",
    "threads_service_api_key",
}
REMOVED_SETTING_KEYS = {"coupang_proxy_url"}
SECRET_MASK = "********"
THREADS_BRIDGE_API_KEY_ENV = "THREADS_BRIDGE_API_KEY"
FAST_CODEX_MODEL = "gpt-5.3-codex-spark"
THREADS_COPY_CODEX_MODEL = "gpt-5.6-terra"
THREAD_DRAFT_VARIANTS = PERSONAS
REDNOTE_SIDECAR_URL_ENV = "REDNOTE_SIDECAR_URL"
REDNOTE_SIDECAR_KEY_ENV = "REDNOTE_SIDECAR_KEY"
REDNOTE_OUTPUT_ROOT_ENV = "REDNOTE_OUTPUT_ROOT"
_REDNOTE_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_REDNOTE_NOTE_ID = re.compile(r"^[0-9a-f]{24}$")
_SAFE_SIDECAR_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_REDNOTE_ACTIONABLE_ERRORS = {
    "CHROME_LOGIN_REQUIRED": (
        401,
        "Google Chrome에서 RedNote 로그인이 필요합니다.",
    ),
    "CHROME_PERMISSION_REQUIRED": (
        403,
        "Google Chrome 자동화를 위한 Apple Events 권한이 필요합니다.",
    ),
    "CHROME_SEARCH_EMPTY": (
        404,
        "RedNote 영상 검색 결과를 찾지 못했습니다.",
    ),
    "CHROME_SEARCH_TIMEOUT": (
        504,
        "Google Chrome의 RedNote 검색 시간이 초과되었습니다.",
    ),
}
_PRIVATE_PUBLISH_JOB_FIELDS = frozenset(
    {
        "publish_idempotency_key",
        "publish_locked_profile_key",
        "publish_locked_threads_user_id",
        "publish_locked_body",
        "publish_locked_comment",
        "publish_locked_media_mode",
        "publish_locked_asset_ids",
        "publish_media_ids",
        "publish_media_urls",
        "publish_child_container_ids",
        "publish_container_id",
        "publish_resume_stage",
    }
)


def fallback_style_for_persona(persona_key: str, custom_instruction: str = "") -> str:
    if persona_key != "custom":
        return persona_key
    instruction = custom_instruction.lower()
    if any(term in instruction for term in ("스토리", "대화", "사연")):
        return "story"
    if any(term in instruction for term in ("문제", "해결", "충격")):
        return "problem_solution"
    if any(term in instruction for term in ("솔직", "발견", "후기")):
        return "honest_discovery"
    if any(term in instruction for term in ("구매", "전환", "확인")):
        return "conversion"
    if any(term in instruction for term in ("공감", "현실", "친근")):
        return "relatable"
    return "curiosity"


def public_settings(settings: dict[str, str]) -> dict[str, str]:
    visible = {key: value for key, value in settings.items() if key not in REMOVED_SETTING_KEYS}
    for key in SECRET_SETTING_KEYS:
        if key in visible and visible[key]:
            visible[key] = SECRET_MASK
    return visible


def settings_to_store(payload: SettingsPayload, current_settings: dict[str, str]) -> dict[str, str]:
    settings = payload.model_dump()
    for key in settings:
        if key not in payload.model_fields_set and current_settings.get(key):
            settings[key] = current_settings[key]
    for key in SECRET_SETTING_KEYS:
        if settings.get(key) == SECRET_MASK and current_settings.get(key):
            settings[key] = current_settings[key]
        if not settings.get(key) and current_settings.get(key):
            settings[key] = current_settings[key]
    return settings


def threads_service_url(settings: dict[str, str]) -> str:
    return settings.get("threads_service_url", "").strip().rstrip("/")


def uses_remote_threads_service(settings: dict[str, str]) -> bool:
    return bool(threads_service_url(settings))


def fetch_coupang_partner_product(
    product_url: str,
    settings: dict[str, str],
    product_keyword: str = "",
    sub_id: str = "",
) -> tuple[CoupangPartnerProduct, str]:
    access_key = settings.get("coupang_access_key", "").strip()
    secret_key = settings.get("coupang_secret_key", "").strip()
    if not access_key or not secret_key:
        raise CoupangPartnersError("쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요.")
    selected_sub_id = sub_id.strip() or settings.get("coupang_sub_id", "")
    partner_product, resolved_url = fetch_partner_product_context(
        product_url,
        access_key=access_key,
        secret_key=secret_key,
        sub_id=selected_sub_id,
        product_keyword=product_keyword,
    )
    return partner_product, resolved_url


def create_coupang_deeplink(product_url: str, settings: dict[str, str], sub_id: str = "") -> dict[str, str]:
    access_key = settings.get("coupang_access_key", "").strip()
    secret_key = settings.get("coupang_secret_key", "").strip()
    if not access_key or not secret_key:
        raise CoupangPartnersError("쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요.")
    clean_url = product_url.strip()
    if not clean_url:
        raise CoupangPartnersError("쿠팡 URL을 입력해 주세요.")
    selected_sub_id = sub_id.strip() or settings.get("coupang_sub_id", "")
    client = CoupangPartnersClient(
        access_key,
        secret_key,
        sub_id=selected_sub_id,
    )
    resolved_url = resolve_coupang_redirect(clean_url) or clean_url
    partner_url = client.create_deeplink(resolved_url)
    if not partner_url and resolved_url != clean_url:
        partner_url = client.create_deeplink(clean_url)
    if not partner_url:
        raise CoupangPartnersError("쿠팡 파트너스 API에서 딥링크를 만들지 못했습니다.")
    return {
        "partner_url": partner_url,
        "product_url": resolved_url,
        "resolved_url": resolved_url,
        "original_url": clean_url,
        "sub_id": selected_sub_id.strip(),
    }


def resolve_coupang_partner_product(
    product_url: str,
    settings: dict[str, str],
) -> tuple[CoupangPartnerProduct, str]:
    partner_product, resolved_url = fetch_coupang_partner_product(product_url, settings)
    if not partner_product.product_name:
        raise CoupangPartnersError("쿠팡 파트너스 API에서 상품 정보를 찾지 못했습니다.")
    return partner_product, resolved_url


def product_preview_response(
    product: CoupangPartnerProduct,
    *,
    original_url: str,
    resolved_url: str,
    fallback_product_name: str = "",
) -> dict[str, Any]:
    product_ids = extract_coupang_ids(resolved_url) + extract_coupang_ids(original_url)
    product_id = product.product_id or (product_ids[0] if product_ids else "")
    product_name = product.product_name or fallback_product_name.strip()
    return {
        "product_name": product_name,
        "product_id": product_id,
        "item_id": product_ids[1] if len(product_ids) > 1 else "",
        "image_url": product.image_url,
        "partner_url": product.partner_url,
        "product_url": product.product_url,
        "resolved_url": resolved_url,
        "original_url": original_url,
        "facts": list(product.facts),
        "needs_product_name": not bool(product_name),
    }


def enrich_partner_product_with_local_context(
    product: CoupangPartnerProduct,
    *,
    original_url: str,
    resolved_url: str,
    product_name: str = "",
) -> CoupangPartnerProduct:
    if product.product_name and product.image_url and product.facts:
        return product
    context = fetch_best_product_context(resolved_url or original_url, product_name or product.product_name)
    return CoupangPartnerProduct(
        product_name=product.product_name or product_name.strip() or context.page_title,
        product_url=product.product_url or resolved_url or context.resolved_url,
        partner_url=product.partner_url,
        image_url=product.image_url or context.image_url,
        facts=product.facts or tuple(context.facts or ()),
        product_id=product.product_id,
    )


def merge_product_facts(*fact_groups: list[str] | tuple[str, ...] | None) -> list[str]:
    merged: list[str] = []
    for facts in fact_groups:
        for fact in facts or []:
            clean_fact = str(fact or "").strip()
            if clean_fact and clean_fact not in merged:
                merged.append(clean_fact)
    return merged


def normalize_rednote_search_response(response: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(response, dict):
        raise ValueError("RedNote search response is invalid")
    search_id = str(response.get("searchId") or "").strip()
    if not _REDNOTE_OPAQUE_ID.fullmatch(search_id):
        raise ValueError("RedNote search response is invalid")
    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("RedNote search response is invalid")

    results: list[dict[str, Any]] = []
    seen_note_ids: set[str] = set()
    seen_result_ids: set[str] = set()
    for raw_result in raw_results[:50]:
        if not isinstance(raw_result, dict) or raw_result.get("isVideo") is not True:
            continue
        result_id = str(raw_result.get("resultId") or "").strip()
        note_id = str(raw_result.get("noteId") or "").strip()
        canonical_url = str(raw_result.get("canonicalUrl") or "").strip()
        if (
            not _REDNOTE_OPAQUE_ID.fullmatch(result_id)
            or not _REDNOTE_NOTE_ID.fullmatch(note_id)
            or canonical_url != f"https://www.rednote.com/explore/{note_id}"
            or result_id in seen_result_ids
            or note_id in seen_note_ids
        ):
            continue
        thumbnail_url = _safe_public_rednote_image_url(raw_result.get("thumbnailUrl"))
        seen_result_ids.add(result_id)
        seen_note_ids.add(note_id)
        results.append(
            {
                "result_id": result_id,
                "note_id": note_id,
                "canonical_url": canonical_url,
                "title": _safe_public_text(raw_result.get("title"), 160),
                "description": _safe_public_text(raw_result.get("description"), 500),
                "creator": _safe_public_text(raw_result.get("creator"), 120),
                "thumbnail_url": thumbnail_url,
                "is_video": True,
            }
        )
    return search_id, results


def _safe_public_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return " ".join(text.replace("\x00", " ").split())[:limit]


def _safe_public_rednote_image_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return raw


def _safe_sidecar_filename(value: Any) -> str:
    name = str(value or "").strip()
    return name if _SAFE_SIDECAR_FILE.fullmatch(name) else ""


def build_rednote_assets_from_completion(
    job: dict[str, Any],
    completed: dict[str, Any],
    *,
    output_root: str | Path,
) -> list[dict[str, Any]]:
    note_id = str(job.get("rednote_note_id") or "").strip()
    canonical_url = str(job.get("rednote_canonical_url") or "").strip()
    if (
        not _REDNOTE_NOTE_ID.fullmatch(note_id)
        or canonical_url != f"https://www.rednote.com/explore/{note_id}"
        or not isinstance(completed, dict)
    ):
        raise ValueError("RedNote completion job is invalid")

    root = _strict_directory(output_root, "RedNote output root")
    output_dir = _strict_directory(completed.get("outputDir"), "RedNote output directory")
    if output_dir.parent == output_dir:
        raise ValueError("RedNote output directory is invalid")
    try:
        relative_output = output_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("RedNote output directory is outside the configured root") from exc
    if (
        len(relative_output.parts) != 1
        or not re.fullmatch(rf"{re.escape(note_id)}(?:-[1-9]\d*)?", relative_output.name)
    ):
        raise ValueError("RedNote output directory is invalid")

    files = completed.get("files")
    note = completed.get("note")
    frame_metadata = completed.get("frameMetadata")
    if not isinstance(files, dict) or not isinstance(note, dict) or not isinstance(frame_metadata, list):
        raise ValueError("RedNote completion response is invalid")
    if str(note.get("noteId") or "") != note_id:
        raise ValueError("RedNote completion note does not match the selected post")
    duration_ms = _positive_media_metric(note.get("durationMs"), "duration")
    width = _positive_media_metric(note.get("width"), "width")
    height = _positive_media_metric(note.get("height"), "height")

    video_name = _safe_sidecar_filename(files.get("video"))
    raw_frame_names = files.get("frames")
    if not video_name or not video_name.lower().endswith(".mp4") or not isinstance(raw_frame_names, list):
        raise ValueError("RedNote completion files are invalid")
    frame_names = [_safe_sidecar_filename(value) for value in raw_frame_names]
    if not 3 <= len(frame_names) <= 5:
        raise ValueError("RedNote completion requires 3 to 5 JPG frames")
    if (
        any(not name or not name.lower().endswith((".jpg", ".jpeg")) for name in frame_names)
        or len(set(frame_names)) != len(frame_names)
    ):
        raise ValueError("RedNote frame files are invalid")

    video_path = _strict_regular_file(output_dir, video_name)
    if not _looks_like_mp4(video_path):
        raise ValueError("RedNote video file is invalid")
    frame_paths = {name: _strict_regular_file(output_dir, name) for name in frame_names}
    if any(not _looks_like_jpeg(path) for path in frame_paths.values()):
        raise ValueError("RedNote frame file is invalid")

    normalized_frames: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    seen_metadata_names: set[str] = set()
    for raw_frame in frame_metadata:
        if not isinstance(raw_frame, dict):
            raise ValueError("RedNote frame metadata is invalid")
        try:
            index = int(raw_frame.get("index"))
            timestamp_ms = int(raw_frame.get("timeMs"))
        except (TypeError, ValueError) as exc:
            raise ValueError("RedNote frame metadata is invalid") from exc
        file_name = _safe_sidecar_filename(raw_frame.get("fileName"))
        if (
            index not in range(1, 6)
            or index in seen_indices
            or timestamp_ms < 0
            or timestamp_ms > duration_ms
            or file_name not in frame_paths
            or file_name in seen_metadata_names
        ):
            raise ValueError("RedNote frame metadata is invalid")
        seen_indices.add(index)
        seen_metadata_names.add(file_name)
        normalized_frames.append(
            {
                "file_name": file_name,
                "timestamp_ms": timestamp_ms,
                "index": index,
            }
        )
    if seen_metadata_names != set(frame_names):
        raise ValueError("RedNote frame metadata does not match saved files")
    normalized_frames.sort(key=lambda item: (item["timestamp_ms"], item["index"]))

    common = {
        "note_id": note_id,
        "canonical_url": canonical_url,
        "width": width,
        "height": height,
    }
    assets = [
        {
            "id": str(uuid4()),
            **common,
            "asset_type": "video",
            "local_path": str(video_path),
            "mime_type": "video/mp4",
            "duration_ms": duration_ms,
            "timestamp_ms": 0,
        }
    ]
    assets.extend(
        {
            "id": str(uuid4()),
            **common,
            "asset_type": "frame",
            "local_path": str(frame_paths[frame["file_name"]]),
            "mime_type": "image/jpeg",
            "duration_ms": 0,
            "timestamp_ms": frame["timestamp_ms"],
        }
        for frame in normalized_frames
    )
    return assets


def _strict_directory(value: Any, label: str) -> Path:
    raw = Path(str(value or "")).expanduser()
    if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink():
        raise ValueError(f"{label} is invalid")
    try:
        resolved = raw.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} does not exist") from exc
    if resolved != raw or not stat.S_ISDIR(mode):
        raise ValueError(f"{label} is invalid")
    return resolved


def _strict_regular_file(directory: Path, file_name: str) -> Path:
    candidate = directory / file_name
    if candidate.parent != directory or candidate.is_symlink():
        raise ValueError("RedNote media must be a regular file")
    try:
        resolved = candidate.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ValueError("RedNote media file does not exist") from exc
    if resolved != candidate or not stat.S_ISREG(mode):
        raise ValueError("RedNote media must be a regular file")
    return resolved


def _positive_media_metric(value: Any, label: str) -> int:
    try:
        metric = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"RedNote {label} metadata is invalid") from exc
    if metric <= 0:
        raise ValueError(f"RedNote {label} metadata is invalid")
    return metric


def _looks_like_mp4(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _looks_like_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(3)
            handle.seek(-2, 2)
            tail = handle.read(2)
    except (OSError, ValueError):
        return False
    return head.startswith(b"\xff\xd8") and tail == b"\xff\xd9"


def public_rednote_asset(job_id: str, asset: dict[str, Any]) -> dict[str, Any]:
    asset_id = str(asset.get("id") or "")
    asset_type = str(asset.get("asset_type") or "")
    media_url = (
        f"/api/jobs/{job_id}/rednote-video"
        if asset_type == "video"
        else f"/api/jobs/{job_id}/rednote-assets/{asset_id}"
    )
    return {
        "id": asset_id,
        "note_id": str(asset.get("note_id") or ""),
        "canonical_url": str(asset.get("canonical_url") or ""),
        "asset_type": asset_type,
        "mime_type": str(asset.get("mime_type") or ""),
        "width": int(asset.get("width") or 0),
        "height": int(asset.get("height") or 0),
        "duration_ms": int(asset.get("duration_ms") or 0),
        "timestamp_ms": int(asset.get("timestamp_ms") or 0),
        "sort_order": int(asset.get("sort_order") or 0),
        "selected": bool(asset.get("selected")),
        "url": media_url,
    }


def public_copy_variant(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(variant.get("id") or ""),
        "persona_key": str(variant.get("persona_key") or ""),
        "persona_label": str(variant.get("persona_label") or ""),
        "label": str(variant.get("persona_label") or ""),
        "body": str(variant.get("body") or ""),
        "text": str(variant.get("body") or ""),
        "generation": int(variant.get("generation") or 1),
        "selected": bool(variant.get("selected")),
        "custom_instruction": str(variant.get("custom_instruction") or ""),
    }


def public_publish_job(job: dict[str, Any]) -> dict[str, Any]:
    public_job = {
        key: value
        for key, value in job.items()
        if key not in _PRIVATE_PUBLISH_JOB_FIELDS
    }
    public_job["publish_locked"] = bool(
        str(job.get("publish_idempotency_key") or "").strip()
    )
    public_job["retryable"] = str(job.get("publish_stage") or "") != "outcome_unknown"
    return public_job


def create_app(db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="Codex Coupang Workbench")
    store = WorkbenchStore(db_path)
    app.state.rednote_searches = {}
    app.state.active_media_publish_keys = set()
    app.state.active_media_publish_lock = Lock()

    def get_store() -> WorkbenchStore:
        return store

    def reject_locked_publish_mutation(job: dict[str, Any]) -> None:
        if str(job.get("publish_idempotency_key") or "").strip():
            raise HTTPException(
                status_code=409,
                detail="게시가 시작된 작업의 본문과 미디어는 변경할 수 없습니다.",
            )

    def get_rednote_sidecar_client() -> RedNoteSidecarClient:
        try:
            return RedNoteSidecarClient(
                os.environ.get(REDNOTE_SIDECAR_URL_ENV, "http://127.0.0.1:4310"),
                api_key=os.environ.get(REDNOTE_SIDECAR_KEY_ENV, ""),
            )
        except RedNoteSidecarError:
            raise HTTPException(
                status_code=503,
                detail="RedNote 로컬 서비스 설정이 올바르지 않습니다.",
            ) from None

    def raise_rednote_sidecar_http_error(error: RedNoteSidecarError) -> None:
        status_code, detail = _REDNOTE_ACTIONABLE_ERRORS.get(
            str(error.code or ""),
            (502, "RedNote 로컬 서비스 요청을 처리하지 못했습니다."),
        )
        raise HTTPException(
            status_code=status_code,
            detail=detail,
        ) from None

    def get_threads_client(settings: dict[str, str]) -> ThreadsApiClient:
        app_id = settings.get("threads_app_id", "").strip()
        app_secret = settings.get("threads_app_secret", "").strip()
        redirect_uri = settings.get("threads_redirect_uri", "").strip()
        if not app_id or not app_secret or not redirect_uri:
            raise HTTPException(status_code=400, detail="Threads app settings are required")
        return ThreadsApiClient(
            app_id=app_id,
            app_secret=app_secret,
            redirect_uri=redirect_uri,
        )

    def get_threads_bridge_client(settings: dict[str, str]) -> ThreadsBridgeClient:
        try:
            return ThreadsBridgeClient(
                threads_service_url(settings),
                api_key=settings.get("threads_service_api_key", ""),
            )
        except ThreadsBridgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    def studio_preview_response(job_id: str, store: WorkbenchStore) -> dict[str, Any]:
        try:
            preview = store.get_studio_preview(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        job = preview["job"]
        variants = [public_copy_variant(variant) for variant in preview["copy_variants"]]
        selected_variant = preview["selected_copy_variant"]
        assets = [
            public_rednote_asset(job_id, asset)
            for asset in preview["rednote_assets"]
        ]
        selected_asset_ids = [
            asset["id"] for asset in preview["selected_rednote_assets"]
        ]
        selected_asset_id_set = set(selected_asset_ids)
        public_assets = [
            {**asset, "selected": asset["id"] in selected_asset_id_set}
            for asset in assets
        ]
        public_assets_by_id = {asset["id"]: asset for asset in public_assets}
        selected_public_assets = [
            public_assets_by_id[asset_id]
            for asset_id in selected_asset_ids
            if asset_id in public_assets_by_id
        ]
        locked_body = str(job.get("publish_locked_body") or "").strip()
        public_selected_variant = (
            public_copy_variant(selected_variant) if selected_variant else None
        )
        if locked_body and public_selected_variant is not None:
            public_selected_variant = {
                **public_selected_variant,
                "body": locked_body,
                "text": locked_body,
                "selected": True,
            }
        locked_comment = str(job.get("publish_locked_comment") or "").strip()
        comment_text = locked_comment or generate_threads_comment(
            str(job.get("product_url") or ""),
            str(job.get("product_name") or ""),
        )
        profile_key = str(
            job.get("publish_locked_profile_key")
            or job.get("selected_profile_key")
            or ""
        )
        return {
            "job": public_publish_job(job),
            "copy_variants": variants,
            "selected_copy_variant": public_selected_variant,
            "selected_variant_id": str(
                (selected_variant or {}).get("id") or ""
            ),
            "rednote_assets": public_assets,
            "selected_rednote_assets": selected_public_assets,
            "media_mode": str(
                job.get("publish_locked_media_mode")
                or job.get("media_mode")
                or ""
            ),
            "profile": {"profile_key": profile_key},
            "text": str(
                locked_body
                or (selected_variant or {}).get("body")
                or ""
            ),
            "comment_text": comment_text,
        }

    def media_publish_result(job: dict[str, Any]) -> dict[str, Any]:
        post_id = str(job.get("threads_post_id") or "").strip()
        reply_id = str(job.get("threads_reply_id") or "").strip()
        return {
            "job": public_publish_job(job),
            "publish_stage": str(job.get("publish_stage") or "draft"),
            "threads_post_id": post_id,
            "threads_reply_id": reply_id,
            "threads_permalink": str(job.get("threads_permalink") or "").strip(),
            "partial": bool(post_id and not reply_id),
        }

    def local_media_outcome_unknown(job: dict[str, Any]) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "code": "PUBLISH_OUTCOME_UNKNOWN",
                "message": (
                    "Threads 응답을 확인하지 못해 자동 재시도를 중단했습니다. "
                    "Threads에서 게시 결과를 직접 확인해 주세요."
                ),
                "retryable": False,
                **media_publish_result(job),
            },
        )

    def read_locked_rednote_asset(
        job_id: str,
        asset_id: str,
        expected_mode: str,
        store: WorkbenchStore,
    ) -> bytes:
        asset = next(
            (
                candidate
                for candidate in store.list_rednote_assets(job_id)
                if candidate["id"] == asset_id
            ),
            None,
        )
        asset_type = str((asset or {}).get("asset_type") or "")
        valid_type = (
            asset_type == "video" if expected_mode == "video"
            else asset_type == "frame" if expected_mode == "images"
            else asset_type in {"video", "frame"} if expected_mode == "mixed"
            else False
        )
        if asset is None or not valid_type:
            raise ValueError("Locked RedNote media selection is no longer available")
        output_root = os.environ.get(
            REDNOTE_OUTPUT_ROOT_ENV,
            str(Path.home() / "Downloads" / "rednote"),
        )
        root = _strict_directory(output_root, "RedNote output root")
        local_path = Path(str(asset.get("local_path") or ""))
        try:
            local_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Locked RedNote media path is invalid") from exc
        media_file = _strict_regular_file(local_path.parent, local_path.name)
        if asset_type == "video":
            if not _looks_like_mp4(media_file):
                raise ValueError("Locked RedNote video is invalid")
        elif not _looks_like_jpeg(media_file):
            raise ValueError("Locked RedNote image is invalid")
        return media_file.read_bytes()

    def acquire_media_publish(idempotency_key: str) -> bool:
        with app.state.active_media_publish_lock:
            if idempotency_key in app.state.active_media_publish_keys:
                return False
            app.state.active_media_publish_keys.add(idempotency_key)
            return True

    def release_media_publish(idempotency_key: str) -> None:
        with app.state.active_media_publish_lock:
            app.state.active_media_publish_keys.discard(idempotency_key)

    def run_local_media_publish(
        job_id: str,
        settings: dict[str, str],
        store: WorkbenchStore,
    ) -> dict[str, Any]:
        bridge = get_threads_bridge_client(settings)
        while True:
            job = store.get_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Job not found")
            stage = str(job.get("publish_stage") or "draft")
            if stage == "published":
                return media_publish_result(job)
            if stage == "outcome_unknown":
                raise local_media_outcome_unknown(job)
            if stage == "draft":
                if store.checkpoint_publish(job_id, "draft", "uploading_media"):
                    continue
                continue
            if stage == "uploading_media":
                asset_ids = list(job.get("publish_locked_asset_ids") or [])
                media_ids = list(job.get("publish_media_ids") or [])
                media_urls = list(job.get("publish_media_urls") or [])
                if len(media_ids) != len(media_urls) or len(media_urls) > len(asset_ids):
                    store.fail_publish(
                        job_id,
                        "uploading_media",
                        resume_stage="uploading_media",
                        error="Temporary media checkpoint is invalid",
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="Temporary media checkpoint is invalid",
                    )
                try:
                    for asset_id in asset_ids[len(media_urls) :]:
                        media_bytes = read_locked_rednote_asset(
                            job_id,
                            asset_id,
                            str(job.get("publish_locked_media_mode") or ""),
                            store,
                        )
                        uploaded = bridge.upload_media(media_bytes)
                        media_id = str(uploaded.get("media_id") or "").strip()
                        public_url = str(uploaded.get("public_url") or "").strip()
                        if not media_id or not public_url:
                            raise ThreadsBridgeError(
                                "Threads service did not return temporary media"
                            )
                        media_ids.append(media_id)
                        media_urls.append(public_url)
                        if not store.checkpoint_publish(
                            job_id,
                            "uploading_media",
                            "uploading_media",
                            media_ids=media_ids,
                            media_urls=media_urls,
                        ):
                            raise ThreadsBridgeError(
                                "Temporary media checkpoint changed concurrently"
                            )
                except (OSError, ValueError, ThreadsBridgeError) as exc:
                    store.fail_publish(
                        job_id,
                        "uploading_media",
                        resume_stage="uploading_media",
                        error=str(exc),
                    )
                    raise HTTPException(status_code=502, detail=str(exc)) from None
                if store.checkpoint_publish(
                    job_id,
                    "uploading_media",
                    "media_uploaded",
                ):
                    continue
                continue
            if stage == "media_uploaded":
                if store.checkpoint_publish(
                    job_id,
                    "media_uploaded",
                    "publishing_main",
                ):
                    continue
                continue
            if stage in {"main_published", "publishing_reply"}:
                if stage == "main_published":
                    store.checkpoint_publish(
                        job_id,
                        "main_published",
                        "publishing_reply",
                    )
                job = store.get_job(job_id) or job
                stage = str(job.get("publish_stage") or stage)
            if stage == "publishing_main" or stage == "publishing_reply":
                try:
                    remote = bridge.publish_media(
                        idempotency_key=str(job["publish_idempotency_key"]),
                        profile_key=str(job["publish_locked_profile_key"]),
                        product_url=str(job["product_url"]),
                        product_name=str(job["product_name"]),
                        text=str(job["publish_locked_body"]),
                        comment_text=str(job["publish_locked_comment"]),
                        media_mode=str(job["publish_locked_media_mode"]),
                        media_urls=list(job.get("publish_media_urls") or []),
                    )
                except ThreadsBridgeError as exc:
                    detail = exc.detail if isinstance(exc.detail, dict) else {}
                    post_id = str(detail.get("threads_post_id") or "").strip()
                    if detail.get("code") == "PUBLISH_OUTCOME_UNKNOWN":
                        checkpoint_fields: dict[str, Any] = {}
                        if post_id:
                            checkpoint_fields.update(
                                {
                                    "threads_post_id": post_id,
                                    "threads_reply_id": str(
                                        detail.get("threads_reply_id") or ""
                                    ),
                                    "threads_permalink": str(
                                        detail.get("threads_permalink") or ""
                                    ),
                                    "threads_profile_key": str(
                                        job["publish_locked_profile_key"]
                                    ),
                                    "threads_published_at": str(
                                        job.get("threads_published_at") or utc_now()
                                    ),
                                    "status": "THREADS_PUBLISHED",
                                }
                            )
                        if not store.mark_publish_outcome_unknown(
                            job_id,
                            stage,
                            error=str(detail.get("message") or exc),
                            **checkpoint_fields,
                        ):
                            current = store.get_job(job_id)
                            if current is None:
                                raise HTTPException(
                                    status_code=404, detail="Job not found"
                                ) from None
                            if current.get("publish_stage") != "outcome_unknown":
                                raise HTTPException(
                                    status_code=409,
                                    detail="게시 상태가 동시에 변경되었습니다.",
                                ) from None
                        sealed = store.get_job(job_id)
                        if sealed is None:
                            raise HTTPException(
                                status_code=404, detail="Job not found"
                            ) from None
                        raise local_media_outcome_unknown(sealed) from None
                    if post_id:
                        store.checkpoint_publish(
                            job_id,
                            stage,
                            "publishing_reply",
                            threads_post_id=post_id,
                            threads_reply_id=str(
                                detail.get("threads_reply_id") or ""
                            ),
                            threads_permalink=str(
                                detail.get("threads_permalink") or ""
                            ),
                            threads_profile_key=str(
                                job["publish_locked_profile_key"]
                            ),
                            threads_published_at=str(
                                job.get("threads_published_at") or utc_now()
                            ),
                            status="THREADS_PUBLISHED",
                        )
                        store.fail_publish(
                            job_id,
                            "publishing_reply",
                            resume_stage="publishing_reply",
                            error=str(exc),
                        )
                    else:
                        store.fail_publish(
                            job_id,
                            stage,
                            resume_stage=stage,
                            error=str(exc),
                        )
                    raise HTTPException(status_code=502, detail=str(exc)) from None
                post_id = str(remote.get("threads_post_id") or "").strip()
                reply_id = str(remote.get("threads_reply_id") or "").strip()
                permalink = str(remote.get("threads_permalink") or "").strip()
                if not post_id:
                    store.fail_publish(
                        job_id,
                        stage,
                        resume_stage=stage,
                        error="Threads service did not return a post id",
                    )
                    raise HTTPException(
                        status_code=502,
                        detail="Threads service did not return a post id",
                    )
                checkpoint_fields = {
                    "threads_post_id": post_id,
                    "threads_reply_id": reply_id,
                    "threads_permalink": permalink,
                    "threads_profile_key": str(job["publish_locked_profile_key"]),
                    "threads_published_at": str(
                        job.get("threads_published_at") or utc_now()
                    ),
                    "status": "THREADS_PUBLISHED",
                }
                if bool(remote.get("partial")) or not reply_id:
                    if store.checkpoint_publish(
                        job_id,
                        stage,
                        "publishing_reply",
                        **checkpoint_fields,
                    ):
                        store.fail_publish(
                            job_id,
                            "publishing_reply",
                            resume_stage="publishing_reply",
                            error="Threads reply publishing is incomplete",
                        )
                    partial_job = store.get_job(job_id)
                    if partial_job is None:
                        raise HTTPException(status_code=404, detail="Job not found")
                    return media_publish_result(partial_job)
                store.checkpoint_publish(
                    job_id,
                    stage,
                    "published",
                    **checkpoint_fields,
                )
                published_job = store.get_job(job_id)
                if published_job is None:
                    raise HTTPException(status_code=404, detail="Job not found")
                return media_publish_result(published_job)
            if stage == "failed":
                raise HTTPException(
                    status_code=409,
                    detail="게시 재시도 버튼을 사용해 주세요.",
                )
            raise HTTPException(status_code=409, detail=f"Unsupported publish stage: {stage}")

    def publish_studio_media(
        job_id: str,
        *,
        retry: bool,
        store: WorkbenchStore,
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        if not uses_remote_threads_service(settings):
            raise HTTPException(
                status_code=400,
                detail="Threads service URL is required for media publishing",
            )
        idempotency_key = str(job.get("publish_idempotency_key") or "").strip()
        if not idempotency_key:
            if retry:
                raise HTTPException(status_code=409, detail="게시를 먼저 시작해 주세요.")
            preview = store.get_studio_preview(job_id)
            selected_variant = preview.get("selected_copy_variant")
            selected_assets = preview.get("selected_rednote_assets") or []
            profile_key = str(job.get("selected_profile_key") or "").strip()
            media_mode = str(job.get("media_mode") or "").strip()
            if selected_variant is None:
                raise HTTPException(status_code=409, detail="게시 문구를 선택해 주세요.")
            if not profile_key:
                raise HTTPException(status_code=409, detail="Threads 계정을 선택해 주세요.")
            if not selected_assets or media_mode not in {"video", "images", "mixed"}:
                raise HTTPException(status_code=409, detail="게시 미디어를 선택해 주세요.")
            remote_profile = next(
                (
                    profile
                    for profile in get_threads_bridge_client(settings).list_profiles()
                    if str(profile.get("profile_key") or "") == profile_key
                ),
                None,
            )
            threads_user_id = str((remote_profile or {}).get("threads_user_id") or "").strip()
            if not remote_profile or not remote_profile.get("is_connected") or not threads_user_id:
                raise HTTPException(
                    status_code=409,
                    detail="선택한 Threads 계정 연결을 다시 확인해 주세요.",
                )
            idempotency_key = uuid4().hex
            locked = store.lock_local_media_publish(
                job_id,
                idempotency_key=idempotency_key,
                profile_key=profile_key,
                threads_user_id=threads_user_id,
                body=str(selected_variant["body"]),
                comment_text=generate_threads_comment(
                    str(job.get("product_url") or ""),
                    str(job.get("product_name") or ""),
                ),
                media_mode=media_mode,
                asset_ids=[str(asset["id"]) for asset in selected_assets],
            )
            job = locked
        elif retry:
            if job.get("publish_stage") == "published":
                return media_publish_result(job)
            if job.get("publish_stage") == "outcome_unknown":
                raise local_media_outcome_unknown(job)
            try:
                job = store.resume_failed_publish(job_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
        elif job.get("publish_stage") == "published":
            return media_publish_result(job)
        elif job.get("publish_stage") == "outcome_unknown":
            raise local_media_outcome_unknown(job)
        elif job.get("publish_stage") == "failed":
            raise HTTPException(
                status_code=409,
                detail="게시 재시도 버튼을 사용해 주세요.",
            )
        if not acquire_media_publish(idempotency_key):
            current = store.get_job(job_id) or job
            raise HTTPException(
                status_code=409,
                detail={"message": "게시가 이미 진행 중입니다.", **media_publish_result(current)},
            )
        try:
            return run_local_media_publish(job_id, settings, store)
        finally:
            release_media_publish(idempotency_key)

    def refresh_threads_record_insights(
        job_id: str,
        settings: dict[str, str],
        store: WorkbenchStore,
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None or job.get("status") != "THREADS_PUBLISHED":
            raise HTTPException(status_code=404, detail="Threads publish record not found")
        post_id = str(job.get("threads_post_id") or "").strip()
        profile_key = str(job.get("threads_profile_key") or "").strip()
        if not post_id or not profile_key:
            raise HTTPException(status_code=400, detail="Threads publish record is missing post or profile data")
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        try:
            insights = get_threads_client(settings).fetch_media_insights(post_id, profile["access_token"])
        except ThreadsApiError as exc:
            store.update_threads_insights(job_id, {}, error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from None
        store.update_threads_insights(job_id, insights)
        record = store.get_threads_publish_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Threads publish record not found")
        return record

    def list_threads_publish_records_with_optional_insights(
        *,
        refresh_insights: bool,
        settings: dict[str, str],
        store: WorkbenchStore,
    ) -> list[dict[str, Any]]:
        records = store.list_threads_publish_records()
        if not refresh_insights:
            return records
        refreshed_records: list[dict[str, Any]] = []
        for record in records:
            job_id = str(record.get("job_id") or "").strip()
            if not job_id:
                refreshed_records.append(record)
                continue
            try:
                refreshed_records.append(refresh_threads_record_insights(job_id, settings, store))
            except HTTPException:
                current_record = store.get_threads_publish_record(job_id)
                refreshed_records.append(current_record or record)
        return refreshed_records

    def fetch_threads_permalink(
        client: ThreadsApiClient,
        post_id: str,
        access_token: str,
        job_id: str,
        store: WorkbenchStore,
    ) -> str:
        try:
            return client.fetch_media_permalink(post_id, access_token)
        except ThreadsApiError as exc:
            store.add_log(job_id, "ERROR", f"Threads permalink refresh failed: {exc}")
            return ""

    def refresh_threads_record_permalink(
        job_id: str,
        settings: dict[str, str],
        store: WorkbenchStore,
    ) -> dict[str, Any]:
        record = store.get_threads_publish_record(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Threads publish record not found")
        if record.get("threads_permalink"):
            return record
        post_id = str(record.get("threads_post_id") or "").strip()
        profile_key = str(record.get("profile_key") or "").strip()
        if not post_id or not profile_key:
            raise HTTPException(status_code=400, detail="Threads publish record is missing post or profile data")
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        permalink = fetch_threads_permalink(
            get_threads_client(settings),
            post_id,
            profile["access_token"],
            job_id,
            store,
        )
        if not permalink:
            raise HTTPException(status_code=400, detail="Threads permalink was not returned")
        store.update_threads_permalink(job_id, permalink)
        refreshed = store.get_threads_publish_record(job_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="Threads publish record not found")
        return refreshed

    def delete_threads_record(job_id: str, store: WorkbenchStore) -> dict[str, Any]:
        if not store.delete_threads_publish_record(job_id):
            raise HTTPException(status_code=404, detail="Threads publish record not found")
        return {"deleted": True, "job_id": job_id}

    def require_threads_bridge_access(settings: dict[str, str], request: Request) -> None:
        expected_api_key = os.environ.get(THREADS_BRIDGE_API_KEY_ENV, "").strip()
        if not expected_api_key:
            return
        provided_api_key = request.headers.get("X-Threads-Bridge-Key", "").strip()
        if not provided_api_key or not compare_digest(provided_api_key, expected_api_key):
            raise HTTPException(status_code=401, detail="Threads bridge API key is required")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    generated_dir = Path(db_path).parent / "generated"
    if generated_dir.exists():
        app.mount("/generated", StaticFiles(directory=generated_dir), name="generated")

    @app.get("/")
    def index() -> FileResponse:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Frontend has not been built")
        return FileResponse(index_path)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        if not ICON_PATH.exists():
            raise HTTPException(status_code=404, detail="Icon not found")
        return FileResponse(ICON_PATH)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        rednote_status = "ok"
        try:
            get_rednote_sidecar_client().health()
        except (HTTPException, RedNoteSidecarError):
            rednote_status = "unavailable"
        return {"status": "ok", "rednote": rednote_status}

    @app.get("/api/settings")
    def get_settings(store: WorkbenchStore = Depends(get_store)) -> dict[str, str]:
        return public_settings(store.get_settings())

    @app.put("/api/settings")
    def set_settings(
        payload: SettingsPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        settings = settings_to_store(payload, store.get_settings())
        return public_settings(store.set_settings(settings))

    @app.get("/api/jobs")
    def list_jobs(store: WorkbenchStore = Depends(get_store)) -> list[dict[str, Any]]:
        return [public_publish_job(job) for job in store.list_jobs()]

    @app.post("/api/coupang/product-preview")
    def preview_coupang_product(
        payload: CoupangProductPreviewPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        product_url = payload.product_url.strip()
        product_name = payload.product_name.strip()
        try:
            product, resolved_url = fetch_coupang_partner_product(
                product_url,
                store.get_settings(),
                product_keyword=product_name,
                sub_id=payload.sub_id,
            )
        except CoupangPartnersError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not product.product_name and not product.partner_url:
            raise HTTPException(status_code=400, detail="쿠팡 파트너스 API에서 상품 정보를 찾지 못했습니다.") from None
        if product.partner_url and not product.product_name:
            product = enrich_partner_product_with_local_context(
                product,
                original_url=product_url,
                resolved_url=resolved_url or product_url,
                product_name=product_name,
            )
        return product_preview_response(
            product,
            original_url=product_url,
            resolved_url=resolved_url or product_url,
            fallback_product_name=product_name if product.partner_url else "",
        )

    @app.post("/api/coupang/products/search")
    def search_coupang_products(
        payload: CoupangProductSearchPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        keyword = payload.keyword
        settings = store.get_settings()
        access_key = settings.get("coupang_access_key", "").strip()
        secret_key = settings.get("coupang_secret_key", "").strip()
        if not access_key or not secret_key:
            raise HTTPException(status_code=400, detail="쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요.")
        selected_sub_id = payload.sub_id.strip() or settings.get("coupang_sub_id", "").strip()
        try:
            client = CoupangPartnersClient(access_key, secret_key, sub_id=selected_sub_id)
            raw_products = client.search_products(keyword, limit=payload.limit)
        except CoupangPartnersError:
            raise HTTPException(
                status_code=400,
                detail="쿠팡 상품 검색에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            ) from None

        products: list[dict[str, Any]] = []
        seen_products: set[str] = set()
        for raw_product in raw_products:
            product = normalize_search_product(raw_product)
            if product is None:
                continue
            dedupe_key = product["product_id"] or product["product_url"]
            if dedupe_key in seen_products:
                continue
            seen_products.add(dedupe_key)
            products.append(product)
            if len(products) >= payload.limit:
                break
        return {"keyword": keyword, "products": products}

    @app.post("/api/coupang/chrome-product-context")
    def chrome_coupang_product_context(payload: CoupangProductPreviewPayload) -> dict[str, Any]:
        product_url = payload.product_url.strip()
        try:
            context = fetch_chrome_product_context(product_url)
        except LocalChromeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        product = CoupangPartnerProduct(
            product_name=context.page_title,
            product_url=context.resolved_url or product_url,
            image_url=context.image_url,
            facts=tuple(context.facts or ()),
        )
        return product_preview_response(
            product,
            original_url=product_url,
            resolved_url=context.resolved_url or product_url,
        )

    @app.post("/api/coupang/deeplink")
    def create_coupang_deeplink_endpoint(
        payload: CoupangDeeplinkPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        try:
            return create_coupang_deeplink(payload.product_url, store.get_settings(), sub_id=payload.sub_id)
        except CoupangPartnersError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/jobs")
    def create_job(
        payload: JobCreatePayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        product_name = payload.product_name.strip()
        image_url = payload.image_url.strip()
        if not product_name or not image_url:
            known_context = store.get_known_product_context(payload.product_url)
            product_name = product_name or known_context.get("product_name", "")
            image_url = image_url or known_context.get("image_url", "")
        if not product_name or not image_url:
            product_context = fetch_best_product_context(
                payload.product_url,
                product_name,
            )
            product_name = product_name or product_context.page_title
            image_url = image_url or product_context.image_url
        return public_publish_job(
            store.add_job(
                product_url=payload.product_url,
                product_name=product_name or "상품명 자동 확인 필요",
                image_url=image_url,
                memo=payload.memo,
            )
        )

    @app.post("/api/jobs/{job_id}/rednote-query")
    def create_rednote_query(
        job_id: str,
        payload: RedNoteQueryPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        source_keyword = payload.source_keyword.strip() or str(job["product_name"])
        query = generate_rednote_query(
            product_name=source_keyword,
            product_facts=payload.product_facts,
            model=FAST_CODEX_MODEL,
        )
        try:
            updated = store.save_rednote_query(job_id, query)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "job_id": job_id,
            "query": updated["rednote_query"],
            "generation": updated["rednote_query_generation"],
        }

    @app.post("/api/jobs/{job_id}/rednote-search")
    def search_rednote_for_job(
        job_id: str,
        _payload: RedNoteSearchPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        query = str(job.get("rednote_query") or "").strip()
        if not query:
            raise HTTPException(
                status_code=409,
                detail="RedNote 검색어를 먼저 만들어 주세요.",
            )
        try:
            raw_response = get_rednote_sidecar_client().search(query)
            search_id, results = normalize_rednote_search_response(raw_response)
        except RedNoteSidecarError as exc:
            raise_rednote_sidecar_http_error(exc)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="RedNote 검색 결과 형식이 올바르지 않습니다.",
            ) from None

        app.state.rednote_searches[job_id] = {
            "search_id": search_id,
            "results": {result["result_id"]: result for result in results},
        }
        try:
            store.update_studio_job(job_id, rednote_search_id=search_id)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="RedNote 검색 결과를 저장하지 못했습니다.",
            ) from None
        return {
            "job_id": job_id,
            "query": query,
            "search_id": search_id,
            "results": results,
        }

    @app.post("/api/jobs/{job_id}/rednote-download")
    def download_rednote_for_job(
        job_id: str,
        payload: RedNoteDownloadPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        reject_locked_publish_mutation(job)
        current_search = app.state.rednote_searches.get(job_id)
        if (
            not isinstance(current_search, dict)
            or job.get("rednote_search_id") != payload.search_id
            or current_search.get("search_id") != payload.search_id
        ):
            raise HTTPException(
                status_code=409,
                detail="최신 RedNote 검색 결과에서 다시 선택해 주세요.",
            )
        selected = current_search.get("results", {}).get(payload.result_id)
        if not isinstance(selected, dict) or selected.get("note_id") != payload.note_id:
            raise HTTPException(
                status_code=400,
                detail="선택한 RedNote 영상이 현재 검색 결과에 없습니다.",
            )
        try:
            sidecar = get_rednote_sidecar_client()
            resolved = sidecar.resolve_search_result(payload.search_id, payload.result_id)
            session_id = str(resolved.get("sessionId") or "").strip()
            resolved_note = resolved.get("note")
            if (
                not _REDNOTE_OPAQUE_ID.fullmatch(session_id)
                or not isinstance(resolved_note, dict)
                or resolved_note.get("noteId") != payload.note_id
            ):
                raise ValueError("invalid resolve response")
            created = sidecar.create_job(session_id)
            sidecar_job_id = str(created.get("jobId") or "").strip()
            if not _REDNOTE_OPAQUE_ID.fullmatch(sidecar_job_id):
                raise ValueError("invalid download response")
        except RedNoteSidecarError as exc:
            raise_rednote_sidecar_http_error(exc)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail="RedNote 다운로드 응답 형식이 올바르지 않습니다.",
            ) from None

        store.update_studio_job(
            job_id,
            rednote_note_id=payload.note_id,
            rednote_canonical_url=selected["canonical_url"],
            rednote_sidecar_job_id=sidecar_job_id,
        )
        return {
            "job_id": job_id,
            "note_id": payload.note_id,
            "canonical_url": selected["canonical_url"],
            "sidecar_job_id": sidecar_job_id,
            "media_url": f"/api/jobs/{job_id}/rednote-video",
            "note": {
                "title": _safe_public_text(resolved_note.get("title"), 160),
                "duration_ms": max(0, int(resolved_note.get("durationMs") or 0)),
                "width": max(0, int(resolved_note.get("width") or 0)),
                "height": max(0, int(resolved_note.get("height") or 0)),
            },
        }

    @app.get("/api/jobs/{job_id}/rednote-video")
    def proxy_rednote_video(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> Response:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        sidecar_job_id = str(job.get("rednote_sidecar_job_id") or "").strip()
        if not sidecar_job_id:
            raise HTTPException(
                status_code=409,
                detail="RedNote 영상을 먼저 다운로드해 주세요.",
            )
        try:
            binary = get_rednote_sidecar_client().get_video(
                sidecar_job_id,
                range_header=request.headers.get("Range", ""),
            )
        except RedNoteSidecarError as exc:
            raise_rednote_sidecar_http_error(exc)
        response_headers = {
            key: value
            for key, value in binary.headers.items()
            if key.lower() in {"accept-ranges", "content-length", "content-range"} and value
        }
        return Response(
            content=binary.body,
            status_code=binary.status_code,
            media_type="video/mp4",
            headers=response_headers,
        )

    @app.put("/api/jobs/{job_id}/rednote-frames/{index}")
    async def proxy_rednote_frame(
        job_id: str,
        index: int,
        request: Request,
        time_ms: float,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        reject_locked_publish_mutation(job)
        sidecar_job_id = str(job.get("rednote_sidecar_job_id") or "").strip()
        if not sidecar_job_id:
            raise HTTPException(
                status_code=409,
                detail="RedNote 영상을 먼저 다운로드해 주세요.",
            )
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/jpeg":
            raise HTTPException(status_code=415, detail="JPEG 이미지만 저장할 수 있습니다.")
        jpeg = await request.body()
        try:
            saved = get_rednote_sidecar_client().upload_frame(
                sidecar_job_id,
                index,
                time_ms,
                jpeg,
            )
        except RedNoteSidecarError as exc:
            raise_rednote_sidecar_http_error(exc)
        file_name = _safe_sidecar_filename(saved.get("fileName"))
        if not file_name:
            raise HTTPException(
                status_code=502,
                detail="RedNote 대표 장면 저장 응답이 올바르지 않습니다.",
            )
        return {
            "job_id": job_id,
            "index": index,
            "time_ms": time_ms,
            "file_name": file_name,
        }

    @app.post("/api/jobs/{job_id}/rednote-complete")
    def complete_rednote_download(
        job_id: str,
        _payload: RedNoteCompletePayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        reject_locked_publish_mutation(job)
        sidecar_job_id = str(job.get("rednote_sidecar_job_id") or "").strip()
        if not sidecar_job_id:
            raise HTTPException(
                status_code=409,
                detail="RedNote 영상을 먼저 다운로드해 주세요.",
            )
        try:
            completed = get_rednote_sidecar_client().complete(sidecar_job_id)
        except RedNoteSidecarError as exc:
            raise_rednote_sidecar_http_error(exc)
        output_root = os.environ.get(
            REDNOTE_OUTPUT_ROOT_ENV,
            str(Path.home() / "Downloads" / "rednote"),
        )
        try:
            assets = build_rednote_assets_from_completion(
                job,
                completed,
                output_root=output_root,
            )
            persisted = store.replace_rednote_assets(job_id, assets)
        except PublishPayloadLockedError:
            raise HTTPException(
                status_code=409,
                detail="게시가 시작된 작업의 RedNote 미디어는 변경할 수 없습니다.",
            ) from None
        except (KeyError, ValueError):
            raise HTTPException(
                status_code=502,
                detail="RedNote 완료 응답이 올바르지 않습니다.",
            ) from None
        return {
            "job_id": job_id,
            "note_id": job["rednote_note_id"],
            "frame_count": len([asset for asset in persisted if asset["asset_type"] == "frame"]),
            "assets": [public_rednote_asset(job_id, asset) for asset in persisted],
        }

    @app.get("/api/jobs/{job_id}/rednote-assets")
    def list_rednote_job_assets(
        job_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        assets = store.list_rednote_assets(job_id)
        return {
            "job_id": job_id,
            "media_mode": str(job.get("media_mode") or ""),
            "assets": [public_rednote_asset(job_id, asset) for asset in assets],
        }

    @app.get("/api/jobs/{job_id}/rednote-assets/{asset_id}")
    def serve_rednote_frame_asset(
        job_id: str,
        asset_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> FileResponse:
        if store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        asset = next(
            (
                candidate
                for candidate in store.list_rednote_assets(job_id)
                if candidate["id"] == asset_id
            ),
            None,
        )
        if asset is None or asset.get("asset_type") != "frame":
            raise HTTPException(status_code=404, detail="RedNote frame not found")
        output_root = os.environ.get(
            REDNOTE_OUTPUT_ROOT_ENV,
            str(Path.home() / "Downloads" / "rednote"),
        )
        try:
            root = _strict_directory(output_root, "RedNote output root")
            local_path = Path(str(asset.get("local_path") or ""))
            local_path.relative_to(root)
            media_file = _strict_regular_file(local_path.parent, local_path.name)
        except ValueError:
            raise HTTPException(status_code=404, detail="RedNote frame not found") from None
        if not _looks_like_jpeg(media_file):
            raise HTTPException(status_code=404, detail="RedNote frame not found")
        return FileResponse(
            media_file,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.patch("/api/jobs/{job_id}/media-selection")
    def select_rednote_job_assets(
        job_id: str,
        payload: MediaSelectionPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            assets = store.select_rednote_assets(job_id, payload.asset_ids, payload.mode)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        except PublishPayloadLockedError:
            raise HTTPException(
                status_code=409,
                detail="게시가 시작된 작업의 미디어 선택은 변경할 수 없습니다.",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "job_id": job_id,
            "media_mode": payload.mode,
            "assets": [public_rednote_asset(job_id, asset) for asset in assets],
            "selected_asset_ids": [asset["id"] for asset in assets if asset["selected"]],
        }

    @app.patch("/api/jobs/{job_id}/copy-selection")
    def select_threads_copy_variant(
        job_id: str,
        payload: CopySelectionPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            selected = store.select_copy_variant(job_id, payload.variant_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        except PublishPayloadLockedError:
            raise HTTPException(
                status_code=409,
                detail="게시가 시작된 작업의 본문 선택은 변경할 수 없습니다.",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {
            "selected_variant_id": selected["id"],
            "variants": [
                public_copy_variant(variant)
                for variant in store.list_copy_variants(job_id)
            ],
        }

    @app.patch("/api/jobs/{job_id}/copy-variants/{variant_id}")
    def edit_threads_copy_variant(
        job_id: str,
        variant_id: str,
        payload: CopyVariantEditPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            updated = store.update_copy_variant_body(job_id, variant_id, payload.body)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        except PublishPayloadLockedError:
            raise HTTPException(
                status_code=409,
                detail="게시가 시작된 작업의 본문은 변경할 수 없습니다.",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        preview = store.get_studio_preview(job_id)
        selected_variant = preview.get("selected_copy_variant")
        return {
            "selected_variant_id": str((selected_variant or {}).get("id") or ""),
            "selected_copy_variant": public_copy_variant(selected_variant or updated),
            "variants": [public_copy_variant(variant) for variant in preview["copy_variants"]],
        }

    @app.get("/api/jobs/{job_id}/studio-preview")
    def get_job_studio_preview(
        job_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        return studio_preview_response(job_id, store)

    @app.post("/api/jobs/{job_id}/threads-media-publish")
    def publish_job_threads_media(
        job_id: str,
        _payload: ThreadsMediaPublishActionPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        return publish_studio_media(job_id, retry=False, store=store)

    @app.post("/api/jobs/{job_id}/threads-media-retry")
    def retry_job_threads_media(
        job_id: str,
        _payload: ThreadsMediaPublishActionPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        return publish_studio_media(job_id, retry=True, store=store)

    @app.post("/api/jobs/{job_id}/draft")
    def draft_job(job_id: str, store: WorkbenchStore = Depends(get_store)) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        image_url = job.get("image_url", "").strip()
        draft = generate_draft(
            product_name=job["product_name"],
            product_url=job["product_url"],
            memo=job["memo"],
            persona=settings.get("writer_persona", ""),
            image_url=image_url,
        )
        return public_publish_job(
            store.update_job_draft(
                job_id,
                title=draft.title,
                draft=draft.body,
                tags=draft.tags,
            )
        )

    @app.post("/api/jobs/{job_id}/campaign")
    def campaign_job(job_id: str, store: WorkbenchStore = Depends(get_store)) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        product_context = fetch_best_product_context(
            job["product_url"],
            job["product_name"],
        )
        if not (product_context.facts or product_context.description.strip()):
            known_campaign = store.get_known_campaign_context(job["product_url"])
            if known_campaign:
                return public_publish_job(
                    store.update_job_campaign(
                        job_id,
                        sns_draft=known_campaign["sns_draft"],
                        image_brief=known_campaign["image_brief"],
                        blog_final=known_campaign["blog_final"],
                        sns_final=known_campaign["sns_final"],
                        title=known_campaign["title"],
                        tags=known_campaign["tags"],
                        image_url=job.get("image_url", "").strip()
                        or known_campaign.get("image_url", ""),
                    )
                )
        reference_image_url = job.get("image_url", "").strip() or product_context.image_url
        product_name = job["product_name"] or product_context.page_title
        campaign = generate_campaign(
            product_name=product_name,
            product_url=job["product_url"],
            memo=job["memo"],
            reference_image_url=reference_image_url,
            persona=settings.get("writer_persona", ""),
            product_facts=product_context.facts or [],
            product_page_title=product_name,
            product_description=product_context.description,
        )
        return public_publish_job(
            store.update_job_campaign(
                job_id,
                sns_draft=campaign.sns_draft,
                image_brief=campaign.image_brief,
                blog_final=campaign.blog_final,
                sns_final=campaign.sns_final,
                title=campaign.title,
                tags=campaign.tags,
                image_url=reference_image_url if reference_image_url else None,
            )
        )

    @app.get("/api/jobs/{job_id}/media")
    def list_media_candidates(
        job_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        if store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return store.list_media_candidates(job_id)

    @app.post("/api/jobs/{job_id}/media")
    def create_media_candidate(
        job_id: str,
        payload: MediaCandidatePayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            return store.add_media_candidate(job_id=job_id, **payload.model_dump())
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None

    @app.post("/api/media/{candidate_id}/approve")
    def approve_media_candidate(
        candidate_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            return store.approve_media_candidate(candidate_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Media candidate not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/media/{candidate_id}/reject")
    def reject_media_candidate(
        candidate_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            return store.reject_media_candidate(candidate_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Media candidate not found") from None

    @app.post("/api/jobs/{job_id}/publish")
    def publish_job(
        job_id: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> PublishHandoff:
        if store.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        message = publish_handoff_message()
        store.mark_publish_handoff(job_id, message)
        return PublishHandoff(status="NEEDS_BROWSER_REVIEW", message=message)

    @app.get("/api/threads/profiles")
    def list_threads_profiles(
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).list_profiles()
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        return store.list_threads_profiles()

    @app.get("/api/threads/publish-records")
    def list_threads_publish_records(
        request: Request,
        refresh_insights: bool = False,
        store: WorkbenchStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).list_publish_records(refresh_insights=refresh_insights)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        return list_threads_publish_records_with_optional_insights(
            refresh_insights=refresh_insights,
            settings=settings,
            store=store,
        )

    @app.post("/api/threads/publish-records/{job_id}/insights")
    def refresh_threads_publish_record_insights(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).refresh_record_insights(job_id)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        return refresh_threads_record_insights(job_id, settings, store)

    @app.post("/api/threads/publish-records/{job_id}/permalink")
    def refresh_threads_publish_record_permalink(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).get_record_permalink(job_id)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        return refresh_threads_record_permalink(job_id, settings, store)

    @app.delete("/api/threads/publish-records/{job_id}")
    def delete_threads_publish_record(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).delete_publish_record(job_id)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        return delete_threads_record(job_id, store)

    @app.post("/api/threads/profiles")
    def upsert_threads_profile(
        payload: ThreadsProfilePayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).upsert_profile(
                    profile_key=payload.profile_key,
                    display_name=payload.display_name,
                    notes=payload.notes,
                )
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        try:
            return store.upsert_threads_profile(
                profile_key=payload.profile_key,
                display_name=payload.display_name,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/threads/auth/start")
    def start_threads_auth(
        profile_key: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).start_auth(profile_key.strip())
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        profile = store.get_threads_profile(profile_key)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        client = get_threads_client(settings)
        state = store.issue_threads_oauth_state(profile_key=profile_key)
        return {"auth_url": client.build_authorization_url(state)}

    @app.get("/api/threads/auth/import/start")
    def start_threads_profile_import(
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).start_import()
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        client = get_threads_client(settings)
        state = store.issue_threads_oauth_state(profile_key=None)
        return {"auth_url": client.build_authorization_url(state)}

    @app.get("/api/threads/auth/callback", response_class=HTMLResponse)
    def threads_auth_callback(
        code: str,
        state: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> str:
        callback_state = state.strip()
        if not callback_state:
            raise HTTPException(status_code=400, detail="Missing profile state")
        oauth_state = store.consume_threads_oauth_state(callback_state)
        if oauth_state is None:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
        is_import = oauth_state["purpose"] == "import"
        profile_key = oauth_state["profile_key"]
        if not is_import and store.get_threads_profile(profile_key) is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        client = get_threads_client(store.get_settings())
        try:
            short_token = client.exchange_code_for_short_token(code)
            long_token = client.exchange_for_long_lived_token(short_token["access_token"])
            profile = client.fetch_me(long_token["access_token"])
        except (KeyError, ThreadsApiError) as exc:
            raise HTTPException(status_code=400, detail=f"Threads auth failed: {exc}") from None
        username = str(profile.get("username") or profile.get("name") or "")
        threads_user_id = str(profile.get("id") or short_token.get("user_id") or "")
        if is_import:
            profile_key = username.strip() or threads_user_id.strip()
            display_name = str(profile.get("name") or username or profile_key)
            store.upsert_threads_profile(
                profile_key=profile_key,
                display_name=display_name,
            )
        store.save_threads_profile_token(
            profile_key=profile_key,
            threads_user_id=threads_user_id,
            username=username,
            access_token=str(long_token["access_token"]),
            expires_in=int(long_token.get("expires_in") or 0),
        )
        return """
        <!doctype html>
        <html lang="ko">
          <head><meta charset="utf-8"><title>Threads 연결 완료</title></head>
          <body>
            <h1>Threads 연결 완료</h1>
            <p>이 창을 닫고 워크벤치로 돌아가도 됩니다.</p>
          </body>
        </html>
        """

    @app.post("/api/threads/draft")
    def create_threads_draft(
        payload: ThreadsDraftPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if not payload.job_id.strip():
            initial_persona_keys = {key for key, _label in THREAD_DRAFT_VARIANTS}
            if payload.custom_persona:
                initial_persona_keys.add("custom")
            unknown_initial_keys = [
                key
                for key in payload.regenerate_persona_keys
                if key not in initial_persona_keys
            ]
            if unknown_initial_keys:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown persona key: {unknown_initial_keys[0]}",
                )
        existing_job = None
        if payload.job_id.strip():
            existing_job = store.get_job(payload.job_id.strip())
            if existing_job is None:
                raise HTTPException(status_code=404, detail="Studio job not found")
            reject_locked_publish_mutation(existing_job)
            job = existing_job
            product_name = job["product_name"]
            product_url = job["product_url"]
            refreshed_context = fetch_best_product_context(product_url, product_name)
            product_facts = merge_product_facts(refreshed_context.facts, payload.facts)
        else:
            product_name = payload.product_name.strip()
            product_url = payload.product_url.strip()
            product_context = None
            partner_url = payload.partner_url.strip()
            api_error = ""
            if settings.get("coupang_access_key", "").strip() and settings.get(
                "coupang_secret_key", ""
            ).strip():
                try:
                    partner_product, resolved_url = fetch_coupang_partner_product(
                        product_url,
                        settings,
                        product_keyword=product_name,
                        sub_id=payload.coupang_channel_id,
                    )
                    if partner_product.partner_url and not partner_product.product_name and not product_name:
                        partner_product = enrich_partner_product_with_local_context(
                            partner_product,
                            original_url=product_url,
                            resolved_url=resolved_url or product_url,
                        )
                    if partner_product.product_name:
                        product_context = partner_product.to_product_context(
                            source_url=product_url,
                            resolved_url=resolved_url or product_url,
                        )
                        product_name = partner_product.product_name
                        partner_url = partner_url or partner_product.partner_url
                    elif product_name and partner_product.partner_url:
                        partner_url = partner_url or partner_product.partner_url
                        product_context = CoupangPartnerProduct(
                            product_name=product_name,
                            product_url=resolved_url or product_url,
                            partner_url=partner_url,
                            image_url=partner_product.image_url,
                            facts=partner_product.facts,
                            product_id=partner_product.product_id,
                        ).to_product_context(
                            source_url=product_url,
                            resolved_url=resolved_url or product_url,
                        )
                    else:
                        api_error = "상품명으로 쿠팡 API에서 정확한 상품을 확인하지 못했습니다."
                except CoupangPartnersError as exc:
                    api_error = str(exc)
            if (
                settings.get("coupang_access_key", "").strip()
                and settings.get("coupang_secret_key", "").strip()
                and product_context is None
            ):
                detail = api_error or "쿠팡 파트너스 API에서 상품 정보를 먼저 확인해 주세요."
                raise HTTPException(status_code=400, detail=detail)
            if product_context is None:
                product_context = fetch_best_product_context(product_url, product_name)
            product_facts = merge_product_facts(product_context.facts, payload.facts)
            if not product_name:
                known_context = store.get_known_product_context(product_url)
                product_name = known_context.get("product_name", "") or product_context.page_title
            if not product_name:
                detail = (
                    "쿠팡 파트너스 API에서 상품 정보를 찾지 못했습니다."
                    if settings.get("coupang_access_key", "").strip()
                    and settings.get("coupang_secret_key", "").strip()
                    else "쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요."
                )
                if api_error:
                    detail = f"{detail} ({api_error})"
                raise HTTPException(status_code=400, detail=detail)
            job = store.add_job(
                product_url=partner_url or product_url,
                product_name=product_name or "상품명 자동 확인 필요",
                memo=payload.memo,
            )

        existing_variants = store.list_copy_variants(job["id"])
        existing_by_key = {
            variant["persona_key"]: variant for variant in existing_variants
        }
        custom_instruction = payload.custom_persona
        if not custom_instruction and "custom" in existing_by_key:
            custom_instruction = existing_by_key["custom"]["custom_instruction"]

        persona_specs = [
            {"persona_key": key, "persona_label": label, "custom_instruction": ""}
            for key, label in THREAD_DRAFT_VARIANTS
        ]
        if custom_instruction:
            persona_specs.append(
                {
                    "persona_key": "custom",
                    "persona_label": "커스텀",
                    "custom_instruction": custom_instruction,
                }
            )
        specs_by_key = {spec["persona_key"]: spec for spec in persona_specs}
        unknown_keys = [
            key for key in payload.regenerate_persona_keys if key not in specs_by_key
        ]
        if unknown_keys:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown persona key: {unknown_keys[0]}",
            )
        if payload.profile_key.strip():
            job = store.update_studio_job(
                job["id"], selected_profile_key=payload.profile_key.strip()
            )
        if existing_job is not None and payload.regenerate_persona_keys:
            target_specs = [specs_by_key[key] for key in payload.regenerate_persona_keys]
        else:
            target_specs = persona_specs

        variant_texts = {
            spec["persona_key"]: generate_threads_post(
                product_name=job["product_name"],
                product_url=job["product_url"],
                product_facts=product_facts,
                memo=payload.memo or job.get("memo", ""),
                persona=settings.get("writer_persona", ""),
                style=fallback_style_for_persona(
                    spec["persona_key"], spec["custom_instruction"]
                ),
            )
            for spec in target_specs
        }
        comment_text = generate_threads_comment(job["product_url"], job["product_name"])
        codex_kwargs = {
            "model": THREADS_COPY_CODEX_MODEL,
            "product_name": job["product_name"],
            "product_url": job["product_url"],
            "product_facts": product_facts,
            "memo": payload.memo or job.get("memo", ""),
            "persona": settings.get("writer_persona", ""),
            "prompt": payload.codex_threads_prompt,
        }
        with ThreadPoolExecutor(max_workers=min(4, len(target_specs))) as executor:
            pending = {
                executor.submit(
                    generate_codex_threads_post,
                    **codex_kwargs,
                    style=spec["persona_key"],
                    custom_instruction=spec["custom_instruction"],
                ): spec["persona_key"]
                for spec in target_specs
            }
            for future in as_completed(pending):
                persona_key = pending[future]
                try:
                    generated = future.result().strip()
                    if generated:
                        variant_texts[persona_key] = generated
                except CodexThreadsError:
                    continue

        variants_to_store = [
            {
                "id": existing_by_key.get(spec["persona_key"], {}).get("id", ""),
                "persona_key": spec["persona_key"],
                "persona_label": spec["persona_label"],
                "custom_instruction": spec["custom_instruction"],
                "body": variant_texts[spec["persona_key"]],
                "generation": int(
                    existing_by_key.get(spec["persona_key"], {}).get("generation", 0)
                )
                + 1,
            }
            for spec in target_specs
        ]
        stored_variants = store.upsert_copy_variants(job["id"], variants_to_store)
        selected_variant = next(
            (variant for variant in stored_variants if variant["selected"]),
            None,
        )
        if selected_variant is None:
            selected_variant = next(
                variant
                for variant in stored_variants
                if variant["persona_key"] == "curiosity"
            )
            selected_variant = store.select_copy_variant(
                job["id"], selected_variant["id"]
            )
        threads_text = selected_variant["body"]
        store.update_job_threads_draft(
            job["id"],
            text=threads_text,
            comment_text=comment_text,
            title=f"{job['product_name']} Threads",
            tags=["쿠팡파트너스", "Threads"],
        )
        preview = store.get_studio_preview(job["id"])
        variants = [
            {
                "id": variant["id"],
                "persona_key": variant["persona_key"],
                "label": variant["persona_label"],
                "text": variant["body"],
                "generation": variant["generation"],
                "selected": variant["selected"],
                "custom_instruction": variant["custom_instruction"],
            }
            for variant in preview["copy_variants"]
        ]
        return {
            "job": public_publish_job(preview["job"]),
            "text": threads_text,
            "variants": variants,
            "selected_variant_id": selected_variant["id"],
            "comment_text": comment_text,
        }

    def publish_threads_job(
        *,
        job: dict[str, Any],
        profile_key: str,
        text: str,
        comment_text: str,
        store: WorkbenchStore,
    ) -> dict[str, Any]:
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        client = get_threads_client(store.get_settings())
        try:
            published = client.publish_text(
                threads_user_id=profile["threads_user_id"],
                access_token=profile["access_token"],
                text=text,
            )
        except ThreadsApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        post_id = str(published.get("id", "")).strip()
        if not post_id:
            raise HTTPException(status_code=400, detail="Threads publish response did not include an id")
        permalink = fetch_threads_permalink(client, post_id, profile["access_token"], job["id"], store)
        comment_text = comment_text.strip()
        reply_id = ""
        if comment_text:
            try:
                reply = client.publish_reply(
                    threads_user_id=profile["threads_user_id"],
                    access_token=profile["access_token"],
                    text=comment_text,
                    reply_to_id=post_id,
                )
            except ThreadsApiError as exc:
                error_detail = f"Threads reply failed after post publish: post_id={post_id}; error={exc}"
                updated_job = store.mark_threads_published(
                    job_id=job["id"],
                    profile_key=profile_key,
                    threads_post_id=post_id,
                    threads_reply_id="",
                    threads_permalink=permalink,
                    published_text=f"본문:\n{text.strip()}\n\n댓글:\n{comment_text}",
                )
                store.add_log(job["id"], "ERROR", error_detail)
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Threads post was published, but reply publishing failed",
                        "threads_post_id": post_id,
                        "threads_reply_id": "",
                        "threads_permalink": permalink,
                        "error": str(exc),
                        "job": public_publish_job(updated_job),
                    },
                ) from None
            reply_id = str(reply.get("id", "")).strip()
        updated_job = store.mark_threads_published(
            job_id=job["id"],
            profile_key=profile_key,
            threads_post_id=post_id,
            threads_reply_id=reply_id,
            threads_permalink=permalink,
            published_text=f"본문:\n{text.strip()}\n\n댓글:\n{comment_text}" if comment_text else text,
        )
        return {
            "status": "THREADS_PUBLISHED",
            "threads_post_id": post_id,
            "threads_reply_id": reply_id,
            "threads_permalink": permalink,
            "job": public_publish_job(updated_job),
        }

    @app.post("/api/threads/publish")
    def publish_threads_post(
        payload: ThreadsPublishPayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        job = store.get_job(payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                remote_result = get_threads_bridge_client(settings).publish(
                    profile_key=payload.profile_key,
                    product_url=job["product_url"],
                    product_name=job["product_name"],
                    text=payload.text,
                    comment_text=payload.comment_text,
                )
            except ThreadsBridgeError as exc:
                if exc.status_code == 400 and isinstance(exc.detail, dict):
                    post_id = str(exc.detail.get("threads_post_id") or "").strip()
                    if post_id:
                        reply_id = str(exc.detail.get("threads_reply_id") or "").strip()
                        permalink = str(exc.detail.get("threads_permalink") or "").strip()
                        updated_job = store.mark_threads_published(
                            job_id=payload.job_id,
                            profile_key=payload.profile_key,
                            threads_post_id=post_id,
                            threads_reply_id=reply_id,
                            threads_permalink=permalink,
                            published_text=(
                                f"본문:\n{payload.text.strip()}\n\n댓글:\n{payload.comment_text.strip()}"
                                if payload.comment_text.strip()
                                else payload.text
                            ),
                        )
                        detail = dict(exc.detail)
                        detail["job"] = public_publish_job(updated_job)
                        raise HTTPException(status_code=400, detail=detail) from None
                raise HTTPException(status_code=502, detail=str(exc)) from None
            post_id = str(remote_result.get("threads_post_id", "")).strip()
            reply_id = str(remote_result.get("threads_reply_id", "")).strip()
            permalink = str(
                remote_result.get("threads_permalink")
                or (remote_result.get("job") or {}).get("threads_permalink")
                or ""
            ).strip()
            if not post_id:
                raise HTTPException(status_code=502, detail="Threads service did not return a post id")
            updated_job = store.mark_threads_published(
                job_id=payload.job_id,
                profile_key=payload.profile_key,
                threads_post_id=post_id,
                threads_reply_id=reply_id,
                threads_permalink=permalink,
                published_text=(
                    f"본문:\n{payload.text.strip()}\n\n댓글:\n{payload.comment_text.strip()}"
                    if payload.comment_text.strip()
                    else payload.text
                ),
            )
            return {
                "status": "THREADS_PUBLISHED",
                "threads_post_id": post_id,
                "threads_reply_id": reply_id,
                "threads_permalink": permalink,
                "job": public_publish_job(updated_job),
                "remote_job": public_publish_job(remote_result.get("job", {})),
            }
        require_threads_bridge_access(settings, request)
        return publish_threads_job(
            job=job,
            profile_key=payload.profile_key,
            text=payload.text,
            comment_text=payload.comment_text,
            store=store,
        )

    @app.post("/api/threads/remote-publish")
    def publish_remote_threads_post(
        payload: ThreadsRemotePublishPayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        require_threads_bridge_access(settings, request)
        job = store.add_job(
            product_url=payload.product_url,
            product_name=payload.product_name,
        )
        job = store.update_job_threads_draft(
            job["id"],
            text=payload.text,
            comment_text=payload.comment_text,
            title=f"{payload.product_name} Threads",
            tags=["쿠팡파트너스", "Threads"],
        )
        return publish_threads_job(
            job=job,
            profile_key=payload.profile_key,
            text=payload.text,
            comment_text=payload.comment_text,
            store=store,
        )

    @app.post("/api/threads/profiles/{profile_key}/refresh")
    def refresh_threads_profile_token(
        profile_key: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).refresh_profile(profile_key)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        client = get_threads_client(settings)
        try:
            refreshed = client.refresh_long_lived_token(profile["access_token"])
        except ThreadsApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return store.save_threads_profile_token(
            profile_key=profile_key,
            threads_user_id=profile["threads_user_id"],
            username=profile.get("username", ""),
            access_token=str(refreshed["access_token"]),
            expires_in=int(refreshed.get("expires_in") or 0),
        )

    @app.post("/api/threads/profiles/{profile_key}/disconnect")
    def disconnect_threads_profile(
        profile_key: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        settings = store.get_settings()
        if uses_remote_threads_service(settings):
            try:
                return get_threads_bridge_client(settings).disconnect_profile(profile_key)
            except ThreadsBridgeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from None
        require_threads_bridge_access(settings, request)
        disconnected = store.disconnect_threads_profile(profile_key)
        if disconnected is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        return disconnected

    @app.get("/api/logs")
    def list_logs(store: WorkbenchStore = Depends(get_store)) -> list[dict[str, Any]]:
        return store.list_logs()

    return app


app = create_app()
