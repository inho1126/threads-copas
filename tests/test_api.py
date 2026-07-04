import pytest
from httpx import ASGITransport, AsyncClient

from codex_coupang_workbench.codex_threads import CodexThreadsError
from codex_coupang_workbench.coupang_partners import CoupangPartnerProduct
from codex_coupang_workbench.main import create_app
from codex_coupang_workbench.product_research import ProductContext


class FakeThreadsClient:
    published = []
    replies = []

    def __init__(self, app_id, app_secret, redirect_uri):
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri

    def build_authorization_url(self, state):
        return f"https://threads.net/oauth/authorize?client_id={self.app_id}&state={state}"

    def exchange_code_for_short_token(self, code):
        assert code == "oauth-code"
        return {"access_token": "short-token", "user_id": "12345"}

    def exchange_for_long_lived_token(self, short_lived_token):
        assert short_lived_token == "short-token"
        return {"access_token": "long-token", "expires_in": 5_184_000}

    def fetch_me(self, access_token):
        assert access_token == "long-token"
        return {"id": "12345", "username": "tesla_daily", "name": "Tesla Daily"}

    def refresh_long_lived_token(self, access_token):
        assert access_token == "long-token"
        return {"access_token": "refreshed-token", "expires_in": 5_184_000}

    def publish_text(self, threads_user_id, access_token, text):
        self.published.append(
            {
                "threads_user_id": threads_user_id,
                "access_token": access_token,
                "text": text,
            }
        )
        return {"id": "post_123"}

    def publish_reply(self, threads_user_id, access_token, text, reply_to_id):
        self.replies.append(
            {
                "threads_user_id": threads_user_id,
                "access_token": access_token,
                "text": text,
                "reply_to_id": reply_to_id,
            }
        )
        return {"id": "reply_123"}


@pytest.mark.anyio
async def test_api_create_job_and_generate_draft(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/health")).json()["status"] == "ok"

        settings_response = await client.put(
            "/api/settings",
            json={
                "naver_blog_id": "myblog",
                "coupang_sub_id": "sub-1",
                "writer_persona": "꼼꼼한 리뷰어",
            },
        )
        assert settings_response.status_code == 200

        create_response = await client.post(
            "/api/jobs",
            json={
                "product_url": "https://link.coupang.com/a/example",
                "product_name": "테스트 상품",
                "memo": "생활용품",
                "image_url": "https://image.example/item.jpg",
            },
        )
        assert create_response.status_code == 200
        job_id = create_response.json()["id"]
        assert create_response.json()["image_url"] == "https://image.example/item.jpg"

        draft_response = await client.post(f"/api/jobs/{job_id}/draft")
        assert draft_response.status_code == 200
        assert draft_response.json()["status"] == "DRAFTED"
        assert "쿠팡 파트너스" in draft_response.json()["draft"]
        assert "작성 톤:" not in draft_response.json()["draft"]
        assert "상품 링크:" not in draft_response.json()["draft"]
        assert "![테스트 상품](https://image.example/item.jpg)" in draft_response.json()["draft"]

        jobs = (await client.get("/api/jobs")).json()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job_id


@pytest.mark.anyio
async def test_api_settings_redacts_and_preserves_secrets(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.put(
            "/api/settings",
            json={
                "threads_app_id": "app-id",
                "threads_app_secret": "super-secret",
                "threads_redirect_uri": "http://test/callback",
                "coupang_secret_key": "coupang-secret",
                "codex_model": "gpt-5.5",
            },
        )
        assert first.status_code == 200
        assert first.json()["threads_app_secret"] == "********"
        assert first.json()["coupang_secret_key"] == "********"
        assert first.json()["codex_model"] == "gpt-5.5"

        second = await client.put(
            "/api/settings",
            json={
                "threads_app_id": "app-id",
                "threads_redirect_uri": "http://test/next-callback",
            },
        )
        assert second.status_code == 200
        assert second.json()["threads_app_secret"] == "********"

        settings = await client.get("/api/settings")
        assert settings.json()["threads_app_secret"] == "********"
        assert settings.json()["coupang_secret_key"] == "********"
        assert settings.json()["codex_model"] == "gpt-5.5"

        import_start = await client.get("/api/threads/auth/import/start")
        assert import_start.status_code == 200


@pytest.mark.anyio
async def test_threads_draft_prefers_codex_auth_generation(tmp_path, monkeypatch):
    calls = []

    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(
            source_url=url,
            resolved_url="https://www.coupang.com/vp/products/example",
            page_title="테슬라 센터 콘솔 수납함",
            facts=["모델Y 주니퍼 호환", "센터 콘솔 수납 트레이"],
        ),
    )

    def fake_generate_codex_threads_post(**kwargs):
        calls.append(kwargs)
        return "왜 테슬라 콘솔 정리는 차를 타고 나서야 신경 쓰이기 시작할까요?\n\n작은 물건이 자꾸 굴러다닌다면 한 번 볼 만한 수납함입니다.\n\n#테슬라용품"

    monkeypatch.setattr("codex_coupang_workbench.main.generate_codex_threads_post", fake_generate_codex_threads_post)
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/settings",
            json={
                "codex_model": "gpt-5.5",
            },
        )

        draft = await client.post(
            "/api/threads/draft",
            json={"profile_key": "tesla", "product_url": "https://link.coupang.com/a/example"},
        )

        assert draft.status_code == 200
        assert draft.json()["text"].startswith("왜 테슬라 콘솔 정리는")
        assert "쿠팡 파트너스" not in draft.json()["text"]
        assert "https://link.coupang.com/a/example" not in draft.json()["text"]
        assert "쿠팡 파트너스" in draft.json()["comment_text"]
        assert "https://link.coupang.com/a/example" in draft.json()["comment_text"]
        assert calls[0]["model"] == "gpt-5.5"
        assert calls[0]["product_name"] == "테슬라 센터 콘솔 수납함"
        assert "모델Y 주니퍼 호환" in calls[0]["product_facts"]


@pytest.mark.anyio
async def test_threads_draft_uses_coupang_partner_context_and_deeplink(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_partner_product_context",
        lambda *args, **kwargs: (
            CoupangPartnerProduct(
                product_name="테슬라 콘솔 정리함",
                partner_url="https://link.coupang.com/a/partner",
                image_url="https://image.example/console.jpg",
                facts=("차량용품 카테고리 상품", "테슬라 관련 상품"),
                product_id="12345",
            ),
            "https://www.coupang.com/vp/products/12345",
        ),
    )
    monkeypatch.setattr(
        "codex_coupang_workbench.main.generate_codex_threads_post",
        lambda **kwargs: (_ for _ in ()).throw(CodexThreadsError("skip codex")),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/settings",
            json={
                "coupang_access_key": "access",
                "coupang_secret_key": "secret",
                "coupang_sub_id": "threads2026",
            },
        )
        response = await client.post(
            "/api/threads/draft",
            json={"profile_key": "tesla", "product_url": "https://link.coupang.com/a/original"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["job"]["product_name"] == "테슬라 콘솔 정리함"
        assert payload["job"]["product_url"] == "https://link.coupang.com/a/partner"
        assert "테슬라 콘솔 정리함" in payload["text"]
        assert "https://link.coupang.com/a/partner" in payload["comment_text"]


@pytest.mark.anyio
async def test_coupang_product_preview_returns_partner_product(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_partner_product_context",
        lambda *args, **kwargs: (
            CoupangPartnerProduct(
                product_name="무선 충전 거치대",
                product_url="https://www.coupang.com/vp/products/777",
                partner_url="https://link.coupang.com/a/preview",
                image_url="https://image.example/charger.jpg",
                facts=("디지털 카테고리 상품", "로켓배송 가능 여부 표시"),
                product_id="777",
            ),
            "https://www.coupang.com/vp/products/777",
        ),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/api/settings",
            json={
                "coupang_access_key": "access",
                "coupang_secret_key": "secret",
            },
        )
        response = await client.post(
            "/api/coupang/product-preview",
            json={"product_url": "https://link.coupang.com/a/original"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["product_name"] == "무선 충전 거치대"
        assert payload["image_url"] == "https://image.example/charger.jpg"
        assert payload["partner_url"] == "https://link.coupang.com/a/preview"
        assert payload["resolved_url"] == "https://www.coupang.com/vp/products/777"
        assert "디지털 카테고리 상품" in payload["facts"]


@pytest.mark.anyio
async def test_threads_draft_requires_coupang_api_when_product_context_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(source_url=url, resolved_url=url, facts=[]),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/threads/draft",
            json={"profile_key": "tesla", "product_url": "https://link.coupang.com/a/blocked"},
        )

        assert response.status_code == 400
        assert "쿠팡 파트너스 API 키" in response.text


@pytest.mark.anyio
async def test_api_create_job_can_infer_product_name_and_image_from_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(
            source_url=url,
            resolved_url="https://www.logitech.com/ko-kr/shop/p/mx-master-4",
            page_title="MX Master 4 무선 마우스 | Logitech",
            image_url="https://resource.logitech.com/content/dam/logitech/en/products/mice/mx-master-4/gallery/mx-master-4.png",
            facts=["햅틱 피드백", "MagSpeed 스크롤", "8K DPI"],
        ),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/jobs",
            json={
                "product_url": "https://link.coupang.com/a/example",
            },
        )

        assert response.status_code == 200
        job = response.json()
        assert job["product_name"] == "MX Master 4 무선 마우스 | Logitech"
        assert "products/mice/mx-master-4/gallery" in job["image_url"]


@pytest.mark.anyio
async def test_api_create_job_reuses_known_product_context_for_same_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(source_url=url, resolved_url=url, facts=[]),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/jobs",
            json={
                "product_url": "https://link.coupang.com/a/eAJ46gv5P2",
                "product_name": "로지텍 MX MASTER 4 무선 마우스",
                "image_url": "https://thumbnail.coupangcdn.com/product.jpg",
            },
        )
        assert first.status_code == 200

        second = await client.post(
            "/api/jobs",
            json={
                "product_url": "https://link.coupang.com/a/eAJ46gv5P2",
            },
        )

        assert second.status_code == 200
        job = second.json()
        assert job["product_name"] == "로지텍 MX MASTER 4 무선 마우스"
        assert job["image_url"] == "https://thumbnail.coupangcdn.com/product.jpg"


@pytest.mark.anyio
async def test_publish_endpoint_is_explicit_handoff(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/example",
                    "product_name": "테스트 상품",
                    "memo": "",
                },
            )
        ).json()

        response = await client.post(f"/api/jobs/{job['id']}/publish")

        assert response.status_code == 200
        assert response.json()["status"] == "NEEDS_BROWSER_REVIEW"
        assert "네이버" in response.json()["message"]


@pytest.mark.anyio
async def test_draft_does_not_create_placeholder_image_when_job_has_no_image(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/generated",
                    "product_name": "생성 이미지 상품",
                    "memo": "대표 이미지를 자동 생성해야 함",
                },
            )
        ).json()

        draft_response = await client.post(f"/api/jobs/{job['id']}/draft")

        assert draft_response.status_code == 200
        drafted = draft_response.json()
        assert drafted["image_url"] == ""
        assert "![" not in drafted["draft"]
        assert not (tmp_path / "generated").exists()


@pytest.mark.anyio
async def test_api_media_candidate_approval_feeds_draft_image(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/video",
                    "product_name": "키크론 키보드",
                    "memo": "영상 캡처 후보를 사용할 상품",
                },
            )
        ).json()

        create_media = await client.post(
            f"/api/jobs/{job['id']}/media",
            json={
                "source": "youtube",
                "source_url": "https://www.youtube.com/watch?v=abc123",
                "image_url": "https://image.example/keychron-frame.jpg",
                "timestamp_label": "02:10",
                "title": "키크론 키보드 타건 영상",
                "creator": "Keyboard Review",
                "notes": "자막 없이 제품 정면이 크게 보이는 구간",
                "no_captions": True,
                "no_tts": True,
                "product_visible": True,
                "permission_reviewed": True,
            },
        )
        assert create_media.status_code == 200
        candidate_id = create_media.json()["id"]

        list_media = await client.get(f"/api/jobs/{job['id']}/media")
        assert list_media.status_code == 200
        assert list_media.json()[0]["timestamp_label"] == "02:10"

        approve_media = await client.post(f"/api/media/{candidate_id}/approve")
        assert approve_media.status_code == 200
        assert approve_media.json()["review_status"] == "APPROVED"

        draft_response = await client.post(f"/api/jobs/{job['id']}/draft")
        assert draft_response.status_code == 200
        assert "![키크론 키보드](https://image.example/keychron-frame.jpg)" in draft_response.json()["draft"]


@pytest.mark.anyio
async def test_api_rejects_approval_without_candidate_image(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/video",
                    "product_name": "키보드",
                },
            )
        ).json()
        candidate = (
            await client.post(
                f"/api/jobs/{job['id']}/media",
                json={
                    "source": "youtube",
                    "source_url": "https://www.youtube.com/watch?v=abc123",
                    "title": "후보 영상",
                },
            )
        ).json()

        response = await client.post(f"/api/media/{candidate['id']}/approve")

        assert response.status_code == 400
        assert "image_url" in response.text


@pytest.mark.anyio
async def test_api_campaign_generation_and_generated_image_update(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(source_url=url, resolved_url=url, facts=[]),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/campaign",
                    "product_name": "무선 마사지건",
                    "memo": "운동 후 회복, 선물용, 강한 진동",
                    "image_url": "https://image.example/massage.jpg",
                },
            )
        ).json()

        campaign_response = await client.post(f"/api/jobs/{job['id']}/campaign")
        assert campaign_response.status_code == 200
        campaign = campaign_response.json()
        assert campaign["status"] == "CAMPAIGN_READY"
        assert "무선 마사지건" in campaign["sns_draft"]
        assert "imagegen" in campaign["image_brief"].lower()
        assert "실사" in campaign["image_brief"]
        assert "https://image.example/massage.jpg" in campaign["image_brief"]
        assert "쿠팡 파트너스" in campaign["blog_final"]
        assert "https://link.coupang.com/a/campaign" in campaign["blog_final"]

        image_response = await client.patch(
            f"/api/jobs/{job['id']}/generated-image",
            json={"generated_image_url": "https://image.example/generated-ad.jpg"},
        )
        assert image_response.status_code == 200
        assert image_response.json()["generated_image_url"] == "https://image.example/generated-ad.jpg"


@pytest.mark.anyio
async def test_api_campaign_uses_fetched_product_context(tmp_path, monkeypatch):
    def fake_fetch_best_product_context(url, product_name):
        return ProductContext(
            source_url=url,
            resolved_url="https://www.coupang.com/vp/products/example",
            page_title="키크론 C2 Pro 8K RGB 핫스왑 기계식 키보드",
            description="풀배열, RGB 백라이트, 핫스왑 스위치, 유선 USB-C 연결",
            image_url="https://image.example/keychron-meta.jpg",
            facts=["풀배열", "RGB 백라이트", "핫스왑 스위치", "유선 USB-C 연결"],
        )

    monkeypatch.setattr("codex_coupang_workbench.main.fetch_best_product_context", fake_fetch_best_product_context)
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/keychron",
                    "product_name": "키크론 키보드",
                    "memo": "",
                },
            )
        ).json()

        response = await client.post(f"/api/jobs/{job['id']}/campaign")

        assert response.status_code == 200
        campaign = response.json()
        assert "핫스왑 스위치" in campaign["sns_draft"]
        assert "유선 USB-C 연결" in campaign["blog_final"]
        assert "RGB 백라이트" in campaign["image_brief"]
        assert campaign["image_url"] == "https://image.example/keychron-meta.jpg"


@pytest.mark.anyio
async def test_api_campaign_prefers_saved_product_name_over_official_page_title(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(
            source_url=url,
            resolved_url="https://www.logitech.com/ko-kr/shop/p/mx-master-4",
            page_title="MX Master 4 무선 마우스 | Logitech",
            image_url="https://image.example/mx-master-4.jpg",
            facts=["햅틱 피드백", "MagSpeed 스크롤", "8K DPI"],
        ),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/eAJ46gv5P2",
                    "product_name": "로지텍 MX MASTER 4 무선 마우스",
                },
            )
        ).json()

        response = await client.post(f"/api/jobs/{job['id']}/campaign")

        assert response.status_code == 200
        campaign = response.json()
        assert campaign["product_name"] == "로지텍 MX MASTER 4 무선 마우스"
        assert campaign["sns_draft"].startswith("로지텍 MX MASTER 4 무선 마우스")
        assert "MX Master 4 무선 마우스 | Logitech" not in campaign["sns_draft"]


@pytest.mark.anyio
async def test_api_campaign_reuses_known_campaign_when_same_url_later_blocks_context(tmp_path, monkeypatch):
    contexts = [
        ProductContext(
            source_url="https://link.coupang.com/a/table",
            resolved_url="https://www.coupang.com/vp/products/6982459498",
            page_title="보니애가구 나탈리 포세린 세라믹 1400 식탁 + 의자 세트",
            image_url="https://thumbnail.coupangcdn.com/table.jpg",
            facts=["포세린 세라믹 1400 식탁", "일반의자 4개 구성", "방문설치 상품"],
        ),
        ProductContext(source_url="https://link.coupang.com/a/table", resolved_url="https://link.coupang.com/a/table", facts=[]),
    ]

    def fake_fetch_best_product_context(url, product_name):
        return contexts.pop(0) if contexts else ProductContext(source_url=url, resolved_url=url, facts=[])

    monkeypatch.setattr("codex_coupang_workbench.main.fetch_best_product_context", fake_fetch_best_product_context)
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/table",
                    "product_name": "보니애가구 나탈리 포세린 세라믹 1400 식탁 + 의자 세트",
                    "image_url": "https://thumbnail.coupangcdn.com/table.jpg",
                },
            )
        ).json()
        first_campaign = (await client.post(f"/api/jobs/{first['id']}/campaign")).json()
        assert "포세린 세라믹 1400 식탁" in first_campaign["sns_draft"]

        second = (
            await client.post(
                "/api/jobs",
                json={"product_url": "https://link.coupang.com/a/table"},
            )
        ).json()
        second_campaign = (await client.post(f"/api/jobs/{second['id']}/campaign")).json()

        assert second_campaign["product_name"] == "보니애가구 나탈리 포세린 세라믹 1400 식탁 + 의자 세트"
        assert "포세린 세라믹 1400 식탁" in second_campaign["sns_draft"]
        assert "상품 상세를 자동으로 충분히 읽지 못했습니다" not in second_campaign["sns_draft"]


@pytest.mark.anyio
async def test_threads_profile_auth_callback_and_publish_flow(tmp_path, monkeypatch):
    FakeThreadsClient.published = []
    FakeThreadsClient.replies = []
    monkeypatch.setattr("codex_coupang_workbench.main.ThreadsApiClient", FakeThreadsClient)
    monkeypatch.setattr(
        "codex_coupang_workbench.main.generate_codex_threads_post",
        lambda **kwargs: (_ for _ in ()).throw(CodexThreadsError("skip codex in publish flow test")),
    )
    monkeypatch.setattr(
        "codex_coupang_workbench.main.fetch_best_product_context",
        lambda url, product_name: ProductContext(
            source_url=url,
            resolved_url="https://www.coupang.com/vp/products/example",
            page_title="테슬라 파노라마 선루프 썬쉐이드 차광 커버",
            image_url="https://image.example/tesla-sunshade.jpg",
            facts=["파노라마 선루프용 차광 커버", "모델Y 호환", "현재 판매가 29,900원"],
        ),
    )
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        settings = await client.put(
            "/api/settings",
            json={
                "threads_app_id": "app-id",
                "threads_app_secret": "secret",
                "threads_redirect_uri": "http://test/api/threads/auth/callback",
            },
        )
        assert settings.status_code == 200

        create_profile = await client.post(
            "/api/threads/profiles",
            json={"profile_key": "tesla", "display_name": "테슬라 용품"},
        )
        assert create_profile.status_code == 200
        assert create_profile.json()["is_connected"] is False

        auth_start = await client.get("/api/threads/auth/start", params={"profile_key": "tesla"})
        assert auth_start.status_code == 200
        assert "state=tesla" in auth_start.json()["auth_url"]

        callback = await client.get(
            "/api/threads/auth/callback",
            params={"code": "oauth-code", "state": "tesla"},
        )
        assert callback.status_code == 200
        assert "연결 완료" in callback.text

        profiles = (await client.get("/api/threads/profiles")).json()
        assert profiles[0]["username"] == "tesla_daily"
        assert profiles[0]["is_connected"] is True
        assert "access_token" not in profiles[0]

        draft = await client.post(
            "/api/threads/draft",
            json={"profile_key": "tesla", "product_url": "https://link.coupang.com/a/example"},
        )
        assert draft.status_code == 200
        draft_payload = draft.json()
        assert "쿠팡 파트너스" not in draft_payload["text"]
        assert "쿠팡 파트너스" in draft_payload["comment_text"]
        assert "파노라마 선루프용 차광 커버" in draft_payload["text"]
        assert "29,900원" not in draft_payload["text"]

        publish = await client.post(
            "/api/threads/publish",
            json={
                "profile_key": "tesla",
                "job_id": draft_payload["job"]["id"],
                "text": draft_payload["text"],
                "comment_text": draft_payload["comment_text"],
            },
        )
        assert publish.status_code == 200
        assert publish.json()["threads_post_id"] == "post_123"
        assert publish.json()["threads_reply_id"] == "reply_123"
        assert FakeThreadsClient.published[0]["threads_user_id"] == "12345"
        assert FakeThreadsClient.published[0]["access_token"] == "long-token"
        assert FakeThreadsClient.replies[0]["reply_to_id"] == "post_123"
        assert "쿠팡 파트너스" in FakeThreadsClient.replies[0]["text"]

        records = (await client.get("/api/threads/publish-records")).json()
        assert records[0]["product_name"] == "테슬라 파노라마 선루프 썬쉐이드 차광 커버"
        assert records[0]["product_url"] == "https://link.coupang.com/a/example"
        assert records[0]["profile_key"] == "tesla"
        assert records[0]["display_name"] == "테슬라 용품"
        assert records[0]["threads_post_id"] == "post_123"


@pytest.mark.anyio
async def test_threads_profile_import_creates_profile_from_current_account(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_coupang_workbench.main.ThreadsApiClient", FakeThreadsClient)
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        settings = await client.put(
            "/api/settings",
            json={
                "threads_app_id": "app-id",
                "threads_app_secret": "secret",
                "threads_redirect_uri": "http://test/api/threads/auth/callback",
            },
        )
        assert settings.status_code == 200

        auth_start = await client.get("/api/threads/auth/import/start")
        assert auth_start.status_code == 200
        auth_url = auth_start.json()["auth_url"]
        assert "state=import-current-profile:" in auth_url
        state = auth_url.split("state=", 1)[1]

        callback = await client.get(
            "/api/threads/auth/callback",
            params={"code": "oauth-code", "state": state},
        )
        assert callback.status_code == 200

        profiles = (await client.get("/api/threads/profiles")).json()
        assert profiles[0]["profile_key"] == "tesla_daily"
        assert profiles[0]["display_name"] == "Tesla Daily"
        assert profiles[0]["username"] == "tesla_daily"
        assert profiles[0]["is_connected"] is True


@pytest.mark.anyio
async def test_threads_publish_requires_connected_profile(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/threads/profiles",
            json={"profile_key": "pet", "display_name": "반려동물 용품"},
        )
        job = (
            await client.post(
                "/api/jobs",
                json={
                    "product_url": "https://link.coupang.com/a/pet",
                    "product_name": "강아지 하네스",
                },
            )
        ).json()

        response = await client.post(
            "/api/threads/publish",
            json={"profile_key": "pet", "job_id": job["id"], "text": "테스트"},
        )

        assert response.status_code == 400
        assert "connected" in response.text
