from __future__ import annotations

import base64
import binascii
import logging
import os
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .media_uploads import MediaUploadError, TemporaryMediaStore
from .schemas import (
    ThreadsMediaUploadPayload,
    ThreadsMediaUploadPartPayload,
    ThreadsMediaUploadStartPayload,
    ThreadsProfilePayload,
    ThreadsRemoteMediaPublishPayload,
    ThreadsRemotePublishPayload,
)
from .storage import WorkbenchStore, utc_now
from .threads import ThreadsApiClient, ThreadsApiError

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PACKAGE_DIR.parent / "workbench_data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "threads_api.sqlite3"
THREADS_BRIDGE_API_KEY_ENV = "THREADS_BRIDGE_API_KEY"
THREADS_APP_ID_ENV = "THREADS_APP_ID"
THREADS_APP_SECRET_ENV = "THREADS_APP_SECRET"
THREADS_REDIRECT_URI_ENV = "THREADS_REDIRECT_URI"
THREADS_PUBLIC_BASE_URL_ENV = "THREADS_PUBLIC_BASE_URL"
logger = logging.getLogger(__name__)
_PRIVATE_REMOTE_PUBLISH_FIELDS = frozenset(
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


def create_threads_api_app(db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="Threads Coupang API")
    store = WorkbenchStore(db_path)
    media_store = TemporaryMediaStore(db_path)

    def get_store() -> WorkbenchStore:
        return store

    def public_base_url(request: Request) -> str:
        configured = os.environ.get(THREADS_PUBLIC_BASE_URL_ENV, "").strip().rstrip("/")
        if configured:
            return configured
        return str(request.base_url).rstrip("/")

    def decode_media_payload(payload: ThreadsMediaUploadPayload) -> bytes:
        try:
            content = base64.b64decode(payload.content_base64.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="content_base64 must be valid base64") from exc
        if not content:
            raise HTTPException(status_code=400, detail="content_base64 is empty")
        return content

    def require_bridge_access(request: Request) -> None:
        expected_api_key = os.environ.get(THREADS_BRIDGE_API_KEY_ENV, "").strip()
        if not expected_api_key:
            raise HTTPException(status_code=500, detail="THREADS_BRIDGE_API_KEY is required")
        provided_api_key = request.headers.get("X-Threads-Bridge-Key", "").strip()
        if not provided_api_key or not compare_digest(provided_api_key, expected_api_key):
            raise HTTPException(status_code=401, detail="Threads bridge API key is required")

    def get_threads_client() -> ThreadsApiClient:
        app_id = os.environ.get(THREADS_APP_ID_ENV, "").strip()
        app_secret = os.environ.get(THREADS_APP_SECRET_ENV, "").strip()
        redirect_uri = os.environ.get(THREADS_REDIRECT_URI_ENV, "").strip()
        if not app_id or not app_secret or not redirect_uri:
            raise HTTPException(status_code=500, detail="Threads API env settings are required")
        return ThreadsApiClient(
            app_id=app_id,
            app_secret=app_secret,
            redirect_uri=redirect_uri,
        )

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
            logger.warning("Threads permalink refresh failed for %s: %s", post_id, exc)
            return ""

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
        client = get_threads_client()
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
        clean_comment = comment_text.strip()
        reply_id = ""
        if clean_comment:
            try:
                reply = client.publish_reply(
                    threads_user_id=profile["threads_user_id"],
                    access_token=profile["access_token"],
                    text=clean_comment,
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
                    published_text=f"본문:\n{text.strip()}\n\n댓글:\n{clean_comment}",
                )
                store.add_log(job["id"], "ERROR", error_detail)
                logger.warning(error_detail)
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "Threads post was published, but reply publishing failed",
                        "threads_post_id": post_id,
                        "threads_reply_id": "",
                        "error": str(exc),
                        "job": updated_job,
                    },
                ) from None
            reply_id = str(reply.get("id", "")).strip()
        updated_job = store.mark_threads_published(
            job_id=job["id"],
            profile_key=profile_key,
            threads_post_id=post_id,
            threads_reply_id=reply_id,
            threads_permalink=permalink,
            published_text=f"본문:\n{text.strip()}\n\n댓글:\n{clean_comment}" if clean_comment else text,
        )
        return {
            "status": "THREADS_PUBLISHED",
            "threads_post_id": post_id,
            "threads_reply_id": reply_id,
            "threads_permalink": permalink,
            "job": updated_job,
        }

    def remote_media_result(job: dict[str, Any]) -> dict[str, Any]:
        post_id = str(job.get("threads_post_id") or "").strip()
        reply_id = str(job.get("threads_reply_id") or "").strip()
        return {
            "job": {
                key: value
                for key, value in job.items()
                if key not in _PRIVATE_REMOTE_PUBLISH_FIELDS
            },
            "publish_stage": str(job.get("publish_stage") or "draft"),
            "threads_post_id": post_id,
            "threads_reply_id": reply_id,
            "threads_permalink": str(job.get("threads_permalink") or "").strip(),
            "partial": bool(post_id and not reply_id),
        }

    def remote_media_publish_conflict(
        job_id: str,
        store: WorkbenchStore,
    ) -> HTTPException:
        current = store.get_job(job_id)
        detail: dict[str, Any] = {
            "message": "Remote media publish is already running"
        }
        if current is not None:
            detail.update(remote_media_result(current))
        return HTTPException(status_code=409, detail=detail)

    def remote_media_outcome_unknown(
        job_id: str,
        store: WorkbenchStore,
    ) -> HTTPException:
        current = store.get_job(job_id)
        detail: dict[str, Any] = {
            "code": "PUBLISH_OUTCOME_UNKNOWN",
            "message": (
                "Threads 응답을 확인하지 못해 자동 재시도를 중단했습니다. "
                "Threads에서 게시 결과를 직접 확인해 주세요."
            ),
            "retryable": False,
        }
        if current is not None:
            detail.update(remote_media_result(current))
        return HTTPException(status_code=409, detail=detail)

    def require_remote_publish_lease(
        job_id: str,
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
    ) -> None:
        if not store.renew_remote_publish_lease(
            job_id,
            lease_owner,
            lease_fence,
        ):
            raise remote_media_publish_conflict(job_id, store)

    def checkpoint_remote_publish(
        job_id: str,
        expected_stage: str,
        next_stage: str,
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
        **fields: Any,
    ) -> None:
        if not store.checkpoint_publish(
            job_id,
            expected_stage,
            next_stage,
            lease_owner=lease_owner,
            lease_fence=lease_fence,
            **fields,
        ):
            raise remote_media_publish_conflict(job_id, store)

    def fail_remote_publish(
        job_id: str,
        expected_stage: str,
        *,
        resume_stage: str,
        error: str,
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
    ) -> None:
        if not store.fail_publish(
            job_id,
            expected_stage,
            resume_stage=resume_stage,
            error=error,
            lease_owner=lease_owner,
            lease_fence=lease_fence,
        ):
            raise remote_media_publish_conflict(job_id, store)

    def seal_remote_publish_outcome(
        job_id: str,
        expected_stage: str,
        *,
        error: str,
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
        **fields: Any,
    ) -> None:
        if not store.mark_publish_outcome_unknown(
            job_id,
            expected_stage,
            error=error,
            lease_owner=lease_owner,
            lease_fence=lease_fence,
            **fields,
        ):
            raise remote_media_publish_conflict(job_id, store)

    def create_remote_media_container(
        *,
        client: ThreadsApiClient,
        profile: dict[str, Any],
        job: dict[str, Any],
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
    ) -> str:
        mode = str(job.get("publish_locked_media_mode") or "")
        media_urls = [str(url) for url in job.get("publish_media_urls") or []]
        common = {
            "threads_user_id": profile["threads_user_id"],
            "access_token": profile["access_token"],
        }
        if mode == "video":
            require_remote_publish_lease(
                job["id"], store, lease_owner, lease_fence
            )
            return client._create_container(
                **common,
                data={
                    "media_type": "VIDEO",
                    "video_url": media_urls[0],
                    "text": str(job["publish_locked_body"]),
                },
            )
        child_ids = list(job.get("publish_child_container_ids") or [])
        child_media = (
            [("VIDEO", "video_url", media_urls[0]), ("IMAGE", "image_url", media_urls[1])]
            if mode == "mixed"
            else [("IMAGE", "image_url", url) for url in media_urls]
        )
        for media_type, url_field, media_url in child_media[len(child_ids) :]:
            require_remote_publish_lease(
                job["id"], store, lease_owner, lease_fence
            )
            child_ids.append(
                client._create_container(
                    **common,
                    data={
                    "media_type": media_type,
                    url_field: media_url,
                        "is_carousel_item": "true",
                    },
                )
            )
            checkpoint_remote_publish(
                job["id"],
                "creating_container",
                "creating_container",
                store,
                lease_owner,
                lease_fence,
                child_container_ids=child_ids,
            )
        require_remote_publish_lease(job["id"], store, lease_owner, lease_fence)
        return client._create_container(
            **common,
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "text": str(job["publish_locked_body"]),
            },
        )

    def run_remote_media_publish(
        job_id: str,
        store: WorkbenchStore,
        lease_owner: str,
        lease_fence: int,
    ) -> dict[str, Any]:
        while True:
            job = store.get_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Remote publish job not found")
            stage = str(job.get("publish_stage") or "draft")
            if stage == "published":
                return remote_media_result(job)
            require_remote_publish_lease(job_id, store, lease_owner, lease_fence)
            profile = store.get_threads_profile(
                str(job.get("publish_locked_profile_key") or ""),
                include_token=True,
            )
            if profile is None:
                raise HTTPException(status_code=404, detail="Threads profile not found")
            if not profile.get("is_connected"):
                raise HTTPException(status_code=400, detail="Threads profile is not connected")
            if str(profile.get("threads_user_id") or "") != str(
                job.get("publish_locked_threads_user_id") or ""
            ):
                raise HTTPException(
                    status_code=409,
                    detail="잠금 이후 Threads 계정이 변경되었습니다. 새 게시를 시작해 주세요.",
                )
            client = get_threads_client()
            if stage == "draft":
                checkpoint_remote_publish(
                    job_id,
                    "draft",
                    "creating_container",
                    store,
                    lease_owner,
                    lease_fence,
                )
                continue
            if stage == "creating_container":
                container_id = str(job.get("publish_container_id") or "").strip()
                try:
                    if not container_id:
                        container_id = create_remote_media_container(
                            client=client,
                            profile=profile,
                            job=job,
                            store=store,
                            lease_owner=lease_owner,
                            lease_fence=lease_fence,
                        )
                    if not container_id:
                        raise ThreadsApiError(
                            "Threads container response did not include an id"
                        )
                except ThreadsApiError as exc:
                    fail_remote_publish(
                        job_id,
                        "creating_container",
                        resume_stage="creating_container",
                        error=str(exc),
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    raise HTTPException(status_code=502, detail=str(exc)) from None
                checkpoint_remote_publish(
                    job_id,
                    "creating_container",
                    "container_created",
                    store,
                    lease_owner,
                    lease_fence,
                    container_id=container_id,
                )
                continue
            if stage == "container_created":
                try:
                    require_remote_publish_lease(
                        job_id, store, lease_owner, lease_fence
                    )
                    client.wait_for_container(
                        str(job.get("publish_container_id") or ""),
                        profile["access_token"],
                    )
                except ThreadsApiError as exc:
                    fail_remote_publish(
                        job_id,
                        "container_created",
                        resume_stage="container_created",
                        error=str(exc),
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    raise HTTPException(status_code=502, detail=str(exc)) from None
                checkpoint_remote_publish(
                    job_id,
                    "container_created",
                    "container_ready",
                    store,
                    lease_owner,
                    lease_fence,
                )
                continue
            if stage == "container_ready":
                checkpoint_remote_publish(
                    job_id,
                    "container_ready",
                    "publishing_main",
                    store,
                    lease_owner,
                    lease_fence,
                )
                continue
            if stage == "publishing_main":
                post_id = str(job.get("threads_post_id") or "").strip()
                if post_id:
                    checkpoint_remote_publish(
                        job_id,
                        "publishing_main",
                        "main_published",
                        store,
                        lease_owner,
                        lease_fence,
                        threads_post_id=post_id,
                        threads_profile_key=str(job["publish_locked_profile_key"]),
                        threads_published_at=str(
                            job.get("threads_published_at") or utc_now()
                        ),
                        status="THREADS_PUBLISHED",
                    )
                    continue
                checkpoint_remote_publish(
                    job_id,
                    "publishing_main",
                    "publishing_main_inflight",
                    store,
                    lease_owner,
                    lease_fence,
                )
                try:
                    require_remote_publish_lease(
                        job_id, store, lease_owner, lease_fence
                    )
                    published = client.publish_creation(
                        threads_user_id=profile["threads_user_id"],
                        access_token=profile["access_token"],
                        creation_id=str(job.get("publish_container_id") or ""),
                    )
                except ThreadsApiError as exc:
                    if exc.outcome_unknown:
                        seal_remote_publish_outcome(
                            job_id,
                            "publishing_main_inflight",
                            error=str(exc),
                            store=store,
                            lease_owner=lease_owner,
                            lease_fence=lease_fence,
                        )
                        raise remote_media_outcome_unknown(job_id, store) from None
                    fail_remote_publish(
                        job_id,
                        "publishing_main_inflight",
                        resume_stage="publishing_main",
                        error=str(exc),
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    raise HTTPException(status_code=502, detail=str(exc)) from None
                post_id = str(published.get("id") or "").strip()
                if not post_id:
                    seal_remote_publish_outcome(
                        job_id,
                        "publishing_main_inflight",
                        error="Threads publish response did not include an id",
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    raise remote_media_outcome_unknown(job_id, store)
                checkpoint_remote_publish(
                    job_id,
                    "publishing_main_inflight",
                    "main_published",
                    store,
                    lease_owner,
                    lease_fence,
                    threads_post_id=post_id,
                    threads_profile_key=str(job["publish_locked_profile_key"]),
                    threads_published_at=utc_now(),
                    status="THREADS_PUBLISHED",
                )
                permalink = fetch_threads_permalink(
                    client,
                    post_id,
                    profile["access_token"],
                    job_id,
                    store,
                )
                if permalink:
                    checkpoint_remote_publish(
                        job_id,
                        "main_published",
                        "main_published",
                        store,
                        lease_owner,
                        lease_fence,
                        threads_permalink=permalink,
                    )
                continue
            if stage == "publishing_main_inflight":
                seal_remote_publish_outcome(
                    job_id,
                    "publishing_main_inflight",
                    error=(
                        "Threads main publish stopped before its outcome was recorded"
                    ),
                    store=store,
                    lease_owner=lease_owner,
                    lease_fence=lease_fence,
                )
                raise remote_media_outcome_unknown(job_id, store)
            if stage == "main_published":
                checkpoint_remote_publish(
                    job_id,
                    "main_published",
                    "publishing_reply",
                    store,
                    lease_owner,
                    lease_fence,
                )
                continue
            if stage == "publishing_reply":
                if job.get("threads_reply_id"):
                    checkpoint_remote_publish(
                        job_id,
                        "publishing_reply",
                        "published",
                        store,
                        lease_owner,
                        lease_fence,
                    )
                    continue
                checkpoint_remote_publish(
                    job_id,
                    "publishing_reply",
                    "publishing_reply_inflight",
                    store,
                    lease_owner,
                    lease_fence,
                )
                try:
                    require_remote_publish_lease(
                        job_id, store, lease_owner, lease_fence
                    )
                    reply = client.publish_reply(
                        threads_user_id=profile["threads_user_id"],
                        access_token=profile["access_token"],
                        text=str(job["publish_locked_comment"]),
                        reply_to_id=str(job["threads_post_id"]),
                    )
                except ThreadsApiError as exc:
                    if exc.outcome_unknown:
                        seal_remote_publish_outcome(
                            job_id,
                            "publishing_reply_inflight",
                            error=str(exc),
                            store=store,
                            lease_owner=lease_owner,
                            lease_fence=lease_fence,
                        )
                        raise remote_media_outcome_unknown(job_id, store) from None
                    fail_remote_publish(
                        job_id,
                        "publishing_reply_inflight",
                        resume_stage="publishing_reply",
                        error=str(exc),
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    partial = store.get_job(job_id)
                    if partial is None:
                        raise HTTPException(
                            status_code=404,
                            detail="Remote publish job not found",
                        )
                    return remote_media_result(partial)
                reply_id = str(reply.get("id") or "").strip()
                if not reply_id:
                    seal_remote_publish_outcome(
                        job_id,
                        "publishing_reply_inflight",
                        error="Threads reply response did not include an id",
                        store=store,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                    raise remote_media_outcome_unknown(job_id, store)
                checkpoint_remote_publish(
                    job_id,
                    "publishing_reply_inflight",
                    "published",
                    store,
                    lease_owner,
                    lease_fence,
                    threads_reply_id=reply_id,
                    status="THREADS_PUBLISHED",
                )
                continue
            if stage == "publishing_reply_inflight":
                seal_remote_publish_outcome(
                    job_id,
                    "publishing_reply_inflight",
                    error=(
                        "Threads reply publish stopped before its outcome was recorded"
                    ),
                    store=store,
                    lease_owner=lease_owner,
                    lease_fence=lease_fence,
                )
                raise remote_media_outcome_unknown(job_id, store)
            if stage == "failed":
                try:
                    store.resume_failed_publish(
                        job_id,
                        lease_owner=lease_owner,
                        lease_fence=lease_fence,
                    )
                except PermissionError:
                    raise remote_media_publish_conflict(job_id, store) from None
                continue
            if stage == "outcome_unknown":
                raise remote_media_outcome_unknown(job_id, store)
            raise HTTPException(status_code=409, detail=f"Unsupported publish stage: {stage}")

    def refresh_threads_record_insights(job_id: str, store: WorkbenchStore) -> dict[str, Any]:
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
            insights = get_threads_client().fetch_media_insights(post_id, profile["access_token"])
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
                refreshed_records.append(refresh_threads_record_insights(job_id, store))
            except HTTPException:
                current_record = store.get_threads_publish_record(job_id)
                refreshed_records.append(current_record or record)
        return refreshed_records

    def refresh_threads_record_permalink(job_id: str, store: WorkbenchStore) -> dict[str, Any]:
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
        permalink = fetch_threads_permalink(get_threads_client(), post_id, profile["access_token"], job_id, store)
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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "threads-api"}

    @app.post("/api/threads/media-uploads")
    def upload_temporary_media(
        payload: ThreadsMediaUploadPayload,
        request: Request,
    ) -> dict[str, Any]:
        require_bridge_access(request)
        try:
            metadata = media_store.create(decode_media_payload(payload))
        except MediaUploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        media_id = metadata["media_id"]
        return {
            "media_id": media_id,
            "public_url": f"{public_base_url(request)}/api/threads/media/{media_id}",
            "expires_at": metadata["expires_at"],
            "mime_type": metadata["mime_type"],
            "size": metadata["size"],
        }

    @app.post("/api/threads/media-uploads/start")
    def start_temporary_media_upload(
        payload: ThreadsMediaUploadStartPayload,
        request: Request,
    ) -> dict[str, str]:
        require_bridge_access(request)
        try:
            return {"upload_id": media_store.start_upload(payload.total_bytes)}
        except MediaUploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/threads/media-uploads/{upload_id}/parts")
    def append_temporary_media_upload_part(
        upload_id: str,
        payload: ThreadsMediaUploadPartPayload,
        request: Request,
    ) -> dict[str, bool]:
        require_bridge_access(request)
        try:
            media_store.append_upload_part(upload_id, payload.index, decode_media_payload(payload))
        except MediaUploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"received": True}

    @app.post("/api/threads/media-uploads/{upload_id}/complete")
    def complete_temporary_media_upload(upload_id: str, request: Request) -> dict[str, Any]:
        require_bridge_access(request)
        try:
            metadata = media_store.complete_upload(upload_id)
        except MediaUploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        media_id = metadata["media_id"]
        return {
            "media_id": media_id,
            "public_url": f"{public_base_url(request)}/api/threads/media/{media_id}",
            "expires_at": metadata["expires_at"],
            "mime_type": metadata["mime_type"],
            "size": metadata["size"],
        }

    @app.api_route("/api/threads/media/{media_id}", methods=["GET", "HEAD"])
    def get_temporary_media(media_id: str, request: Request) -> Response:
        metadata = media_store.get(media_id)
        if metadata is None:
            raise HTTPException(status_code=404, detail="Temporary media not found")
        mime_type = metadata["mime_type"]
        size = int(metadata["size"])
        headers = {
            "Content-Length": str(size),
            "X-Content-Type-Options": "nosniff",
        }
        range_header = request.headers.get("Range", "").strip()
        if mime_type == "video/mp4":
            headers["Accept-Ranges"] = "bytes"
        if range_header and mime_type == "video/mp4":
            try:
                start, end = _parse_byte_range(range_header, size)
            except ValueError:
                raise HTTPException(
                    status_code=416,
                    detail="Requested range is not satisfiable",
                    headers={"Content-Range": f"bytes */{size}"},
                ) from None
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
            content = b""
            if request.method != "HEAD":
                try:
                    content = media_store.read(media_id)[start : end + 1]
                except MediaUploadError:
                    raise HTTPException(status_code=404, detail="Temporary media not found") from None
            return Response(
                content=content,
                status_code=206,
                media_type=mime_type,
                headers=headers,
            )
        content = b""
        if request.method != "HEAD":
            try:
                content = media_store.read(media_id)
            except MediaUploadError:
                raise HTTPException(status_code=404, detail="Temporary media not found") from None
        return Response(content=content, media_type=mime_type, headers=headers)

    @app.delete("/api/threads/media/{media_id}")
    def delete_temporary_media(media_id: str, request: Request) -> dict[str, Any]:
        require_bridge_access(request)
        if not media_store.delete(media_id):
            raise HTTPException(status_code=404, detail="Temporary media not found")
        return {"deleted": True, "media_id": media_id}

    @app.get("/api/threads/profiles")
    def list_threads_profiles(
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        require_bridge_access(request)
        return store.list_threads_profiles()

    @app.get("/api/threads/publish-records")
    def list_threads_publish_records(
        request: Request,
        refresh_insights: bool = False,
        store: WorkbenchStore = Depends(get_store),
    ) -> list[dict[str, Any]]:
        require_bridge_access(request)
        return list_threads_publish_records_with_optional_insights(
            refresh_insights=refresh_insights,
            store=store,
        )

    @app.post("/api/threads/publish-records/{job_id}/insights")
    def refresh_threads_publish_record_insights(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
        return refresh_threads_record_insights(job_id, store)

    @app.post("/api/threads/publish-records/{job_id}/permalink")
    def refresh_threads_publish_record_permalink(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
        return refresh_threads_record_permalink(job_id, store)

    @app.delete("/api/threads/publish-records/{job_id}")
    def delete_threads_publish_record(
        job_id: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
        return delete_threads_record(job_id, store)

    @app.post("/api/threads/profiles")
    def upsert_threads_profile(
        payload: ThreadsProfilePayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
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
        require_bridge_access(request)
        clean_profile_key = profile_key.strip()
        profile = store.get_threads_profile(clean_profile_key)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        state = store.issue_threads_oauth_state(profile_key=clean_profile_key)
        return {"auth_url": get_threads_client().build_authorization_url(state)}

    @app.get("/api/threads/auth/import/start")
    def start_threads_profile_import(
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        require_bridge_access(request)
        state = store.issue_threads_oauth_state(profile_key=None)
        return {"auth_url": get_threads_client().build_authorization_url(state)}

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
        client = get_threads_client()
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
            <p>이 창을 닫고 로컬 화면으로 돌아가도 됩니다.</p>
          </body>
        </html>
        """

    @app.post("/api/threads/remote-publish")
    def publish_remote_threads_post(
        payload: ThreadsRemotePublishPayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
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

    @app.post("/api/threads/remote-media-publish")
    def publish_remote_threads_media(
        payload: ThreadsRemoteMediaPublishPayload,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
        profile = store.get_threads_profile(payload.profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        existing_publish = store.get_publish_job_by_idempotency_key(
            payload.idempotency_key
        )
        if existing_publish and str(
            existing_publish.get("publish_locked_threads_user_id") or ""
        ) != str(profile.get("threads_user_id") or ""):
            raise HTTPException(
                status_code=409,
                detail="잠금 이후 Threads 계정이 변경되었습니다. 새 게시를 시작해 주세요.",
            )
        try:
            job, _created = store.get_or_create_remote_media_publish(
                idempotency_key=payload.idempotency_key,
                profile_key=payload.profile_key,
                threads_user_id=str(profile.get("threads_user_id") or ""),
                product_url=payload.product_url,
                product_name=payload.product_name,
                body=payload.text,
                comment_text=payload.comment_text,
                media_mode=payload.media_mode,
                media_urls=[str(url) for url in payload.media_urls],
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if job.get("publish_stage") == "published":
            return remote_media_result(job)
        lease_owner = f"lease-{token_urlsafe(24)}"
        lease_fence = store.acquire_remote_publish_lease(job["id"], lease_owner)
        if lease_fence is None:
            raise remote_media_publish_conflict(job["id"], store)
        try:
            return run_remote_media_publish(
                job["id"],
                store,
                lease_owner,
                lease_fence,
            )
        finally:
            store.release_remote_publish_lease(
                job["id"],
                lease_owner,
                lease_fence,
            )

    @app.post("/api/threads/profiles/{profile_key}/refresh")
    def refresh_threads_profile_token(
        profile_key: str,
        request: Request,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        require_bridge_access(request)
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        try:
            refreshed = get_threads_client().refresh_long_lived_token(profile["access_token"])
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
        require_bridge_access(request)
        disconnected = store.disconnect_threads_profile(profile_key)
        if disconnected is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        return disconnected

    return app


def _parse_byte_range(range_header: str, size: int) -> tuple[int, int]:
    if size <= 0 or not range_header.startswith("bytes="):
        raise ValueError("invalid byte range")
    specification = range_header.removeprefix("bytes=").strip()
    if not specification or "," in specification or "-" not in specification:
        raise ValueError("invalid byte range")
    raw_start, raw_end = specification.split("-", 1)
    if not raw_start:
        suffix_length = int(raw_end)
        if suffix_length <= 0:
            raise ValueError("invalid byte range")
        start = max(0, size - suffix_length)
        return start, size - 1
    start = int(raw_start)
    if start < 0 or start >= size:
        raise ValueError("invalid byte range")
    end = size - 1 if not raw_end else int(raw_end)
    if end < start:
        raise ValueError("invalid byte range")
    return start, min(end, size - 1)


app = create_threads_api_app()
