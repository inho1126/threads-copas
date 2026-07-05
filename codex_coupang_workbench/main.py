from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .coupang_partners import (
    CoupangPartnerProduct,
    CoupangPartnersError,
    extract_coupang_ids,
    fetch_partner_product_context,
)
from .codex_threads import CodexThreadsError, DEFAULT_CODEX_MODEL, generate_codex_threads_post
from .naver import publish_handoff_message
from .product_research import fetch_best_product_context
from .schemas import (
    GeneratedImagePayload,
    JobCreatePayload,
    MediaCandidatePayload,
    CoupangProductPreviewPayload,
    PublishHandoff,
    SettingsPayload,
    ThreadsDraftPayload,
    ThreadsProfilePayload,
    ThreadsPublishPayload,
)
from .storage import WorkbenchStore
from .threads import ThreadsApiClient, ThreadsApiError
from .writer import generate_campaign, generate_draft, generate_threads_comment, generate_threads_post

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PACKAGE_DIR.parent / "workbench_data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "workbench.sqlite3"
STATIC_DIR = PACKAGE_DIR / "static"
ICON_PATH = PACKAGE_DIR.parent / "assets" / "appicon.ico"
THREADS_IMPORT_STATE_PREFIX = "import-current-profile:"
SECRET_SETTING_KEYS = {"coupang_secret_key", "threads_app_secret", "openai_api_key"}
SECRET_MASK = "********"


def public_settings(settings: dict[str, str]) -> dict[str, str]:
    visible = dict(settings)
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


def fetch_coupang_partner_product(
    product_url: str,
    settings: dict[str, str],
) -> tuple[CoupangPartnerProduct, str]:
    access_key = settings.get("coupang_access_key", "").strip()
    secret_key = settings.get("coupang_secret_key", "").strip()
    if not access_key or not secret_key:
        raise CoupangPartnersError("쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요.")
    partner_product, resolved_url = fetch_partner_product_context(
        product_url,
        access_key=access_key,
        secret_key=secret_key,
        sub_id=settings.get("coupang_sub_id", ""),
    )
    return partner_product, resolved_url


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
) -> dict[str, Any]:
    product_ids = extract_coupang_ids(resolved_url) + extract_coupang_ids(original_url)
    product_id = product.product_id or (product_ids[0] if product_ids else "")
    return {
        "product_name": product.product_name,
        "product_id": product_id,
        "item_id": product_ids[1] if len(product_ids) > 1 else "",
        "image_url": product.image_url,
        "partner_url": product.partner_url,
        "product_url": product.product_url,
        "resolved_url": resolved_url,
        "original_url": original_url,
        "facts": list(product.facts),
        "needs_product_name": not bool(product.product_name),
    }


def create_app(db_path: str | Path = DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(title="Codex Coupang Workbench")
    store = WorkbenchStore(db_path)

    def get_store() -> WorkbenchStore:
        return store

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
        return {"status": "ok"}

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
        return store.list_jobs()

    @app.post("/api/coupang/product-preview")
    def preview_coupang_product(
        payload: CoupangProductPreviewPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        product_url = payload.product_url.strip()
        try:
            product, resolved_url = fetch_coupang_partner_product(product_url, store.get_settings())
        except CoupangPartnersError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not product.product_name and not product.partner_url:
            raise HTTPException(status_code=400, detail="쿠팡 파트너스 API에서 상품 정보를 찾지 못했습니다.") from None
        return product_preview_response(
            product,
            original_url=product_url,
            resolved_url=resolved_url or product_url,
        )

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
            product_context = fetch_best_product_context(payload.product_url, product_name)
            product_name = product_name or product_context.page_title
            image_url = image_url or product_context.image_url
        return store.add_job(
            product_url=payload.product_url,
            product_name=product_name or "상품명 자동 확인 필요",
            image_url=image_url,
            memo=payload.memo,
        )

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
        return store.update_job_draft(
            job_id,
            title=draft.title,
            draft=draft.body,
            tags=draft.tags,
        )

    @app.post("/api/jobs/{job_id}/campaign")
    def campaign_job(job_id: str, store: WorkbenchStore = Depends(get_store)) -> dict[str, Any]:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        settings = store.get_settings()
        product_context = fetch_best_product_context(job["product_url"], job["product_name"])
        if not (product_context.facts or product_context.description.strip()):
            known_campaign = store.get_known_campaign_context(job["product_url"])
            if known_campaign:
                return store.update_job_campaign(
                    job_id,
                    sns_draft=known_campaign["sns_draft"],
                    image_brief=known_campaign["image_brief"],
                    blog_final=known_campaign["blog_final"],
                    sns_final=known_campaign["sns_final"],
                    title=known_campaign["title"],
                    tags=known_campaign["tags"],
                    image_url=job.get("image_url", "").strip() or known_campaign.get("image_url", ""),
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
        return store.update_job_campaign(
            job_id,
            sns_draft=campaign.sns_draft,
            image_brief=campaign.image_brief,
            blog_final=campaign.blog_final,
            sns_final=campaign.sns_final,
            title=campaign.title,
            tags=campaign.tags,
            image_url=reference_image_url if reference_image_url else None,
        )

    @app.patch("/api/jobs/{job_id}/generated-image")
    def update_generated_image(
        job_id: str,
        payload: GeneratedImagePayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        try:
            return store.update_job_generated_image(job_id, payload.generated_image_url)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None

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
    def list_threads_profiles(store: WorkbenchStore = Depends(get_store)) -> list[dict[str, Any]]:
        return store.list_threads_profiles()

    @app.get("/api/threads/publish-records")
    def list_threads_publish_records(store: WorkbenchStore = Depends(get_store)) -> list[dict[str, Any]]:
        return store.list_threads_publish_records()

    @app.post("/api/threads/profiles")
    def upsert_threads_profile(
        payload: ThreadsProfilePayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
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
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        profile = store.get_threads_profile(profile_key)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        client = get_threads_client(store.get_settings())
        return {"auth_url": client.build_authorization_url(profile_key.strip())}

    @app.get("/api/threads/auth/import/start")
    def start_threads_profile_import(
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, str]:
        client = get_threads_client(store.get_settings())
        state = f"{THREADS_IMPORT_STATE_PREFIX}{uuid4().hex}"
        return {"auth_url": client.build_authorization_url(state)}

    @app.get("/api/threads/auth/callback", response_class=HTMLResponse)
    def threads_auth_callback(
        code: str,
        state: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> str:
        callback_state = state.strip()
        is_import = callback_state.startswith(THREADS_IMPORT_STATE_PREFIX)
        if not callback_state:
            raise HTTPException(status_code=400, detail="Missing profile state")
        if not is_import and store.get_threads_profile(callback_state) is None:
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
        profile_key = callback_state
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
        product_name = payload.product_name.strip()
        image_url = payload.image_url.strip()
        product_url = payload.product_url.strip()
        product_context = None
        partner_url = payload.partner_url.strip()
        api_error = ""
        if settings.get("coupang_access_key", "").strip() and settings.get("coupang_secret_key", "").strip():
            try:
                partner_product, resolved_url = fetch_coupang_partner_product(product_url, settings)
                product_context = partner_product.to_product_context(
                    source_url=product_url,
                    resolved_url=resolved_url or product_url,
                )
                product_name = product_name or partner_product.product_name
                image_url = image_url or partner_product.image_url
                partner_url = partner_url or partner_product.partner_url
            except CoupangPartnersError as exc:
                api_error = str(exc)
        if product_context is None:
            product_context = fetch_best_product_context(product_url, product_name)
        if not product_name:
            known_context = store.get_known_product_context(product_url)
            product_name = known_context.get("product_name", "") or product_context.page_title
        if not product_name:
            detail = (
                "쿠팡 파트너스 API에서 상품 정보를 찾지 못했습니다."
                if settings.get("coupang_access_key", "").strip() and settings.get("coupang_secret_key", "").strip()
                else "쿠팡 파트너스 API 키를 저장한 뒤 다시 시도해 주세요."
            )
            if api_error:
                detail = f"{detail} ({api_error})"
            raise HTTPException(
                status_code=400,
                detail=detail,
            )
        if not image_url:
            image_url = product_context.image_url
        final_product_url = partner_url or product_url
        job = store.add_job(
            product_url=final_product_url,
            product_name=product_name or "상품명 자동 확인 필요",
            image_url=image_url,
            memo=payload.memo,
        )
        threads_text = generate_threads_post(
            product_name=job["product_name"],
            product_url=job["product_url"],
            product_facts=product_context.facts or [],
            memo=payload.memo,
            persona=settings.get("writer_persona", ""),
        )
        comment_text = generate_threads_comment(job["product_url"])
        try:
            threads_text = generate_codex_threads_post(
                model=settings.get("codex_model", "").strip() or DEFAULT_CODEX_MODEL,
                product_name=job["product_name"],
                product_url=job["product_url"],
                product_facts=product_context.facts or [],
                memo=payload.memo,
                persona=settings.get("writer_persona", ""),
            )
        except CodexThreadsError:
            pass
        updated_job = store.update_job_threads_draft(
            job["id"],
            text=threads_text,
            comment_text=comment_text,
            title=f"{job['product_name']} Threads",
            tags=["쿠팡파트너스", "Threads"],
            image_url=image_url or None,
        )
        return {"job": updated_job, "text": threads_text, "comment_text": comment_text}

    @app.post("/api/threads/publish")
    def publish_threads_post(
        payload: ThreadsPublishPayload,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        if store.get_job(payload.job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        profile = store.get_threads_profile(payload.profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        client = get_threads_client(store.get_settings())
        try:
            published = client.publish_text(
                threads_user_id=profile["threads_user_id"],
                access_token=profile["access_token"],
                text=payload.text,
            )
        except ThreadsApiError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        post_id = str(published.get("id", "")).strip()
        if not post_id:
            raise HTTPException(status_code=400, detail="Threads publish response did not include an id")
        comment_text = payload.comment_text.strip()
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
                raise HTTPException(status_code=400, detail=str(exc)) from None
            reply_id = str(reply.get("id", "")).strip()
        updated_job = store.mark_threads_published(
            job_id=payload.job_id,
            profile_key=payload.profile_key,
            threads_post_id=post_id,
            threads_reply_id=reply_id,
            published_text=f"본문:\n{payload.text.strip()}\n\n댓글:\n{comment_text}" if comment_text else payload.text,
        )
        return {
            "status": "THREADS_PUBLISHED",
            "threads_post_id": post_id,
            "threads_reply_id": reply_id,
            "job": updated_job,
        }

    @app.post("/api/threads/profiles/{profile_key}/refresh")
    def refresh_threads_profile_token(
        profile_key: str,
        store: WorkbenchStore = Depends(get_store),
    ) -> dict[str, Any]:
        profile = store.get_threads_profile(profile_key, include_token=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Threads profile not found")
        if not profile.get("is_connected"):
            raise HTTPException(status_code=400, detail="Threads profile is not connected")
        client = get_threads_client(store.get_settings())
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

    @app.get("/api/logs")
    def list_logs(store: WorkbenchStore = Depends(get_store)) -> list[dict[str, Any]]:
        return store.list_logs()

    return app


app = create_app()
