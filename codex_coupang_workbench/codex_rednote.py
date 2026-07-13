from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


_CHINESE_QUERY_PATTERN = re.compile(r"^[\u3400-\u9fff]{2,24}$")
_KOREAN_PRODUCT_TERMS = (
    ("보조배터리", "充电宝"),
    ("레이저 가위", "激光剪刀"),
    ("무선청소기", "无线吸尘器"),
    ("무선 청소기", "无线吸尘器"),
    ("차량용", "汽车"),
    ("자동차", "汽车"),
    ("테슬라", "特斯拉"),
    ("수납함", "收纳"),
    ("수납", "收纳"),
    ("무선", "无线"),
    ("청소기", "吸尘器"),
    ("충전기", "充电器"),
    ("방수", "防水"),
    ("스프레이", "喷雾"),
    ("신발", "鞋子"),
    ("선풍기", "风扇"),
    ("가습기", "加湿器"),
    ("주방", "厨房"),
    ("정리", "整理"),
)


def generate_rednote_query(
    *,
    product_name: str,
    product_facts: list[str],
    model: str,
    timeout: float = 60.0,
) -> str:
    """Return one Chinese RedNote shopping query without URL or punctuation."""
    fallback = _fallback_rednote_query(product_name)
    if shutil.which("codex") is None:
        return fallback

    temp_dir = Path(tempfile.mkdtemp(prefix="rednote-query-codex-"))
    output_path = temp_dir / "rednote-query.txt"
    command = [
        "codex",
        "exec",
        "--config",
        "model_reasoning_effort=low",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
    ]
    clean_model = model.strip()
    if clean_model:
        command.extend(["--model", clean_model])
    command.append("-")

    try:
        try:
            subprocess.run(
                command,
                input=_build_rednote_query_prompt(product_name, product_facts),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=True,
                cwd=str(temp_dir),
            )
            raw_query = output_path.read_text(encoding="utf-8")
        except (OSError, subprocess.SubprocessError):
            return fallback
        return _validated_rednote_query(raw_query) or fallback
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_rednote_query_prompt(product_name: str, product_facts: list[str]) -> str:
    del product_facts
    return "\n".join(
        [
            "请将下面用于小红书搜索的韩文商品名原样直译成简短的中文商品名。",
            "只输出翻译后的商品名，不要解释，不要 JSON。",
            "必须只包含 2到24个中文字符，不要空格、标点、链接或换行。",
            "禁止添加用途、场景、推荐、实测、神器、最高级或其他营销修饰语。",
            "例如：레이저 가위 -> 激光剪刀。",
            f"쿠팡 한국어 검색어: {product_name.strip()}",
        ]
    )


def _validated_rednote_query(raw_query: str) -> str:
    candidate = raw_query.strip().strip("`").strip()
    if not candidate:
        return ""
    if any(character in candidate for character in ("\n", "\r")):
        return ""
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in candidate):
        return ""
    lowered = candidate.lower()
    if "http" in lowered or "www." in lowered or "://" in candidate:
        return ""
    if not _CHINESE_QUERY_PATTERN.fullmatch(candidate):
        return ""
    return candidate


def _fallback_rednote_query(product_name: str) -> str:
    clean_name = product_name.strip()
    direct_chinese = "".join(re.findall(r"[\u3400-\u9fff]", clean_name))
    if len(direct_chinese) >= 2:
        base = direct_chinese[:20]
    else:
        matches: list[tuple[int, int, int, str]] = []
        for source, target in _KOREAN_PRODUCT_TERMS:
            position = clean_name.find(source)
            if position >= 0:
                matches.append((position, -len(source), len(source), target))
        matches.sort()
        translated: list[str] = []
        occupied: set[int] = set()
        for position, _priority, source_length, target in matches:
            span = set(range(position, position + source_length))
            if span & occupied:
                continue
            occupied.update(span)
            if target not in translated:
                translated.append(target)
        base = "".join(translated)
    if len(base) < 2:
        return "韩国商品"
    query = base[:24]
    return query if _CHINESE_QUERY_PATTERN.fullmatch(query) else "韩国商品"
