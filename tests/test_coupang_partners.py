from codex_coupang_workbench.coupang_partners import (
    CoupangPartnersClient,
    extract_coupang_ids,
    fetch_partner_product_context,
)


class FakeCoupangTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, data=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data or {}})
        assert headers["Authorization"].startswith("CEA algorithm=HmacSHA256")
        assert "access-key=access" in headers["Authorization"]
        if url.endswith("/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"):
            return {
                "rCode": "0",
                "data": [
                    {
                        "originalUrl": data["coupangUrls"][0],
                        "shortenUrl": "https://link.coupang.com/a/partner",
                    }
                ],
            }
        if "/v2/providers/affiliate_open_api/apis/openapi/products/search" in url:
            return {
                "rCode": "0",
                "data": {
                    "productData": [
                        {
                            "productId": 9579586125,
                            "productName": "테스트 상품 정리함",
                            "productImage": "https://image.example/item.jpg",
                            "productUrl": "https://www.coupang.com/vp/products/9579586125",
                            "categoryName": "자동차용품",
                            "keyword": "9579586125",
                            "isRocket": True,
                            "isFreeShipping": False,
                        }
                    ]
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")


def test_extract_coupang_ids_from_product_url():
    ids = extract_coupang_ids(
        "https://www.coupang.com/vp/products/9579586125?itemId=28594687231&vendorItemId=123"
    )

    assert ids == ["9579586125", "28594687231", "123"]


def test_client_builds_deeplink_and_search_requests():
    transport = FakeCoupangTransport()
    client = CoupangPartnersClient("access", "secret", sub_id="threads2026", transport=transport)

    link = client.create_deeplink("https://www.coupang.com/vp/products/9579586125")
    products = client.search_products("9579586125")

    assert link == "https://link.coupang.com/a/partner"
    assert products[0]["productName"] == "테스트 상품 정리함"
    assert transport.calls[0]["data"]["subId"] == "threads2026"
    assert "subId=threads2026" in transport.calls[1]["url"]


def test_fetch_partner_product_context_maps_product_data(monkeypatch):
    monkeypatch.setattr(
        "codex_coupang_workbench.coupang_partners.resolve_coupang_redirect",
        lambda url: "https://www.coupang.com/vp/products/9579586125?itemId=28594687231",
    )
    product, resolved_url = fetch_partner_product_context(
        "https://link.coupang.com/a/example",
        access_key="access",
        secret_key="secret",
        sub_id="threads2026",
        transport=FakeCoupangTransport(),
    )

    assert resolved_url.startswith("https://www.coupang.com/vp/products/9579586125")
    assert product.product_name == "테스트 상품 정리함"
    assert product.partner_url == "https://link.coupang.com/a/partner"
    assert product.image_url == "https://image.example/item.jpg"
    assert "자동차용품 카테고리 상품" in product.facts


def test_fetch_partner_product_context_does_not_use_unmatched_search_result(monkeypatch):
    class UnmatchedTransport(FakeCoupangTransport):
        def __call__(self, method, url, headers, data=None):
            if "/v2/providers/affiliate_open_api/apis/openapi/products/search" in url:
                return {
                    "rCode": "0",
                    "data": {
                        "productData": [
                            {
                                "productId": 8185470223,
                                "productName": "다른 상품",
                                "productImage": "https://image.example/other.jpg",
                                "productUrl": "https://link.coupang.com/re/AFFSDP?pageKey=8185470223",
                            }
                        ]
                    },
                }
            return super().__call__(method, url, headers, data)

    monkeypatch.setattr(
        "codex_coupang_workbench.coupang_partners.resolve_coupang_redirect",
        lambda url: "https://www.coupang.com/vp/products/9579586125?itemId=28594687231",
    )
    product, resolved_url = fetch_partner_product_context(
        "https://link.coupang.com/a/example",
        access_key="access",
        secret_key="secret",
        transport=UnmatchedTransport(),
    )

    assert resolved_url.startswith("https://www.coupang.com/vp/products/9579586125")
    assert product.product_name == ""
    assert product.partner_url == "https://link.coupang.com/a/partner"
