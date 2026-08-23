"""C2 글 생성 + 품질 게이트 — 주제를 받아 인용되기 좋은 정보형 글을 만든다.

템플릿·채점 기준의 근거는 docs/mate-analysis.md "C2 글 템플릿·품질 게이트 사양":
  템플릿 7요소 — ①첫 문단 즉답 ②H2/H3 위계 ③고정 스키마 표 ④통계·수치+출처
                 ⑤FAQ 절 ⑥기간·집계 기준 명시 ⑦발행일 명시
  게이트 7차원 — 사실성 / 구조 / 去AI味 / 키워드 스터핑 감점 / 두괄식 / 구체성 / 신선도

흐름(왕복 루프): 리서치(검색 API 스니펫) → 초안(gpt-4.1) → 게이트(gpt-5.4-mini 채점)
  → 게이트가 action을 지시한다:
    rewrite     — 피드백을 넣어 재작성 (기존 재생성과 동일)
    re_research — 소스 자체가 낡거나 부족 → 게이트가 준 보완 검색어로 재리서치 후 재작성
  → 최대 GATE_MAX_RETRIES회 재시도, 실패 시 스킵.

신선도는 사실성과 같은 단독 커트라인이다 — 2026-08-18 "2024년 기준" 글 2건이
사실성 게이트를 통과해 발행된 사고의 재발 방지 장치 (docs/status.md 신선도 버그).
"""

import json
import re
import sqlite3
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from src import config, db, llm
from src.naver_api import openapi_search

# 게이트 합격선 (100점 만점). 낮추는 것은 사람만 할 수 있다 (가드레일 ④의 연장)
GATE_PASS_SCORE = 70
# 신선도 단독 커트라인 — 평균이 높아도 낡은 정보 중심이면 불합격
FRESHNESS_PASS_SCORE = 60
# 이 나이(일)를 넘은 소스는 프롬프트에 '낡음'으로 표시해 중심 근거 사용을 막는다
STALE_DAYS = 365
# 왕복 루프에서 재리서치 최대 횟수 (LLM이 무한 재리서치를 지시하지 못하게)
# 2026-08-22 1→2 상향: 소스 부실형 탈락(소상공인지원금 — 지자체 기사만 잡힘)에
# 재리서치 1회로는 부족했던 실측 반영
RE_RESEARCH_MAX = 2

# 去AI味 — 네이버 블로그에서 'AI 티'로 읽히는 상투 표현들 (게이트가 감점)
AI_CLICHES = ["결론적으로", "종합해보면", "~하는 것이 중요합니다", "알아보도록 하겠습니다",
              "도움이 되셨기를", "지금까지 ~에 대해 알아보았습니다", "혁신적인", "게임 체인저"]


def _strip_tags(s: str) -> str:
    """검색 API 응답의 <b> 태그 등 HTML 제거."""
    return re.sub(r"<[^>]+>", "", s).replace("&quot;", '"').replace("&amp;", "&")


def _age_days(date_str: str) -> int | None:
    """소스 날짜 문자열 → 나이(일). 뉴스는 RFC822, 블로그는 YYYYMMDD. 실패·없음은 None."""
    s = (date_str or "").strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{8}", s):  # 블로그 postdate
            d = datetime.strptime(s, "%Y%m%d")
        else:  # 뉴스 pubDate — 로케일 무관 파서 사용
            d = parsedate_to_datetime(s).replace(tzinfo=None)
        return max((datetime.now() - d).days, 0)
    except (ValueError, TypeError):
        return None


def _age_label(age: int | None) -> str:
    """프롬프트에 넣을 나이 표기 — 낡은 소스는 눈에 띄게 경고를 단다."""
    if age is None:
        return "날짜 미상"
    if age <= 60:
        return f"{age}일 전"
    if age <= STALE_DAYS:
        return f"약 {age // 30}개월 전"
    return f"약 {age // 365}년 전 ⚠낡음"


def gather_research(keyword: str) -> list[dict]:
    """검색 API(뉴스·웹문서·블로그)에서 사실 스니펫을 모은다.

    v1은 스니펫 수준 — 본문 전문 수집(정밀 리서치)은 측정 결과를 보고 보강한다.
    순위류 등 공식 데이터가 있는 주제는 커넥터 소스가 맨 앞([1])에 붙는다.
    """
    from src.connectors import official_sources  # 지연 임포트 (순환 방지)
    sources = official_sources(keyword)
    for kind, n in (("news", 6), ("webkr", 6), ("blog", 4)):
        try:
            for item in openapi_search(kind, keyword, display=n).get("items", []):
                date_str = item.get("pubDate", item.get("postdate", ""))
                sources.append({
                    "kind": kind,
                    "title": _strip_tags(item.get("title", "")),
                    "snippet": _strip_tags(item.get("description", "")),
                    "url": item.get("link", ""),
                    "date": date_str,
                    "age_days": _age_days(date_str),
                })
        except Exception:
            continue  # 한 소스가 죽어도 나머지로 진행
    return sources


def merge_research(base: list[dict], extra: list[dict], cap: int = 16) -> list[dict]:
    """재리서치 결과를 기존 소스와 합친다 — URL 중복 제거, 최신 우선, cap개 제한."""
    seen, merged = set(), []
    for s in sorted(base + extra, key=lambda s: s["age_days"] if s["age_days"] is not None else 9999):
        if s["url"] and s["url"] in seen:
            continue
        seen.add(s["url"])
        merged.append(s)
    return merged[:cap]


def _src_block(research: list[dict], with_url: bool = True) -> str:
    """프롬프트용 소스 목록 — 날짜·낡음 경고를 항상 함께 보여준다."""
    return "\n".join(
        f"[{i+1}] ({s['kind']}, {_age_label(s.get('age_days'))}) {s['title']} — {s['snippet']}"
        + (f" ({s['url']})" if with_url else "")
        for i, s in enumerate(research)
    )


def gate_fail_summary(gate: dict) -> str:
    """게이트 탈락 사유 — 텔레그램 통보와 posts.skip_reason이 공용으로 쓴다.

    차원별 감점 사유(low_reasons)를 붙여 "무엇이 왜 낮았는지"가 바로 보이게 한다
    (2026-08-22: '스터핑 55'만 보고 무슨 문제인지 알 수 없던 보고의 개선).
    전문은 gate_json에 있고, 텔레그램은 분할 발송이라 잘리지 않는다.
    """
    names = {"factual": "사실성", "structure": "구조", "deai": "AI문체", "stuffing": "키워드반복",
             "frontload": "두괄식", "specificity": "구체성", "freshness": "신선도"}
    scores = gate.get("scores", {})
    reasons = scores.get("low_reasons") or {}
    # 합격선(70) 아래인 차원을 낮은 순으로 최대 3개 — 무엇이 발목을 잡았는지
    low = sorted(((k, float(scores.get(k, 0))) for k in names
                  if float(scores.get(k, 0)) < GATE_PASS_SCORE), key=lambda x: x[1])
    parts = []
    for k, v in low[:3]:
        # 사유는 자르지 않는다 — 문장 중간 절단이 "잘려서 옴" 보고의 원인이었다.
        # 길이는 텔레그램 분할 발송(notify._split)이 책임진다 (2026-08-23 절단 제거)
        why = str(reasons.get(k) or reasons.get(names[k]) or "").strip()
        parts.append(f"{names[k]} {int(v)}" + (f" — {why}" if why else ""))
    dims = "\n  · ".join(parts) or "없음 (총점 미달)"
    return (f"총점 {gate.get('total')}, 미달 차원:\n  · {dims}\n"
            f"  종합: {gate.get('feedback', '')}")


def write_draft(topic: dict, research: list[dict], feedback: str | None = None,
                hint: str | None = None) -> str:
    """초안 작성 — 작문 모델(gpt-4.1)에 템플릿 7요소를 강제한다.

    hint: 해결 루프(resolver)가 정책에 주입한 작문 지침 — 검수 반복 지적을
    글 생성 단계에서 교정하는 경로 (있을 때만 프롬프트에 덧붙는다).
    """
    src_block = _src_block(research)
    # 분야별 조정 (mate-analysis: 테크·인사이트=통계·출처↑, 라이프·푸드·여행=가독성↑)
    tone = ("통계·수치와 출처 인용의 밀도를 높여라"
            if topic.get("category") in ("테크", "인사이트", "미디어")
            else "쉬운 설명과 읽기 편한 흐름을 우선하되 수치는 정확히 써라")

    today = date.today()
    prompt = f"""네이버 블로그에 올릴 정보 정리·분석형 글을 작성하라. 주제: "{topic['keyword']}" (분야: {topic.get('category', '일반')})
오늘 날짜: {today:%Y-%m-%d}

목표: 네이버 AI 브리핑이 인용하기 좋은 글. 아래 9요소를 반드시 지켜라
(1~7은 GEO 실증, 8~9는 네이버 공식 셀프 체크 가이드 반영):
1. 첫 문단에서 검색 의도에 바로 답한다 (두괄식 즉답 — 서론·인사말 금지)
2. H2/H3 헤딩 위계 (마크다운 ## / ###)
3. 핵심 정보를 정리한 표 최소 1개 (동일 스키마, 마크다운 표)
4. 통계·수치를 쓸 때마다 아래 리서치 소스 번호로 출처 표기 — 예: (출처: [3])
5. 마지막에 FAQ 절 (## 자주 묻는 질문, Q 3개 이상) — Q는 실제 검색될 법한
   **구체 질문**(조건·예외·계산·서류·차이)으로 쓰고 각각 한 문단 안에 즉답한다.
   AI 브리핑은 공식 페이지가 즉답 못 하는 구체 질문에서 블로그를 인용한다 (실측 2026-08-22)
6. 정보의 기준 시점은 오늘 날짜 기준으로 명시한다 ("{today.year}년 {today.month}월 기준") —
   제목·본문에 과거 연도를 기준 시점으로 달지 않는다 ("2024년 기준" 금지)
7. {tone}
8. 첫 문단 직후에 이 글이 누구에게, 어떤 상황에서 유용한지 한두 문장으로 밝힌다 (TPO)
9. 상황별 추천 절을 넣는다 — "~한 경우엔 A, ~한 경우엔 B"처럼 독자 상황에 따라
   선택지를 갈라 주고 각 선택의 결정적 이유를 적는다 (단순 나열 금지)

금지사항:
- 리서치 소스에 없는 사실을 지어내지 않는다 (모르면 "확인 필요"로 남긴다)
- 경험담·후기 톤 금지 ("제가 써보니" 등) — 정보 정리자의 톤
- AI 상투어 금지: {", ".join(AI_CLICHES)}
- 같은 키워드를 기계적으로 반복하지 않는다 (네이버 공식 스팸 유형)
- 리서치 소스의 문장을 그대로 복사하지 않는다 — 반드시 내 문장으로 재구성
  (기계적 변형 게시는 네이버 공식 '스크래핑' 스팸 유형)
- 제목과 본문 내용이 어긋나는 낚시성 제목 금지
- ⚠낡음 표시(1년 이상 지난) 소스를 글의 중심 근거로 쓰지 않는다 — 배경 설명에만
  제한적으로 쓰되 그 시점을 명시하고, 최신 소스와 상충하면 최신 쪽을 따른다

리서치 소스 — (official) 표시는 공식 데이터다: 순위·수치는 반드시 이것을 기준으로 쓰고,
다른 소스와 상충하면 official을 따른다:
{src_block}

{f'이전 초안의 게이트 불합격 피드백 — 반드시 고쳐라: {feedback}' if feedback else ''}
{f'운영 정책 추가 지침 (블로그 검수 반복 지적의 교정 — 반드시 반영): {hint}' if hint else ''}

출력: 첫 줄에 "제목: <제목>", 둘째 줄부터 마크다운 본문."""
    return llm.chat(config.MODEL_WRITER, prompt, purpose="write-draft")


def gate_draft(draft: str, topic: dict, research: list[dict]) -> dict:
    """품질 게이트 — 판단 모델이 7차원 채점 + 다음 행동 지시.

    반환: {scores, total, passed, feedback, action, research_query}
      action: "pass" | "rewrite"(재작성으로 고칠 수 있음) | "re_research"(소스가 낡거나 부족)
    """
    today = date.today()
    src_block = _src_block(research, with_url=False)
    prompt = f"""다음 블로그 초안을 채점하라. 주제: "{topic['keyword']}" / 오늘 날짜: {today:%Y-%m-%d}

채점 기준 (각 0~100):
- factual: 본문의 사실·수치가 리서치 소스와 부합하나? 소스에 없는 주장에는 감점
- structure: 두괄식 즉답 / H2·H3 / 표 / FAQ / 기준 시점 / TPO(누구에게 유용한지) /
  상황별 추천·대안 비교 절이 갖춰졌나
- deai: AI 상투어·기계적 문체가 없고, 리서치 소스 문장을 그대로 베끼지 않았나
  (복사·기계적 변형은 네이버 스팸 유형 — 발견 시 크게 감점)
- stuffing: 키워드 기계 반복이 없고, 제목이 본문 내용과 일치하나 (낚시성 감점)
- frontload: 첫 문단이 검색 의도에 즉답하나
- specificity: 모호한 서술 대신 구체 수치·근거가 있나
- freshness: 오늘({today:%Y-%m-%d}) 발행하기에 정보가 현재적인가? **다음 세 가지만 본다**:
  ①제목·본문의 기준 시점이 과거 연도로 표기("2024년 기준" 등) ②중심 근거가
  ⚠낡음(1년+) 소스에 의존 ③낡은 정보를 최신인 것처럼 서술.
  "공식 소스로 재확인 필요" 같은 사실 신뢰 문제는 factual에서만 감점하라 —
  freshness에 이중 감점 금지 (셋 다 해당 없으면 freshness는 85 이상)

점수 앵커 — 모든 차원 공통. 구체적인 문제 문장·수치를 지적할 수 없는 차원에
낮은 점수를 주지 마라 (근거 없는 중간대 점수 금지):
- 90~100: 그 차원에 감점 요소가 없다
- 75~89: 사소한 흠 1~2개 (합격권)
- 60~74: 뚜렷한 문제가 있으나 재작성으로 교정 가능 — low_reasons에 사유 필수
- 60 미만: 심각 — 그 차원 때문에 발행 불가 — low_reasons에 사유 필수

다음 행동도 지시하라:
- 합격 수준이면 action: "pass"
- 초안만 고치면 되면 action: "rewrite"
- 소스 자체가 낡았거나 핵심 정보가 소스에 없어 재작성으로 해결이 안 되면
  action: "re_research" + research_query에 보완 검색어 (예: "{topic['keyword']} {today.year}")

리서치 소스:
{src_block}

초안:
{draft[:6000]}

JSON만 출력:
{{"factual": 0, "structure": 0, "deai": 0, "stuffing": 0, "frontload": 0, "specificity": 0,
 "freshness": 0,
 "low_reasons": {{"75 미만을 준 차원명(영문 키)": "감점 사유 한 줄 — 어떤 문장·수치가 문제인지 구체적으로"}},
 "action": "pass|rewrite|re_research", "research_query": "",
 "feedback": "불합격 원인과 고칠 점을 구체적으로 (합격이어도 개선점 한 줄)"}}
(low_reasons의 사유와 점수는 서로 모순되면 안 된다 — "반복 심하지 않음"이라 쓰고
낮은 점수를 주는 식 금지. 사유가 없으면 점수를 올려라)"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="quality-gate")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    scores = json.loads(raw)
    dims = ["factual", "structure", "deai", "stuffing", "frontload", "specificity", "freshness"]
    total = sum(float(scores.get(d, 0)) for d in dims) / len(dims)
    # 사실성·신선도는 단독 커트라인 — 평균이 높아도 이 둘이 낮으면 불합격
    # (사실성: 허위 정보 방지 / 신선도: "2024년 기준" 발행 사고 재발 방지)
    passed = (total >= GATE_PASS_SCORE
              and float(scores.get("factual", 0)) >= 60
              and float(scores.get("freshness", 0)) >= FRESHNESS_PASS_SCORE)
    return {"scores": scores, "total": round(total, 1), "passed": passed,
            "feedback": scores.get("feedback", ""),
            "action": "pass" if passed else (scores.get("action") or "rewrite"),
            "research_query": scores.get("research_query", "")}


# 대표 이미지 스타일 풀 — 글마다 다른 스타일이 걸리게 한다 (검수 반복 지적
# "썸네일이 유사 톤 일러스트로 획일 반복"의 근본 수정, 2026-08-22).
# 선택은 글 파일명(stem) 해시 기반 — 같은 글은 항상 같은 스타일(재현성), 글마다 다름
HERO_STYLES = [
    "미니멀 플랫 일러스트, 넉넉한 여백",
    "아이소메트릭(입체 사선 시점) 일러스트, 정돈된 구성",
    "종이 콜라주 질감의 일러스트, 레이어 느낌",
    "부드러운 그라데이션 배경 위 단순 오브젝트 3D 렌더 느낌",
    "손그림 라인 드로잉에 부분 채색, 여백 많은 구성",
    "기하학 도형 패턴 중심의 추상 일러스트",
]
HERO_PALETTES = {
    "여행": "청록·모래 베이지 톤", "푸드": "따뜻한 주황·크림 톤", "레시피": "밝은 그린·크림 톤",
    "스타일": "뮤트 핑크·그레이 톤", "테크": "딥블루·시안 톤", "라이프": "옐로·민트 톤",
    "컬쳐": "버건디·아이보리 톤", "미디어": "퍼플·다크블루 톤", "인사이트": "네이비·골드 톤",
    "취미": "오렌지·스카이블루 톤",
}


def make_hero_image(title: str, category: str, stem: str,
                    style_hint: str | None = None) -> str | None:
    """대표 이미지 1장 생성·저장 — 실패해도 글 발행은 계속 (치명 요소 아님).

    이미지 안에 글자를 넣지 않는다 — 생성 모델의 한글 렌더링이 불안정해
    깨진 글자가 오히려 신뢰도를 깎는다 (검수 에이전트 지적 사항이기도 함).
    스타일은 HERO_STYLES 풀에서 글마다 다르게 선택 + 분야별 팔레트 (획일화 방지).
    style_hint: 해결 루프(resolver)가 정책에 주입한 추가 지침 (풀 위에 덧입힘).
    """
    import zlib  # 안정 해시 — 프로세스가 바뀌어도 같은 글은 같은 스타일
    pick = HERO_STYLES[zlib.crc32(stem.encode("utf-8")) % len(HERO_STYLES)]
    palette = HERO_PALETTES.get(category, "차분한 파스텔 톤")
    style = (f"{palette}의 {pick}, 블로그 대표 이미지. "
             "글자·텍스트·숫자 절대 없음, 브랜드 로고·상표 절대 없음, 깔끔한 구성")
    if style_hint:
        style += f". 추가 스타일 지침: {style_hint}"
    try:
        img = llm.generate_image(f"{style}. 주제: {title} ({category} 분야)",
                                 purpose="hero-image", size="1536x1024")
        config.ensure_dirs()
        path = config.IMAGES_DIR / f"{stem}.png"
        path.write_bytes(img)
        return str(path)
    except Exception as e:
        print(f"대표 이미지 생성 실패 (글은 이미지 없이 진행): {type(e).__name__}: {e}")
        return None


def generate(topic: dict, conn: sqlite3.Connection | None = None) -> dict:
    """주제 하나 → 리서치 → 초안 → 게이트 (재시도 포함) → posts 기록.

    반환: {status: 'gated'|'skipped', post_id, body_path, gate, attempts}
    """
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        research = gather_research(topic["keyword"])
        if len(research) < 3:
            reason = f"리서치 소스 부족 ({len(research)}건 < 3)"
            # 스킵도 posts에 행을 남긴다 — 사유가 DB에서 조회되게 (2026-08-22)
            conn.execute(
                "INSERT INTO posts (topic_id, title, status, skip_reason) "
                "VALUES (?, ?, 'skipped', ?)",
                (topic.get("id"), topic["keyword"], reason))
            conn.commit()
            return {"status": "skipped", "reason": reason}

        # 해결 루프(resolver)가 정책에 주입한 힌트 — 반복 지적을 생성 단계에서 교정
        from src.steering import load_policy  # 지연 임포트 (순환 방지)
        policy = load_policy(conn)
        writer_hint = policy.get("writer_hint")
        image_hint = policy.get("image_style_hint")

        draft, gate, feedback = "", None, None
        attempts, re_researched = 0, 0
        for attempts in range(1, config.GATE_MAX_RETRIES + 2):  # 최초 1회 + 재시도
            draft = write_draft(topic, research, feedback, hint=writer_hint)
            gate = gate_draft(draft, topic, research)
            if gate["passed"]:
                break
            feedback = gate["feedback"]
            # 왕복 루프 — 게이트가 소스 문제로 판정하면 재작성 전에 재리서치
            if gate["action"] == "re_research" and re_researched < RE_RESEARCH_MAX:
                re_researched += 1
                query = gate["research_query"] or f"{topic['keyword']} {date.today().year}"
                research = merge_research(gather_research(query), research)
                feedback += f" (소스를 '{query}'로 재리서치해 최신 자료로 교체했다 — 새 소스 번호 기준으로 다시 써라)"

        title = draft.splitlines()[0].removeprefix("제목:").strip() if draft else topic["keyword"]
        body = "\n".join(draft.splitlines()[1:]).strip()

        if not gate["passed"]:
            reason = f"게이트 {attempts}회 불합격 — {gate_fail_summary(gate)}"
            conn.execute(
                "INSERT INTO posts (topic_id, title, gate_json, status, skip_reason) "
                "VALUES (?, ?, ?, 'skipped', ?)",
                (topic.get("id"), title, json.dumps(gate, ensure_ascii=False), reason),
            )
            conn.commit()
            return {"status": "skipped", "reason": reason,
                    "gate": gate, "attempts": attempts, "re_researched": re_researched}

        # 합격 — 본문 파일 저장 + 대표 이미지 + posts 기록 (발행은 C3의 일)
        config.ensure_dirs()
        safe_kw = re.sub(r"[^\w가-힣]", "_", topic["keyword"])
        body_path = config.POSTS_DIR / f"{today}-{safe_kw}.md"
        body_path.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        hero = make_hero_image(title, topic.get("category", "일반"), f"{today}-{safe_kw}",
                               style_hint=image_hint)

        cur = conn.execute(
            "INSERT INTO posts (topic_id, title, body_path, images_json, gate_json, "
            "sources_json, status) VALUES (?, ?, ?, ?, ?, ?, 'gated')",
            (topic.get("id"), title, str(body_path),
             json.dumps([hero] if hero else []),
             json.dumps(gate, ensure_ascii=False),
             json.dumps(research, ensure_ascii=False)),
        )
        conn.commit()
        return {"status": "gated", "post_id": cur.lastrowid, "title": title,
                "body_path": str(body_path), "image": hero, "gate": gate,
                "attempts": attempts, "re_researched": re_researched}
    finally:
        if own:
            conn.close()
