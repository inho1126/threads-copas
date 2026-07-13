from __future__ import annotations

from dataclasses import dataclass
import re


DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."


@dataclass(frozen=True)
class DraftPost:
    title: str
    body: str
    sections: list[str]
    tags: list[str]


@dataclass(frozen=True)
class CampaignPackage:
    title: str
    sns_draft: str
    image_brief: str
    blog_final: str
    sns_final: str
    tags: list[str]


def generate_draft(
    product_name: str,
    product_url: str,
    memo: str = "",
    persona: str = "",
    image_url: str = "",
) -> DraftPost:
    clean_name = product_name.strip() or "추천 상품"
    clean_url = product_url.strip()
    clean_memo = memo.strip() or "상품 특징을 확인하고 구매 전 체크할 포인트를 정리했습니다."
    clean_image_url = image_url.strip()
    subject_marker = _subject_marker(clean_name)

    title = f"{clean_name} 구매 전 체크할 점과 추천 포인트"
    sections = [
        "첫인상",
        "구매 전 확인하면 좋은 점",
        "이런 분에게 잘 맞습니다",
        "정리",
    ]
    tags = _build_tags(clean_name)
    parts = [
        DISCLOSURE,
        title,
    ]
    if clean_image_url:
        parts.append(f"![{clean_name}]({clean_image_url})")
    parts.extend(
        [
            "첫인상",
            f"{clean_memo}",
            f"{clean_name}{subject_marker} 가격, 배송 조건, 최근 리뷰 흐름을 함께 보고 판단하는 것이 좋습니다.",
            "구매 전 체크 포인트",
            "• 현재 판매가와 쿠폰 적용 여부",
            "• 배송 방식과 도착 예정일",
            "• 최근 리뷰에서 반복해서 언급되는 장점과 불편점",
            "• 옵션, 색상, 구성품 차이",
            "이런 분에게 잘 맞습니다",
            "구매 전에 핵심 장단점을 빠르게 확인하고 싶은 분, 용도에 맞는 옵션을 비교해 보고 싶은 분에게 잘 맞습니다.",
            "정리",
            f"{clean_name}{subject_marker} 구매 목적이 분명할 때 만족도가 높아지는 제품입니다. 최신 가격과 옵션은 아래 링크에서 한 번 더 확인해 보세요.",
            f"{clean_url}",
        ]
    )
    body = "\n\n".join(parts)
    return DraftPost(title=title, body=body, sections=sections, tags=tags)


def generate_campaign(
    product_name: str,
    product_url: str,
    memo: str = "",
    reference_image_url: str = "",
    persona: str = "",
    product_facts: list[str] | None = None,
    product_page_title: str = "",
    product_description: str = "",
) -> CampaignPackage:
    clean_name = product_page_title.strip() or product_name.strip() or "추천 상품"
    clean_url = product_url.strip()
    clean_memo = memo.strip()
    clean_description = product_description.strip()
    clean_reference = reference_image_url.strip()
    subject_marker = _subject_marker(clean_name)
    tags = _build_tags(clean_name)
    persona_hint = persona.strip() or "실사용 관점의 블로그 에디터"
    title = f"{clean_name} 광고 캠페인"
    facts = _normalize_facts(product_facts or [])
    if clean_memo:
        facts.extend(_normalize_facts([clean_memo]))
    if clean_description and not facts:
        facts.extend(_normalize_facts([clean_description]))
    facts = _dedupe(facts)
    public_facts = _public_content_facts(facts)
    has_product_details = bool(public_facts)
    fact_line = (
        ", ".join(public_facts[:5])
        if has_product_details
        else "상품 상세를 자동으로 충분히 읽지 못했습니다. 강조 포인트를 입력하면 그 내용으로 다시 정리합니다."
    )
    fact_bullets = (
        "\n".join(f"• {fact}" for fact in public_facts[:6])
        if has_product_details
        else "• 상품 상세를 자동으로 충분히 읽지 못했습니다.\n• 상품명과 링크만으로 확인 가능한 범위가 제한적입니다."
    )
    primary_fact = public_facts[0] if has_product_details else "상품 상세를 자동으로 충분히 읽지 못했습니다"

    sns_draft = "\n".join(
        [
            f"{clean_name}",
            (
                f"{primary_fact} 중심으로 보는 상품입니다."
                if has_product_details
                else f"{primary_fact}. 상품 내용 / 강조 포인트를 입력하면 더 정확한 SNS 글로 다시 정리합니다."
            ),
            fact_bullets,
            "상세 옵션과 구성은 링크에서 확인하세요." if has_product_details else "상품 내용 / 강조 포인트를 보강하면 더 정확하게 정리됩니다.",
            f"{clean_url}",
            "#쿠팡추천 #쿠팡파트너스",
        ]
    )

    reference_line = (
        f"참고 이미지 URL: {clean_reference}. 제품 형태와 주요 색감은 이 이미지를 우선 참고한다."
        if clean_reference
        else "참고 이미지가 없으므로 제품명과 확보된 상품 정보를 바탕으로 자연스러운 실사 광고 컷을 구성한다."
    )
    image_brief = "\n".join(
        [
            "imagegen 실사 광고 이미지 브리프",
            f"상품: {clean_name}",
            f"상품에서 반드시 보여줄 요소: {fact_line}",
            reference_line,
            "방향: 제품이 첫눈에 보이는 프리미엄 커머셜 사진. 상품의 실제 용도와 핵심 사양이 시각적으로 느껴지는 사용 장면 또는 제품 단독 컷.",
            "구도: 제품을 화면 중심에 크게 배치하고 주변 소품은 상품의 용도만 암시할 정도로 절제한다.",
            "피해야 할 요소: 정보 나열 그래픽, 표, 긴 문구, 말풍선, 튜토리얼 화면, 과장된 효과, 브랜드가 아닌 임의 로고.",
            "결과물: 블로그와 SNS 광고에 바로 사용할 수 있는 사실적인 제품 광고 이미지.",
        ]
    )

    blog_final_parts = [
        DISCLOSURE,
            f"{clean_name} 핵심 포인트 정리",
        (
            _intro_sentence(clean_name, public_facts, persona_hint)
            if has_product_details
            else f"{clean_name}{subject_marker} 현재 자동 수집된 상세 정보가 부족합니다. 아래 링크에서 옵션과 구성품을 확인한 뒤 강조 포인트를 보강하는 것이 좋습니다."
        ),
        "상품 내용",
        fact_bullets,
        "활용 장면",
        _usage_sentence(clean_name, public_facts),
        (
            f"{clean_name}{subject_marker} 위 요소가 필요한 분에게 우선 비교해볼 만한 상품입니다. 상세 옵션과 구성은 아래 링크에서 확인하세요."
            if has_product_details
            else f"{clean_name}{subject_marker} 상품 상세를 자동으로 충분히 읽지 못했습니다. 정확한 광고 글을 위해 상품 상세의 핵심 사양이나 강조 포인트를 입력해 주세요."
        ),
        clean_url,
    ]
    blog_final = "\n\n".join(blog_final_parts)

    sns_final = "\n".join(
        [
            DISCLOSURE,
            f"{clean_name}",
            f"{fact_line}",
            "필요한 사양과 사용 장면이 맞는지 링크에서 확인하세요.",
            clean_url,
            "#쿠팡파트너스 #쿠팡추천",
        ]
    )

    return CampaignPackage(
        title=title,
        sns_draft=sns_draft,
        image_brief=image_brief,
        blog_final=blog_final,
        sns_final=sns_final,
        tags=tags,
    )


def generate_threads_post(
    product_name: str,
    product_url: str,
    product_facts: list[str] | None = None,
    memo: str = "",
    persona: str = "",
    style: str = "relatable",
) -> str:
    clean_name = product_name.strip() or "추천 상품"
    facts = _normalize_facts(product_facts or [])
    if memo.strip():
        facts.extend(_normalize_facts([memo]))
    public_facts = _public_content_facts(_dedupe(facts))
    relatable = _threads_curiosity_post(clean_name, public_facts)
    if style in {"shock", "problem_solution"}:
        return _threads_shock_post(relatable)
    if style == "story":
        return _threads_story_post(relatable)
    if style in {"viral", "curiosity"}:
        return _threads_viral_post(relatable)
    if style == "honest_discovery":
        return _threads_honest_discovery_post(relatable)
    if style == "conversion":
        return _threads_conversion_post(relatable)
    return relatable


def generate_threads_comment(product_url: str, product_name: str = "") -> str:
    clean_url = product_url.strip()
    parts = [DISCLOSURE]
    if product_name.strip():
        parts.append(product_name.strip())
    if clean_url:
        parts.append(clean_url)
    return "\n\n".join(parts)


def _build_tags(product_name: str) -> list[str]:
    tokens = [part for part in product_name.replace("/", " ").split() if part]
    tags = [product_name]
    tags.extend(tokens[:4])
    tags.extend(["쿠팡추천", "쿠팡파트너스"])
    deduped: list[str] = []
    for tag in tags:
        if tag not in deduped:
            deduped.append(tag)
    return deduped


def _threads_hook_sentence(product_name: str) -> str:
    lowered = product_name.lower()
    if any(term in product_name for term in ("강아지", "반려", "펫", "하네스", "물티슈")):
        return "산책이나 외출이 잦으면 작게 챙겨두는 용품이 은근히 편하더라고요."
    if "테슬라" in product_name or "tesla" in lowered:
        return "차 안에서 매일 거슬리는 부분은 작은 용품 하나로 체감이 꽤 달라집니다."
    if any(term in product_name for term in ("우산", "레인", "부츠", "장우산")):
        return "비 오는 날엔 꺼내기 쉽고 바로 쓰기 편한지가 제일 먼저 보이더라고요."
    return "자주 쓰는 생활템은 거창한 기능보다 실제로 손이 자주 가는지가 중요하죠."


def _threads_curiosity_post(product_name: str, facts: list[str]) -> str:
    context = " ".join([product_name, *facts]).lower()
    if _has_any(context, ("비상", "탈출", "도어", "개폐", "스트랩", "손잡이")):
        return "\n\n".join(
            [
                "뒷좌석에서 문이 바로 안 열리는 순간은\n평소엔 생각도 안 하던 부분을 보게 만듦.",
                "매일 쓸 일은 없어 보여도\n필요한 순간엔 찾는 과정부터 짧아야 함.",
                "차 안에 이런 방법이 있다는 걸\n왜 미리 알아두는지 조금 이해되는 부분임.",
            ]
        )
    if _has_any(context, ("콘솔", "수납", "트레이", "정리함")) and _has_any(
        context, ("차량", "자동차", "테슬라", "차 안", "차 ")
    ):
        return "\n\n".join(
            [
                "차에 타고 나서 카드나 열쇠를 찾느라\n콘솔 주변을 한 번씩 뒤질 때가 있음.",
                "작은 물건은 금방 굴러다니는데\n막상 꺼낼 땐 손에 바로 안 잡힘.",
                "자리 하나를 나눠 쓰는 방식이\n왜 차 안에서 유독 눈에 들어오는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("목베개", "목쿠션", "헤드레스트", "메모리폼")):
        return "\n\n".join(
            [
                "차를 오래 타고 내렸는데\n목부터 천천히 돌리게 되는 날이 있음.",
                "운전할 땐 자세를 바꾸기도 어렵고\n헤드레스트와 목 사이가 계속 애매하게 남음.",
                "그 빈자리를 받쳐주는 방식이\n왜 장거리 전에 생각나는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("휴족", "쿨링시트", "발바닥", "종아리", "다리", "풋케어")):
        return "\n\n".join(
            [
                "하루 종일 걷거나 서 있던 날엔\n집에 와서 누워도 다리가 계속 존재감이 있음.",
                "발바닥이나 종아리를 잠깐 챙기는 일인데\n막상 귀찮으면 그냥 넘기기 쉬움.",
                "붙여두고 쉬는 방식이\n왜 이런 날 자꾸 눈에 들어오는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("선풍기", "쿨러", "냉각", "에어컨", "손풍기")):
        return "\n\n".join(
            [
                "여름엔 횡단보도 하나만 건너도\n얼굴에 열기가 먼저 올라오는 날이 있음.",
                "그늘을 찾아도 바람이 없으면\n잠깐 멈춘다고 금방 편해지진 않음.",
                "가까이 두고 바로 쓰는 방식이\n왜 외출 전에 생각나는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("썬쉐이드", "선루프", "차광", "햇빛", "그늘")):
        return "\n\n".join(
            [
                "여름에 차 문을 열었는데\n좌석보다 머리 위 열기가 먼저 느껴질 때가 있음.",
                "에어컨을 켜도 햇빛이 계속 들어오면\n차 안이 쉽게 편해지지 않음.",
                "위쪽을 가리는 방식이\n왜 여름마다 다시 생각나는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("강아지", "반려", "펫", "물티슈", "산책", "하네스")):
        return "\n\n".join(
            [
                "산책을 마치고 현관에 들어서면\n발바닥과 털부터 눈에 들어오는 날이 있음.",
                "집 안으로 들어간 뒤 다시 챙기려면 늦어서\n외출 가방에서 바로 꺼낼 수 있는지가 중요함.",
                "그 짧은 순간을 정리하는 방식이\n왜 산책할 때마다 생각나는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("신발", "운동화", "구두")) and _has_any(
        context, ("방수", "비 오는 날", "장마", "젖")
    ):
        return "\n\n".join(
            [
                "비 오는 날 현관에서\n밝은 신발을 신고 나가도 될지 잠깐 멈출 때가 있음.",
                "우산은 챙겨도 발끝은 그대로라서\n작은 웅덩이 하나가 계속 신경 쓰임.",
                "나가기 전에 미리 챙기는 방식이\n왜 장마철마다 눈에 들어오는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("우산", "레인", "부츠", "장마", "방수")):
        return "\n\n".join(
            [
                "비가 애매하게 오는 날엔\n큰 우산을 들고 갈지부터 고민하게 됨.",
                "가방 자리는 적게 쓰면서\n막상 펼쳤을 땐 몸을 제대로 가려야 함.",
                "작게 챙기고 넓게 쓰는 방식이\n왜 날씨를 볼 때마다 생각나는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("체중계", "몸무게")):
        return "\n\n".join(
            [
                "씻고 나오거나 옷을 갈아입을 때\n바닥 숫자를 한 번 확인하게 되는 순간이 있음.",
                "매일 챙기겠다고 마음먹어도\n과정이 번거로우면 며칠 지나 손이 안 가게 됨.",
                "눈에 잘 보이고 바로 확인되는 방식이\n왜 계속 생각나는지 조금 알 것 같음.",
            ]
        )
    if _has_any(context, ("보조배터리", "충전기", "무선충전", "맥세이프")):
        return "\n\n".join(
            [
                "밖에 나와서 배터리 숫자가 한 자리면\n그때부터 가방 속 케이블부터 찾게 됨.",
                "기기마다 선을 따로 챙기면\n충전보다 정리하는 일이 더 번거로울 때가 있음.",
                "가까이 두고 바로 채우는 방식이\n왜 외출할 때 눈에 들어오는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("방향제", "디퓨저", "향기", "냄새")):
        return "\n\n".join(
            [
                "문을 열었을 때 남아 있는 냄새는\n매일 타는 사람보다 가끔 탄 사람이 먼저 알아챔.",
                "향이 너무 강하면 그것대로 부담스럽고\n금방 사라지면 챙긴 의미가 없어짐.",
                "공간에 은근히 남는 방식이\n왜 더 오래 고민되는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("텀블러", "물병", "물컵", "보틀")):
        return "\n\n".join(
            [
                "물을 챙겨 나가려는데\n가방 안에서 세워둘 자리가 애매할 때가 있음.",
                "눕히자니 새는 게 걱정되고\n세우자니 다른 물건이 잘 안 들어감.",
                "들고 다니는 방식까지 편해야\n왜 자주 손이 가는지 알 것 같음.",
            ]
        )
    if _has_any(context, ("신발", "샌들", "슬리퍼", "클로그", "크록스")):
        return "\n\n".join(
            [
                "잠깐 나가는 날엔\n편하게 신을지 옷에 맞출지 현관에서 고민하게 됨.",
                "발이 편해도 모양이 아쉽거나\n보기엔 괜찮아도 오래 걷기 부담스러운 경우가 있음.",
                "그 중간을 찾는 방식이\n왜 신발장 앞에서 자꾸 생각나는지 알 것 같음.",
            ]
        )
    return "\n\n".join(
        [
            "자주 쓰는 물건인데도\n막상 필요한 순간엔 손에 바로 안 잡힐 때가 있음.",
            "기능이 많아서보다\n평소 불편한 한 장면을 줄여주는지가 더 중요함.",
            "별것 아닌 사용 방식이\n왜 계속 눈에 남는지 조금 알 것 같음.",
        ]
    )


def _threads_shock_post(relatable: str) -> str:
    paragraphs = [paragraph for paragraph in relatable.split("\n\n") if paragraph.strip()]
    context = paragraphs[0] if paragraphs else relatable
    detail = paragraphs[1] if len(paragraphs) > 1 else "평소에는 대수롭지 않게 넘기기 쉬운 불편임."
    return "\n\n".join(
        [
            "딱 한 번 제대로 불편해지면\n그다음부터는 모른 척하기 어려움.",
            context,
            detail,
            "문제는 해결 방식이 생각보다 단순해 보인다는 것.\n그래서 더 궁금해짐.",
        ]
    )


def _threads_story_post(relatable: str) -> str:
    paragraphs = [paragraph for paragraph in relatable.split("\n\n") if paragraph.strip()]
    context = paragraphs[0] if paragraphs else relatable
    detail = paragraphs[1] if len(paragraphs) > 1 else "필요한 순간마다 같은 자리에서 손이 멈춤."
    return "\n\n".join(
        [
            "“이거 매번 왜 이러지?”\n별일 아닌데 꼭 필요한 순간마다 같은 말이 나옴.",
            context,
            detail,
            "설명보다 그 장면이 먼저 떠오르는 걸 보면\n사람들이 찾는 이유가 따로 있는 듯함.",
        ]
    )


def _threads_viral_post(relatable: str) -> str:
    paragraphs = [paragraph for paragraph in relatable.split("\n\n") if paragraph.strip()]
    context = paragraphs[0] if paragraphs else relatable
    detail = paragraphs[1] if len(paragraphs) > 1 else "알고 보면 평소 불편과 바로 이어지는 방식임."
    return "\n\n".join(
        [
            "처음 보고 눈을 의심함.\n이런 방식이 여기 왜 있어?",
            context,
            detail,
            "별거 아닐 줄 알았는데 자꾸 생각남.\n이거 나만 이제 안 건가.",
        ]
    )


def _threads_honest_discovery_post(relatable: str) -> str:
    paragraphs = [paragraph for paragraph in relatable.split("\n\n") if paragraph.strip()]
    context = paragraphs[0] if paragraphs else relatable
    detail = paragraphs[1] if len(paragraphs) > 1 else "알고 보면 생활의 짧은 순간과 맞닿아 있음."
    return "\n\n".join(
        [
            "처음엔 굳이 이까지 싶었음.",
            context,
            detail,
            "쓰는 장면를 알고 나니\n왜 찾는지는 솔직히 조금 이해됨.",
        ]
    )


def _threads_conversion_post(relatable: str) -> str:
    paragraphs = [paragraph for paragraph in relatable.split("\n\n") if paragraph.strip()]
    context = paragraphs[0] if paragraphs else relatable
    detail = paragraphs[1] if len(paragraphs) > 1 else "반복되는 불편일수록 작은 차이가 더 크게 보임."
    return "\n\n".join(
        [
            "이 장면이 자주 반복된다면\n결국 보게 되는 건 복잡한 기능이 아님.",
            context,
            detail,
            "누군가에게는 별거 아닐 수 있지만\n딱 그 순간이 있는 사람은 쓰임새부터 눈에 들어옴.",
        ]
    )


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in text for term in terms)


def _threads_detail_sentence(product_name: str, facts: list[str]) -> str:
    selected = facts[:3]
    if not selected:
        return f"{product_name}{_topic_marker(product_name)} 상세 페이지에서 구성과 옵션을 확인해보고 고르면 좋습니다."
    if any(term in product_name for term in ("강아지", "반려", "펫", "물티슈")):
        portable = _find_fact(selected, ("소포장", "휴대", "20매", "20매입")) or selected[0]
        quantity = _find_fact(selected, ("구성", "팩", "개입", "20팩"))
        cleanup = _clean_usage_fact(_find_fact(facts, ("산책", "발", "털", "닦")))
        if quantity and cleanup:
            return f"{cleanup}할 때 쓰기 좋고, {portable}에 {quantity}이라 외출용으로 나눠 챙기기 편합니다."
        if cleanup:
            return f"{cleanup}할 때 쓰기 좋고, {portable}이라 외출용으로 챙기기 편합니다."
        return f"{portable}이라 산책 가방이나 외출 파우치에 넣어두기 좋습니다."
    if "테슬라" in product_name or "tesla" in product_name.lower():
        primary = selected[0]
        secondary = selected[1] if len(selected) > 1 else ""
        if secondary:
            return f"{primary} 제품이라 {secondary} 여부를 먼저 보고 고르면 좋습니다."
        return f"{primary} 용도로 필요한 분들이 먼저 비교해볼 만합니다."
    if any(term in product_name for term in ("우산", "레인", "부츠", "장우산")):
        return f"{selected[0]}처럼 비 오는 날 바로 쓰는 요소를 기준으로 보면 좋습니다."
    if len(selected) == 1:
        return f"{product_name}{_topic_marker(product_name)} {selected[0]} 부분을 먼저 볼 만합니다."
    if len(selected) == 2:
        return f"{product_name}{_topic_marker(product_name)} {selected[0]}, {selected[1]} 구성이 눈에 들어옵니다."
    return f"{product_name}{_topic_marker(product_name)} {selected[0]}, {selected[1]}, {selected[2]} 같은 부분을 보고 고르면 좋습니다."


def _threads_usage_sentence(product_name: str, facts: list[str]) -> str:
    joined = ", ".join(facts[:2])
    if any(term in product_name for term in ("강아지", "반려", "펫", "물티슈")):
        return "산책 후 발이나 털을 닦을 때, 외출 가방에 나눠 넣어두기 좋은 쪽으로 보면 됩니다."
    if "하네스" in product_name:
        return "산책할 때 착용감과 사이즈가 맞는지 먼저 보고 고르면 좋습니다."
    if "테슬라" in product_name:
        return "차량 모델 호환 여부와 실제 장착 위치를 먼저 맞춰보고 고르면 실패를 줄일 수 있습니다."
    if any(term in product_name for term in ("우산", "레인", "부츠", "장우산")):
        return "출근길이나 장마철 외출처럼 바로 써야 하는 상황에 맞춰 보면 좋습니다."
    if joined:
        return f"평소 사용 장면에서 {joined} 같은 부분이 필요한지 기준으로 보면 좋습니다."
    return "평소 쓰는 장면에 맞는지 먼저 생각해보고 고르면 좋습니다."


def _threads_check_sentence(product_name: str) -> str:
    if "테슬라" in product_name:
        return "구매 전에는 호환 모델, 장착 위치, 구성품을 꼭 확인해보세요."
    if any(term in product_name for term in ("강아지", "하네스")):
        return "구매 전에는 사이즈, 착용 방식, 반려견 체형에 맞는지 확인해보세요."
    if any(term in product_name for term in ("우산", "레인", "부츠", "장우산")):
        return "구매 전에는 사이즈, 소재, 휴대 방식을 확인해보세요."
    return "구매 전에는 구성, 사이즈, 사용 목적에 맞는지 확인해보세요."


def _find_fact(facts: list[str], terms: tuple[str, ...]) -> str:
    for fact in facts:
        if any(term in fact for term in terms):
            return fact
    return ""


def _clean_usage_fact(fact: str) -> str:
    cleaned = fact.strip()
    cleaned = cleaned.replace("에 사용", "")
    cleaned = cleaned.replace("으로 사용", "")
    return cleaned


def _normalize_facts(facts: list[str]) -> list[str]:
    normalized: list[str] = []
    for fact in facts:
        for part in re.split(r"ㆍ|(?<!\d)/(?!\d)|(?<!\d),(?!\d)", fact):
            cleaned = part.strip(" -•\n\t")
            if cleaned and not _is_low_value_fact(cleaned) and cleaned not in normalized:
                normalized.append(cleaned)
    return normalized


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _public_content_facts(facts: list[str]) -> list[str]:
    public_facts: list[str] = []
    for fact in facts:
        cleaned = _clean_source_phrasing(fact)
        if not cleaned or _is_commerce_fact(cleaned) or _mentions_price(cleaned):
            continue
        if cleaned not in public_facts:
            public_facts.append(cleaned)
    return public_facts


def _usage_sentence(product_name: str, facts: list[str]) -> str:
    marker = _subject_marker(product_name)
    product_specs = [fact for fact in facts if not _is_commerce_fact(fact)]
    selected = product_specs[:3] or facts[:3]
    joined = ", ".join(selected)
    if joined:
        if "마우스" in product_name:
            return f"{product_name}{marker} 문서 작업, 콘텐츠 편집, 긴 페이지 탐색처럼 스크롤과 버튼 조작이 잦은 환경에서 {joined} 같은 장점을 확인해볼 만합니다."
        return f"{product_name}{marker} {joined} 같은 특징을 실제 사용 장면에서 확인하고 선택하는 것이 좋습니다."
    return f"{product_name}{marker} 상품 상세를 자동으로 충분히 읽지 못했습니다. 정확한 활용 장면을 쓰려면 핵심 사양이나 사용 목적을 보강해야 합니다."


def _intro_sentence(product_name: str, facts: list[str], persona_hint: str) -> str:
    marker = _subject_marker(product_name)
    product_specs = [fact for fact in facts if not _is_commerce_fact(fact)]
    if "마우스" in product_name and product_specs:
        return f"{product_name}{marker} 정밀한 포인터 조작, 빠른 스크롤, 업무 흐름 커스터마이징을 중시하는 사용자에게 맞는 프리미엄 무선 마우스입니다."
    first_fact = product_specs[0] if product_specs else facts[0]
    return f"{product_name}{marker} {persona_hint} 기준으로 {first_fact}{_object_marker(first_fact)} 먼저 확인해볼 만한 상품입니다."


def _is_commerce_fact(fact: str) -> bool:
    commerce_terms = (
        "판매가",
        "정상가",
        "할인가",
        "가격",
        "쿠폰",
        "할인",
        "별점",
        "리뷰",
        "구매",
        "도착",
        "배송",
        "설치일",
        "설치 가능",
        "적립",
        "쿠팡캐시",
    )
    return any(term in fact for term in commerce_terms)


def _mentions_price(fact: str) -> bool:
    return bool(re.search(r"\d[\d,]*\s*원", fact))


def _clean_source_phrasing(fact: str) -> str:
    cleaned = fact.strip()
    cleaned = re.sub(r"^(쿠팡\s*)?상품\s*페이지\s*기준\s*", "", cleaned)
    cleaned = re.sub(r"^쿠팡\s*(확인\s*)?기준\s*", "", cleaned)
    cleaned = cleaned.replace("쿠팡 상품", "상품")
    return cleaned.strip(" -•\n\t")


def _is_low_value_fact(fact: str) -> bool:
    cleaned = fact.strip(" .!?\n\t")
    lowered = cleaned.lower()
    if not cleaned:
        return True
    if lowered in {"com", "www", "apple", "coupang"}:
        return True
    return any(
        phrase in lowered
        for phrase in (
            "com에서 구입",
            ".com에서 구입",
            "에서 구입하세요",
            "구입하세요",
            "구매하기",
            "확인하세요",
            "지금 쿠팡에서",
            "다양한 bar형 제품들을 확인",
        )
    )


def _object_marker(text: str) -> str:
    last = _last_korean_syllable(text)
    if last is None:
        return "을"
    return "을" if (ord(last) - 0xAC00) % 28 else "를"


def _subject_marker(text: str) -> str:
    last = _last_korean_syllable(text)
    if last is None:
        return "은"
    return "은" if (ord(last) - 0xAC00) % 28 else "는"


def _topic_marker(text: str) -> str:
    return _subject_marker(text)


def _last_korean_syllable(text: str) -> str | None:
    for char in reversed(text.strip()):
        if 0xAC00 <= ord(char) <= 0xD7A3:
            return char
    return None
