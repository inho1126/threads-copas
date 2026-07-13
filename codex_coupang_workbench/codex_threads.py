from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .writer import DISCLOSURE

DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
THREADS_COPY_MAX_CHARS = 90
THREADS_COPY_MAX_SENTENCES = 2
PERSONAS = (
    ("curiosity", "호기심"),
    ("relatable", "현실 공감"),
    ("problem_solution", "문제 해결"),
    ("honest_discovery", "솔직한 발견"),
    ("story", "스토리"),
    ("conversion", "구매 전환"),
)
THREAD_STYLE_DIRECTIONS = {
    "curiosity": (
        "호기심",
        "용도를 바로 밝히지 말고, 의외의 사용 장면을 짧게 던져 하나의 궁금증으로 끝내기.",
    ),
    "relatable": (
        "현실 공감",
        "독자가 겪어봤을 구체적인 불편으로 시작하고, 해결책은 직접 밝히지 않은 채 자연스러운 궁금증으로 끝내기.",
    ),
    "problem_solution": (
        "문제 해결",
        "자주 반복되는 불편을 한 문장으로 보여주고, 생각보다 단순한 해결 방식이 있음만 암시하기.",
    ),
    "honest_discovery": (
        "솔직한 발견",
        "처음엔 별거 아니라고 느꼈지만 사용 맥락을 알고 나면 달라 보이는 포인트를 솔직하고 짧게 풀기.",
    ),
    "story": (
        "스토리",
        "짧은 대화나 생활 장면에서 시작하되, 실제 후기를 지어내지 말고 상황의 흐름만 살리기.",
    ),
    "conversion": (
        "구매 전환",
        "직접적으로 사라고 하지 말고, 어떤 사람의 어떤 순간에 유용한지만 선명하게 남겨 확인하고 싶게 만들기.",
    ),
    "shock": (
        "충격 문장형",
        "첫 1~2줄에 불편의 강한 결과나 의외의 순간을 먼저 보여주기. 맥락과 인과관계는 유지하고 거짓 효과나 억지 반전은 만들지 않기.",
    ),
    "viral": (
        "바이럴 발견형",
        "'눈을 의심함', '이런 게 왜 여기 있어?', '나만 이제 알았나'처럼 뜻밖의 발견으로 시작하고 확인된 상품 정보 중 의외의 포인트 하나를 말맛 있게 강조하기. 유명인 사용, 본인 실사용, 가족 반응, 효능, 가격, 품절이나 희소성은 입력에 있어도 사실처럼 주장하지 않기.",
    ),
}


class CodexThreadsError(RuntimeError):
    pass


def generate_codex_threads_post(
    *,
    product_name: str,
    product_url: str,
    product_facts: list[str] | None = None,
    memo: str = "",
    persona: str = "",
    prompt: str = "",
    style: str = "",
    custom_instruction: str = "",
    model: str = DEFAULT_CODEX_MODEL,
    timeout: float = 90.0,
) -> str:
    if shutil.which("codex") is None:
        raise CodexThreadsError("Codex CLI is not installed")

    temp_dir = Path(tempfile.mkdtemp(prefix="threads-codex-"))
    output_path = temp_dir / "threads-post.txt"
    command = [
        "codex",
        "exec",
        "--config",
        "model_reasoning_effort=medium",
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

    base_prompt = _build_codex_prompt(
        product_name=product_name,
        product_url=product_url,
        product_facts=product_facts or [],
        memo=memo,
        persona=persona,
        style=style or "relatable",
        custom_instruction=custom_instruction,
    )
    clean_prompt = prompt.strip()
    input_prompt = base_prompt
    if clean_prompt:
        input_prompt = "\n\n".join(
            [
                base_prompt,
                "사용자 추가 요청:",
                clean_prompt,
                "추가 요청은 위의 필수 제약을 바꾸지 않는 범위에서만 반영해.",
            ]
        )

    try:
        try:
            subprocess.run(
                command,
                input=input_prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=True,
                cwd=str(temp_dir),
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise CodexThreadsError(detail or str(exc)) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexThreadsError(str(exc)) from exc

        try:
            text = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CodexThreadsError("Codex output could not be read") from exc
        if not text:
            raise CodexThreadsError("Codex did not return a Threads post")
        return _normalize_generated_post(text, product_url, product_name)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_codex_prompt(
    *,
    product_name: str,
    product_url: str,
    product_facts: list[str],
    memo: str,
    persona: str,
    style: str = "relatable",
    custom_instruction: str = "",
) -> str:
    facts = "\n".join(f"- {fact}" for fact in product_facts if fact.strip()) or "- 자동 수집된 상세 정보 없음"
    persona_line = persona.strip() or "친근하고 실사용 관점이 있는 한국어 Threads 작성자"
    style_label, style_direction = _style_direction(style, custom_instruction)
    return "\n".join(
        [
            "Codex CLI에 로그인된 계정 인증을 사용해 쿠팡 파트너스 Threads 게시글을 작성해줘.",
            "최종 답변에는 게시글 본문만 출력해. 설명, 마크다운 코드블록, 주석은 쓰지 마.",
            "",
            "스타일:",
            "- 상품 설명이나 가상 상황극이 아니라 실제 사용 맥락이 자연스럽게 이어지는 Threads 본문",
            "- 독자가 겪어봤을 법한 구체적인 불편이나 순간으로 시작하기",
            "- 상품의 용도와 사용 장면은 선명하게 보여주되, 해결 방법을 바로 밝히지 않는 방식으로 궁금증 남기기",
            "- 문장 연결과 인과관계를 우선하고, 억지 반전이나 말장난보다 자연스러운 공감을 살리기",
            "- 짧은 문장과 줄바꿈으로 모바일에서 편하게 읽히는 톤",
            f"- 이번 출력 스타일: {style_label}",
            f"- {style_direction}",
            f"- 작성자 톤: {persona_line}",
            "",
            "반드시 지킬 것:",
            "- 링크와 고지 문구는 본문에 쓰지 마. 링크와 고지는 별도 댓글에 들어간다.",
            "- '자세한 건 댓글에 남겨둘게요' 같은 댓글 안내 문장 쓰지 않기",
            "- 해시태그 쓰지 않기",
            "- 가격, 할인율, 배송일, 재고, 리뷰 수는 쓰지 않기",
            "- 입력에 없는 효과, 인증, 성능, 호환 모델은 지어내지 않기",
            "- 검증된 상품 정보 중 한 가지 구체적 특징을 반드시 본문 맥락에 반영하기",
            "- bullet 목록 금지",
            "- 상품명은 본문에 직접 쓰지 마. 브랜드명, 모델명, 정확한 상품명 노출 금지",
            "- 브랜드·모델·정확한 상품명은 숨기되, 상품 카테고리와 실제 쓰임은 독자가 알 수 있게 쓰기",
            "- 후기 내용은 사실 주장처럼 쓰지 말고 분위기만 참고하기",
            "- 실제로 사용했다는 1인칭 후기나 가족의 반응을 지어내지 마",
            "- 설명문처럼 쓰지 마. 사양, 구성, 장점 나열 금지",
            "- 구매 전 같은 표현 쓰지 마. '확인해보세요', '추천', '필요한 분', '비교해볼 만' 같은 문구도 쓰지 마",
            "- 억지 반전, 랜덤 비유, 과장된 결과를 만들지 마",
            "- 1~2개 짧은 문장, 공백과 문장부호를 포함해 90자 이내",
            "- 가능하면 45~75자로 끝내고, 같은 맥락을 반복해 길이를 늘리지 마",
            "- 모든 문장은 짧은 해체(반말 구어체)로 쓰고, ~요·~습니다·~세요 같은 높임말 종결은 쓰지 마",
            "- 각 줄은 자연스러운 한국어로 의미를 완결하기",
            "- 마지막 문단은 상품명을 밝히거나 댓글을 안내하지 말고, 왜 이런 방식이 눈에 들어오는지 살짝 궁금하게 끝내기",
            "",
            f"내부 참고용 상품명: {product_name.strip() or '상품명 자동 확인 필요'}",
            f"쿠팡 URL: {product_url.strip()}",
            "상품 정보:",
            facts,
            f"사용자 메모: {memo.strip() or '없음'}",
        ]
    )


def _normalize_generated_post(text: str, product_url: str, product_name: str = "") -> str:
    clean_text = unicodedata.normalize("NFKC", text).strip().strip("`")
    clean_url = product_url.strip()
    clean_name = unicodedata.normalize("NFKC", product_name).strip()
    if DISCLOSURE in clean_text:
        clean_text = clean_text.replace(DISCLOSURE, "")
    if clean_url:
        clean_text = clean_text.replace(clean_url, "")
    if clean_name:
        clean_text = re.sub(re.escape(clean_name), "", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(
        r"(?im)^.*쿠팡\s*파트너스.*$",
        "",
        clean_text,
    )
    clean_text = re.sub(
        r"(?im)^.*(?:제휴|광고).*(?:수수료|제공받|받을\s*수).*$",
        "",
        clean_text,
    )
    clean_text = re.sub(r"https?://[^\s]+", "", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"#[^\s#]+", "", clean_text)
    clean_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", clean_text)
    clean_text = clean_text.replace("#쿠팡파트너스", "")
    clean_text = "\n".join(
        line.rstrip()
        for line in clean_text.splitlines()
        if not _should_drop_generated_line(line)
    )
    return _limit_generated_post(clean_text.strip())


def _limit_generated_post(text: str) -> str:
    compact = _casualize_generated_post(re.sub(r"[ \t]+", " ", text).strip())
    if not compact:
        return ""

    sentences = [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?…])\s+|\n+", compact)
        if sentence.strip()
    ]
    selected: list[str] = []
    for sentence in sentences[:THREADS_COPY_MAX_SENTENCES]:
        candidate = " ".join([*selected, sentence])
        if len(candidate) > THREADS_COPY_MAX_CHARS:
            break
        selected.append(sentence)

    if selected:
        return " ".join(selected)
    if len(compact) <= THREADS_COPY_MAX_CHARS:
        return compact

    cutoff = compact.rfind(" ", 0, THREADS_COPY_MAX_CHARS)
    if cutoff < THREADS_COPY_MAX_CHARS // 2:
        cutoff = THREADS_COPY_MAX_CHARS - 1
    return compact[:cutoff].rstrip(" ,.;:!?…") + "…"


def _casualize_generated_post(text: str) -> str:
    sentence_end = r"(?=[.!?…]|$)"
    replacements = (
        (r"있습니다" + sentence_end, "있어"),
        (r"없습니다" + sentence_end, "없어"),
        (r"입니다" + sentence_end, "이야"),
        (r"합니다" + sentence_end, "해"),
        (r"됩니다" + sentence_end, "돼"),
        (r"주세요" + sentence_end, "줘"),
        (r"보세요" + sentence_end, "봐"),
        (r"하세요" + sentence_end, "해"),
        (r"있어요" + sentence_end, "있어"),
        (r"없어요" + sentence_end, "없어"),
        (r"이에요" + sentence_end, "이야"),
        (r"예요" + sentence_end, "야"),
        (r"거든요" + sentence_end, "거든"),
        (r"해요" + sentence_end, "해"),
        (r"돼요" + sentence_end, "돼"),
        (r"봐요" + sentence_end, "봐"),
        (r"여요" + sentence_end, "여"),
        (r"어요" + sentence_end, "어"),
        (r"아요" + sentence_end, "아"),
        (r"나요" + sentence_end, "나"),
        (r"까요" + sentence_end, "까"),
        (r"네요" + sentence_end, "네"),
        (r"군요" + sentence_end, "군"),
        (r"죠" + sentence_end, "지"),
        (r"([가-힣]+)습니다" + sentence_end, r"\1다"),
    )
    casual = text
    for pattern, replacement in replacements:
        casual = re.sub(pattern, replacement, casual)
    return casual


def _should_drop_generated_line(line: str) -> bool:
    clean_line = line.strip()
    if clean_line.startswith("#"):
        return True
    if "댓글" in clean_line and any(term in clean_line for term in ("남겨", "남길", "확인", "링크", "자세한")):
        return True
    if any(term in clean_line for term in ("구매 전", "확인해보세요", "추천", "비교해볼 만", "필요한 분")):
        return True
    return False


def _style_direction(style: str, custom_instruction: str = "") -> tuple[str, str]:
    if style == "custom":
        direction = custom_instruction.strip()
        if not direction:
            direction = "사용자가 지정한 말투로 짧고 자연스럽게 호기심을 유발하기."
        return "커스텀", direction
    return THREAD_STYLE_DIRECTIONS.get(style, THREAD_STYLE_DIRECTIONS["relatable"])
