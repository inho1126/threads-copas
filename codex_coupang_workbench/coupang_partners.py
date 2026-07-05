from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .product_research import ProductContext


COUPANG_PARTNERS_DOMAIN = "https://api-gateway.coupang.com"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
PRODUCT_SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"

JsonTransport = Callable[[str, str, dict[str, str], dict[str, Any] | None], dict[str, Any]]


class CoupangPartnersError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoupangPartnerProduct:
    product_name: str = ""
    product_url: str = ""
    partner_url: str = ""
    image_url: str = ""
    facts: tuple[str, ...] = ()
    product_id: str = ""

    def to_product_context(self, source_url: str, resolved_url: str = "") -> ProductContext:
        return ProductContext(
            source_url=source_url,
            resolved_url=resolved_url or self.product_url or source_url,
            page_title=self.product_name,
            image_url=self.image_url,
            facts=list(self.facts),
        )


class CoupangPartnersClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        sub_id: str = "",
        domain: str = COUPANG_PARTNERS_DOMAIN,
        transport: JsonTransport | None = None,
    ) -> None:
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.sub_id = sub_id.strip()
        self.domain = domain.rstrip("/")
        self._transport = transport or _urlopen_json_transport
        if not self.access_key or not self.secret_key:
            raise CoupangPartnersError("Coupang Partners API keys are required")

    def create_deeplink(self, coupang_url: str) -> str:
        payload: dict[str, Any] = {"coupangUrls": [coupang_url.strip()]}
        if self.sub_id:
            payload["subId"] = self.sub_id
        response = self._request("POST", DEEPLINK_PATH, data=payload)
        data = response.get("data") or []
        if not isinstance(data, list) or not data:
            return ""
        item = data[0] if isinstance(data[0], dict) else {}
        return str(
            item.get("shortenUrl")
            or item.get("shortUrl")
            or item.get("landingUrl")
            or item.get("originalUrl")
            or ""
        ).strip()

    def search_products(self, keyword: str, *, limit: int = 10, image_size: str = "512x512") -> list[dict[str, Any]]:
        clean_keyword = keyword.strip()
        if not clean_keyword:
            return []
        query: dict[str, str | int] = {
            "keyword": clean_keyword,
            "limit": max(1, min(limit, 10)),
            "imageSize": image_size,
        }
        if self.sub_id:
            query["subId"] = self.sub_id
        path_with_query = f"{PRODUCT_SEARCH_PATH}?{urlencode(query, quote_via=quote)}"
        response = self._request("GET", path_with_query)
        data = response.get("data") if isinstance(response, dict) else {}
        products = data.get("productData") if isinstance(data, dict) else []
        return products if isinstance(products, list) else []

    def _request(self, method: str, path_with_query: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": _build_authorization(
                method=method,
                path_with_query=path_with_query,
                access_key=self.access_key,
                secret_key=self.secret_key,
            ),
            "Content-Type": "application/json;charset=UTF-8",
        }
        return self._transport(method, f"{self.domain}{path_with_query}", headers, data)


def fetch_partner_product_context(
    product_url: str,
    *,
    access_key: str,
    secret_key: str,
    sub_id: str = "",
    product_keyword: str = "",
    transport: JsonTransport | None = None,
) -> tuple[CoupangPartnerProduct, str]:
    clean_url = product_url.strip()
    if not clean_url:
        return CoupangPartnerProduct(), ""
    client = CoupangPartnersClient(access_key, secret_key, sub_id=sub_id, transport=transport)
    resolved_url = resolve_coupang_redirect(clean_url) or clean_url
    partner_url = client.create_deeplink(resolved_url) or client.create_deeplink(clean_url)
    product_ids = extract_coupang_ids(resolved_url) + extract_coupang_ids(clean_url)
    products: list[dict[str, Any]] = []
    for product_id_keyword in _dedupe(product_ids):
        products = client.search_products(product_id_keyword)
        if products:
            break
    selected = _select_product(products, product_ids)
    if not selected and product_keyword.strip():
        products = client.search_products(product_keyword.strip())
        selected = _select_product(products, product_ids)
    if not selected:
        return CoupangPartnerProduct(partner_url=partner_url), resolved_url
    return _product_from_api(selected, partner_url=partner_url), resolved_url


def resolve_coupang_redirect(url: str, timeout: float = 8.0) -> str:
    request = Request(
        url.strip(),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
        },
    )
    opener = build_opener(_NoRedirectHandler)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        location = exc.headers.get("Location", "")
        return location.strip()
    except (OSError, URLError, ValueError):
        return ""
    location = response.headers.get("Location", "")
    return location.strip()


def extract_coupang_ids(url: str) -> list[str]:
    parsed = urlparse(url.strip())
    ids: list[str] = []
    path_parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "products" and index + 1 < len(path_parts):
            ids.append(path_parts[index + 1])
    query = parse_qs(parsed.query)
    for key in ("productId", "itemId", "vendorItemId"):
        ids.extend(query.get(key, []))
    return [item for item in _dedupe(ids) if item.isdigit()]


def _build_authorization(method: str, path_with_query: str, access_key: str, secret_key: str) -> str:
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    path, _, query = path_with_query.partition("?")
    message = f"{signed_date}{method.upper()}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        "CEA algorithm=HmacSHA256, "
        f"access-key={access_key}, signed-date={signed_date}, signature={signature}"
    )


def _urlopen_json_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CoupangPartnersError(f"Coupang Partners API HTTP {exc.code}: {detail}") from exc
    except (OSError, URLError) as exc:
        raise CoupangPartnersError(f"Coupang Partners API request failed: {exc}") from exc
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CoupangPartnersError("Coupang Partners API returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise CoupangPartnersError("Coupang Partners API returned an unexpected response")
    if str(parsed.get("rCode", "0")) not in {"0", ""}:
        raise CoupangPartnersError(str(parsed.get("rMessage") or parsed))
    return parsed


def _select_product(products: list[dict[str, Any]], product_ids: list[str]) -> dict[str, Any]:
    if not products:
        return {}
    wanted = set(product_ids)
    for product in products:
        if str(product.get("productId", "")).strip() in wanted:
            return product
        if any(product_id and product_id in str(product.get("productUrl", "")) for product_id in wanted):
            return product
    if wanted:
        return {}
    return products[0] if isinstance(products[0], dict) else {}


def _product_from_api(product: dict[str, Any], partner_url: str) -> CoupangPartnerProduct:
    facts: list[str] = []
    category = str(product.get("categoryName") or "").strip()
    keyword = str(product.get("keyword") or "").strip()
    if category:
        facts.append(f"{category} 카테고리 상품")
    if keyword and keyword not in category:
        facts.append(f"{keyword} 관련 상품")
    if product.get("isRocket"):
        facts.append("로켓배송 가능 여부 표시")
    if product.get("isFreeShipping"):
        facts.append("무료배송 가능 여부 표시")
    return CoupangPartnerProduct(
        product_name=str(product.get("productName") or "").strip(),
        product_url=str(product.get("productUrl") or "").strip(),
        partner_url=partner_url or str(product.get("productUrl") or "").strip(),
        image_url=str(product.get("productImage") or "").strip(),
        facts=tuple(facts),
        product_id=str(product.get("productId") or "").strip(),
    )


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in deduped:
            deduped.append(clean)
    return deduped


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None
