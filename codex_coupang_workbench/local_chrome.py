from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from subprocess import CompletedProcess
from typing import Any

from .product_research import ProductContext


class LocalChromeError(RuntimeError):
    pass


CHROME_EXTRACT_SCRIPT = """
(() => {
  const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const texts = (selector) => Array.from(document.querySelectorAll(selector))
    .map((el) => clean(el.innerText || el.textContent || el.getAttribute("alt") || ""))
    .filter(Boolean);
  const attrs = (selector, attr) => Array.from(document.querySelectorAll(selector))
    .map((el) => el.getAttribute(attr) || "")
    .filter(Boolean);
  const unique = (items) => [...new Set(items.map(clean).filter(Boolean))];
  const detailSelectors = [
    "#productDetail",
    ".product-detail",
    ".prod-description",
    ".sdp-product-detail",
    "[class*=product-detail]",
    "[class*=description]",
  ];
  const reviewSelectors = [
    ".product-review",
    ".sdp-review",
    ".sdp-review__article__list__review__content",
    ".js_reviewArticleContent",
    "[class*=review]",
  ];
  const detailTexts = unique(detailSelectors.flatMap(texts))
    .filter((text) => text.length > 20)
    .slice(0, 8);
  const detailImages = unique(attrs(
    "#productDetail img, .product-detail img, .prod-description img, [class*=product-detail] img",
    "src"
  )).slice(0, 30);
  const reviewBlocks = unique(reviewSelectors.flatMap(texts))
    .filter((text) => text.length > 80)
    .slice(0, 4);
  return JSON.stringify({
    title: document.title || "",
    h1: document.querySelector("h1")?.textContent?.trim() || "",
    prodTitle: document.querySelector(".prod-buy-header__title")?.textContent?.trim() || "",
    ogTitle: document.querySelector('meta[property="og:title"]')?.getAttribute("content") || "",
    twitterTitle: document.querySelector('meta[name="twitter:title"]')?.getAttribute("content") || "",
    imageUrl: document.querySelector('meta[property="og:image"]')?.getAttribute("content") || "",
    url: location.href,
    bodyHead: (document.body?.innerText || "").slice(0, 800),
    detailTexts,
    detailImages,
    reviewBlocks
  });
})()
""".strip()

CHROME_APPLESCRIPT = """
on run argv
  set targetUrl to item 1 of argv
  set extractionScript to item 2 of argv
  tell application "Google Chrome"
    activate
    if (count of windows) = 0 then make new window
    set targetWindow to front window
    set targetTab to make new tab at end of tabs of targetWindow with properties {URL:targetUrl}
    set active tab index of targetWindow to (count of tabs of targetWindow)
    repeat with i from 1 to 80
      delay 0.25
      try
        tell targetTab to set readyState to execute javascript "document.readyState"
        if readyState is "interactive" or readyState is "complete" then exit repeat
      end try
    end repeat
    delay 2
    repeat with i from 1 to 14
      tell targetTab to execute javascript "window.scrollBy(0, Math.floor(window.innerHeight * 0.85));"
      delay 0.35
    end repeat
    try
      tell targetTab to execute javascript "(() => { const clean = s => (s || '').trim(); const target = Array.from(document.querySelectorAll('a,button,li,div')).find(el => /상품평|상품 리뷰|리뷰/.test(clean(el.innerText || el.textContent))); if (target) { target.scrollIntoView({block: 'center'}); target.click(); } })()"
      delay 1.5
      repeat with i from 1 to 8
        tell targetTab to execute javascript "window.scrollBy(0, Math.floor(window.innerHeight * 0.9));"
        delay 0.25
      end repeat
    end try
    tell targetTab to return execute javascript extractionScript
  end tell
end run
""".strip()


def fetch_chrome_product_context(
    product_url: str,
    *,
    timeout: float = 30.0,
    runner: Callable[..., CompletedProcess[str]] | None = None,
) -> ProductContext:
    clean_url = product_url.strip()
    if not clean_url:
        raise LocalChromeError("쿠팡 URL을 입력해 주세요.")
    run = runner or subprocess.run
    if runner is None and shutil.which("osascript") is None:
        raise LocalChromeError("로컬 Chrome 확인은 macOS Google Chrome에서만 사용할 수 있습니다.")
    try:
        completed = run(
            ["osascript", "-e", CHROME_APPLESCRIPT, clean_url, CHROME_EXTRACT_SCRIPT],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalChromeError("Chrome에서 상품 페이지 확인 시간이 초과되었습니다.") from exc
    except OSError as exc:
        raise LocalChromeError("Chrome을 실행하거나 제어하지 못했습니다.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = "Chrome에서 상품 정보를 읽지 못했습니다."
        if detail:
            message = f"{message} {detail}"
        raise LocalChromeError(message)
    return _context_from_payload(clean_url, completed.stdout)


def _context_from_payload(product_url: str, raw_payload: str) -> ProductContext:
    try:
        payload = json.loads(raw_payload.strip())
    except json.JSONDecodeError as exc:
        raise LocalChromeError("Chrome 응답을 해석하지 못했습니다.") from exc
    if not isinstance(payload, dict):
        raise LocalChromeError("Chrome 응답 형식이 올바르지 않습니다.")
    title = _best_product_title(payload)
    if not title:
        raise LocalChromeError("Chrome에서 상품명을 찾지 못했습니다.")
    resolved_url = _clean_text(payload.get("url")) or product_url
    return ProductContext(
        source_url="chrome",
        resolved_url=resolved_url,
        page_title=title,
        image_url=_normalize_url(_clean_text(payload.get("imageUrl"))),
        facts=_facts_from_payload(payload),
    )


def _facts_from_payload(payload: dict[str, Any]) -> list[str]:
    facts = ["Chrome에서 확인한 상품명"]
    for text in _clean_list(payload.get("detailTexts"))[:4]:
        facts.append(f"상세: {_truncate_fact(text)}")
    detail_images = _clean_list(payload.get("detailImages"))
    if detail_images:
        facts.append(f"상세 이미지 {len(detail_images)}개 확인")
    for text in _clean_list(payload.get("reviewBlocks"))[:2]:
        facts.append(f"상품평: {_truncate_fact(text, max_length=420)}")
    return facts


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _truncate_fact(value: str, max_length: int = 180) -> str:
    text = _clean_text(value)
    return text if len(text) <= max_length else f"{text[:max_length].rstrip()}..."


def _best_product_title(payload: dict[str, Any]) -> str:
    for key in ("h1", "prodTitle", "ogTitle", "twitterTitle", "title"):
        title = _clean_product_title(payload.get(key))
        if _is_useful_title(title):
            return title
    return ""


def _clean_product_title(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r"\s+-\s+[^|]+?\|\s*쿠팡\s*$", "", text)
    text = re.sub(r"\s+\|\s*쿠팡\s*$", "", text)
    return text.strip()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_url(value: str) -> str:
    if value.startswith("//"):
        return f"https:{value}"
    return value


def _is_useful_title(title: str) -> bool:
    lower_title = title.lower()
    if not title or title == "쿠팡":
        return False
    return "access denied" not in lower_title and "permission" not in lower_title
