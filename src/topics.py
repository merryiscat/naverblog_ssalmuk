"""C1 주제 발굴 — 오늘 쓸 주제를 데이터로 고른다.

흐름 (usecases.md C1):
  ① 시드 키워드(보정 정책이 관리, 없으면 기본값)를 검색광고 키워드도구에 넣어
     연관키워드 수백 개로 확장 — 월간 검색량 확보
  ② 검색량 상위 후보만 검색 API로 블로그 문서수 조회 (쿼리 절약)
  ③ 골든키워드 점수 = 월간 검색량 ÷ 문서수 (수요 대비 공급이 빈 곳)
  ④ LLM(판단 모델)이 '정보형 글로 쓸 가치'를 채점 — 골든 점수 상위만 투입
  ⑤ 최종 3개 selected(config.DAILY_SELECT_COUNT), 다음 2개 reserve(config.RESERVE_COUNT)로
     topics 테이블에 기록 — 게이트 탈락 시 스케줄러가 예비 투입
     (2026-08-22 사용자 결정: 정책·지원금형 2 + 탐색 1 부분 특화, 예비 1→2 확대 —
     "하루 2건은 실제로 올라가야 한다". 8/21 게이트 전멸이 계기)
"""

import json
import re
import sqlite3
from datetime import date

from src import config, db, llm
from src.naver_api import doc_count, keyword_stats, openapi_search

# 네이버 메이트 공식 10개 분야 (보도자료 2026-07-15) — 선정이 분야별로 이뤄지므로
# 시드를 이 체계에 정렬한다. 시드는 콜드 스타트용이며,
# 운영이 시작되면 일일 보정(C5)이 policy로 시드·타깃 분야를 조정한다.
# 2026-08-22 부분 특화: 라이프·인사이트에 정책·지원금 시드 확대 — 실측 근거:
# AI 브리핑 신호 2건(소상공인정책자금·희망리턴패키지)이 모두 이 계열,
# 게이트 통과작도 정책류 위주. 대상별(청년/소상공인/육아/주거) 변형으로 소재 고갈 방지.
DEFAULT_SEEDS_BY_CATEGORY = {
    "여행": ["국내여행", "해외여행준비물"],
    "푸드": ["맛집추천", "제철음식"],
    "레시피": ["자취요리", "에어프라이어요리"],
    "스타일": ["여름코디", "패션기초"],
    "테크": ["노트북추천", "스마트홈"],
    "라이프": ["정부지원금", "청년지원금", "소상공인지원", "육아지원금", "자취꿀팁"],
    "컬쳐": ["전시회추천", "독서모임"],
    "미디어": ["넷플릭스추천", "드라마정보"],
    "인사이트": ["재테크기초", "청약방법", "주거지원정책", "고용지원금"],
    "취미": ["캠핑초보", "홈트레이닝"],
}

# 정책·지원금이 주로 태그되는 분야 — 롱테일 확장의 씨앗을 여기서 고른다
POLICY_CATEGORIES = ("라이프", "인사이트")
# 롱테일 확장 목표 수, 롱테일 경쟁 상한(이보다 문서 많으면 롱테일 의미 없음),
# 브리핑 확인 예산, 브리핑 캐시 유효기간(일)
LONGTAIL_TARGET = 16
LONGTAIL_MAX_DOCS = 30_000
BRIEFING_BUDGET = 12
BRIEFING_CACHE_DAYS = 14

# 쿼리 예산 — 문서수 조회(검색 API)는 검색량 상위 이 개수만
DOC_COUNT_BUDGET = 40
# 한 분야가 후보를 도배하지 못하게 분야별 상한 (첫 실행에서 "~가볼만한곳" 도배 확인)
PER_CATEGORY_CAP = 6
# LLM 채점에 올리는 후보 수 (골든 점수 상위)
LLM_CANDIDATES = 20


def _to_int(v) -> int:
    """검색광고 API의 검색량 값 정리 — '< 10' 같은 문자열은 5로 취급."""
    if isinstance(v, int):
        return v
    try:
        return int(str(v).replace("<", "").replace(",", "").strip()) or 5
    except ValueError:
        return 5


def get_seeds(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """분야별 시드 — 최신 policy에 있으면 그것을, 없으면 기본값.

    policy가 target_categories를 지정하면 그 분야만 발굴한다
    (보정 루프가 잘 되는 분야로 수렴할 때 사용 — C-rank 전문성 축적).
    """
    row = conn.execute("SELECT policy_json FROM policy ORDER BY id DESC LIMIT 1").fetchone()
    seeds = DEFAULT_SEEDS_BY_CATEGORY
    if row:
        p = json.loads(row["policy_json"])
        seeds = p.get("seeds_by_category") or seeds
        targets = p.get("target_categories")
        if targets:
            seeds = {c: kws for c, kws in seeds.items() if c in targets}
    return seeds


def recent_keywords(conn: sqlite3.Connection, days: int = 30) -> set[str]:
    """최근에 이미 다룬(선정된) 키워드 — 중복 주제 방지."""
    rows = conn.execute(
        "SELECT keyword FROM topics WHERE status IN ('selected', 'used') "
        "AND date >= date('now', 'localtime', ?)", (f"-{days} days",)
    ).fetchall()
    return {r["keyword"] for r in rows}


def expand_candidates(seeds_by_cat: dict[str, list[str]]) -> dict[str, dict]:
    """분야별 시드를 연관키워드로 확장. 반환: {키워드: {volume, category}}."""
    out: dict[str, dict] = {}
    for category, seeds in seeds_by_cat.items():
        for i in range(0, len(seeds), 5):  # 키워드도구는 한 번에 최대 5개
            for row in keyword_stats(seeds[i:i + 5]):
                kw = row["relKeyword"].strip()
                vol = _to_int(row.get("monthlyPcQcCnt", 0)) + _to_int(row.get("monthlyMobileQcCnt", 0))
                if len(kw) >= 2 and vol >= 300:  # 검색량이 너무 작으면 인용 기회도 작다
                    if kw not in out or vol > out[kw]["volume"]:
                        out[kw] = {"volume": vol, "category": category}
    return out


def score_golden(candidates: dict[str, dict], skip: set[str]) -> list[dict]:
    """검색량 상위 후보의 문서수를 조회해 골든 점수를 매긴다.

    분야별 상한(PER_CATEGORY_CAP)으로 한 분야의 도배를 막는다.
    """
    ranked = sorted(candidates.items(), key=lambda x: -x[1]["volume"])
    out, per_cat = [], {}
    for kw, info in ranked:
        if len(out) >= DOC_COUNT_BUDGET:
            break
        cat = info["category"]
        if kw in skip or per_cat.get(cat, 0) >= PER_CATEGORY_CAP:
            continue
        per_cat[cat] = per_cat.get(cat, 0) + 1
        docs = doc_count(kw)
        out.append({
            "keyword": kw, "volume": info["volume"], "docs": docs,
            "golden": info["volume"] / max(docs, 1), "category": cat,
        })
    out.sort(key=lambda c: -c["golden"])
    return out


def _policy_category(kw: str) -> str:
    """롱테일 키워드의 분야 추정 — 고용·취업 계열은 인사이트, 나머지 정책은 라이프."""
    if any(t in kw for t in ("취업", "실업", "고용", "근로", "일자리", "채용", "연금", "청약", "재테크")):
        return "인사이트"
    return "라이프"


def expand_longtail_questions(head_keywords: list[str]) -> list[dict]:
    """정책·지원금 헤드 키워드를 '구체 질문형 롱테일'로 확장한다 (LLM).

    lab.md 원칙 2/3: 헤드 키워드는 AI 브리핑이 공식기관만 인용하지만, 공식이 한 문장으로
    답 못 하는 구체 질문(조건·예외·계산·서류·차이·기간·중복·불이익)은 경쟁이 얇고
    블로그가 인용된다. 우리가 이길 수 있는 유일한 자리 (2026-08-26 사용자 전략 확정).
    반환: [{keyword, head, why}] — doc_count·score는 이후 단계가 채운다.
    """
    if not head_keywords:
        return []
    listing = ", ".join(head_keywords[:15])
    prompt = f"""너는 네이버 블로그 주제 발굴가다. 아래 정책·지원금 헤드 키워드에서
실제 검색될 법한 '구체 질문형' 세부 주제를 만들어라.

헤드 키워드(경쟁 수십만 개라 이대로는 AI 브리핑 인용을 못 받는다): {listing}

목표: AI 브리핑이 '블로그'를 인용하는 자리 = 공식 페이지가 한 문장으로 답 못 하는
구체 질문. 조건/예외/계산/서류/차이/기간/중복/불이익/거절사유 등을 담아라. 예:
- 실업급여 → "실업급여 조기재취업수당 조건", "실업급여 이직확인서 안 나올 때", "실업급여 부정수급 처벌"
- 국민취업지원제도 → "국민취업지원제도 1유형 2유형 차이", "국민취업지원제도 소득 재산 기준"
- 소상공인지원금 → "소상공인 정책자금 거절 사유", "소상공인 대환대출 조건"

규칙:
- 검색창에 칠 법한 짧은 질문구 (띄어쓰기 포함, 대략 8~20자)
- 일반형('~신청방법', '~총정리')은 금지 — 반드시 구체적 조건/상황/예외를 담아라
- 서로 다른 헤드·상황으로 폭넓게 {LONGTAIL_TARGET}개

JSON 배열만: [{{"keyword": "...", "head": "원 헤드 키워드", "why": "왜 구체 질문인지 한 줄"}}]"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="longtail-expand")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    out, seen = [], set()
    for item in json.loads(raw):
        kw = str(item.get("keyword", "")).strip()
        if len(kw) >= 5 and kw not in seen:
            seen.add(kw)
            out.append({"keyword": kw, "head": item.get("head", ""), "why": item.get("why", "")})
    return out


def score_longtail(longtails: list[dict], skip: set[str],
                   max_docs: int = LONGTAIL_MAX_DOCS) -> list[dict]:
    """롱테일 후보에 doc_count(경쟁)를 붙이고 경쟁이 얇은 것만 candidate 형식으로.

    검색량은 롱테일이라 대개 미상 → 하한값으로 두고, 얇은 경쟁(doc_count↓)이 골든 점수를
    끌어올리게 한다. max_docs를 넘으면 버린다(예측 주제는 신선도로 보상하니 상한을 더 높게).
    """
    out = []
    for lt in longtails:
        kw = lt["keyword"]
        if kw in skip:
            continue
        try:
            docs = doc_count(kw)
        except Exception:
            continue
        if docs > max_docs:
            continue  # 경쟁이 두꺼우면 롱테일이 아니다 — 스킵
        out.append({
            "keyword": kw, "volume": 300, "docs": docs,  # 검색량 미상 → 하한값
            "golden": 300 / max(docs, 1), "category": _policy_category(kw),
            "is_longtail": True, "head": lt.get("head", ""),
        })
    out.sort(key=lambda c: c["docs"])  # 경쟁 얇은 순
    return out


# 월말 이 날짜(포함)부터 다음 달 예측 주제를 발굴에 추가한다 (신선도 선점)
NEXT_MONTH_PREVIEW_FROM_DAY = 28


def expand_next_month_forward(when: date | None = None) -> list[dict]:
    """월말에 '다음 달 기준'으로 새로 시행·접수·변경되는 정책·지원금을 미리 예측 발굴한다.

    다음 달 검색이 급증하기 전에 신선한 글을 올려 AI 브리핑 인용을 선점하는 베팅
    (mate-analysis '영화구름' 스나이핑 사례 — 브리핑이 낡은 소스를 사과하며 인용하던 공백을
    신선한 글로 가져온다). 2026-08-28 사용자 요청.

    실제 'N월부터 달라지는'·'N월 시행/신청' 뉴스로 근거화한 뒤 LLM이 구체 질문형 롱테일 생성.
    반환: [{keyword, head, why}] — doc_count·score는 score_longtail이 채운다.
    """
    today = when or date.today()
    nm = today.month % 12 + 1  # 다음 달
    # 뉴스 근거 — 'N월부터 달라지는' 류 요약 기사가 월 전환 변경의 금맥
    heads = []
    for q in (f"{nm}월부터 달라지는", f"{nm}월 시행 지원금", f"{nm}월 신청 정책", f"{nm}월 접수"):
        try:
            for it in openapi_search("news", q, display=5).get("items", []):
                t = re.sub(r"<[^>]+>", "", it.get("title", ""))
                if t and t not in heads:
                    heads.append(t)
        except Exception:
            continue
    news_block = "\n".join(f"- {h}" for h in heads[:18]) or "(뉴스 근거 없음 — 매년 반복되는 정기 정책 위주로)"
    prompt = f"""오늘은 {today}, 곧 {nm}월이다. {nm}월부터 새로 시행·접수 시작되거나 기준·금액이
바뀌는 정책·지원금 중, 사람들이 '{nm}월 기준'으로 검색할 **구체 질문형** 주제를 예측하라.

참고 뉴스({nm}월 실제 변경):
{news_block}

규칙:
- 구체 질문형(신청기간·달라지는 점·대상·금액 변경·조건 등) + 정책·지원금 위주 (우리 승부처)
- {nm}월에 실제로 검색 수요가 생길 것에 한정 — 신규 접수·기준 변경·정기 이벤트·명절(추석) 등
- 근거 없는 상상 금지: 위 뉴스나 매년 반복되는 정기 정책에 기반하라
- 검색창에 칠 법한 짧은 질문구(8~20자), 서로 다른 주제로 {LONGTAIL_TARGET}개

JSON 배열만: [{{"keyword": "...", "why": "왜 {nm}월에 검색이 뜰지 한 줄"}}]"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="next-month-forward")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    out, seen = [], set()
    for item in json.loads(raw):
        kw = str(item.get("keyword", "")).strip()
        if len(kw) >= 5 and kw not in seen:
            seen.add(kw)
            out.append({"keyword": kw, "head": f"{nm}월 예측", "why": item.get("why", "")})
    return out


def check_briefing(conn: sqlite3.Connection, keywords: list[str]) -> dict[str, bool]:
    """키워드별 AI 브리핑 노출 여부 — 캐시(keyword_meta) 우선, 미확인만 브라우저로.

    lab.md 원칙 1: 브리핑이 안 뜨는 키워드는 인용 기회가 0이다 → 발행 전 컷의 근거.
    브리핑 유무는 안정적이라 BRIEFING_CACHE_DAYS 동안 캐시를 재사용한다.
    브라우저는 비로그인 — 브리핑은 로그인 없이도 보여 세션 위험이 없다 (04시 실행 안전).
    """
    result: dict[str, bool] = {}
    to_check = []
    for kw in keywords:
        row = conn.execute(
            "SELECT has_briefing FROM keyword_meta WHERE keyword = ? "
            "AND checked_at >= datetime('now', 'localtime', ?)",
            (kw, f"-{BRIEFING_CACHE_DAYS} days")).fetchone()
        if row is not None and row["has_briefing"] is not None:
            result[kw] = bool(row["has_briefing"])
        else:
            to_check.append(kw)

    to_check = to_check[:BRIEFING_BUDGET]  # 예산 상한 — 나머지는 이번엔 미확인(보수적으로 통과)
    if to_check:
        from src.competitors import observe_keyword  # 지연 임포트 (순환 방지)
        from playwright.sync_api import sync_playwright
        import time
        with sync_playwright() as pl:
            browser = pl.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=config.BROWSER_UA)  # 비로그인
            page = ctx.new_page()
            try:
                for kw in to_check:
                    try:
                        obs = observe_keyword(page, kw)
                        has = bool(obs.get("briefing"))
                    except Exception:
                        has = True  # 확인 실패는 보수적으로 통과 (기회 놓치지 않게)
                    result[kw] = has
                    conn.execute(
                        "INSERT INTO keyword_meta (keyword, has_briefing) VALUES (?, ?) "
                        "ON CONFLICT(keyword) DO UPDATE SET has_briefing = excluded.has_briefing, "
                        "checked_at = datetime('now', 'localtime')",
                        (kw, 1 if has else 0))
                    time.sleep(2)  # 검색 간격 (사람 같은 속도)
                conn.commit()
            finally:
                browser.close()
    # 예산 초과로 미확인인 것은 통과(True)로 둔다 — 다음 회차에 확인됨
    for kw in keywords:
        result.setdefault(kw, True)
    return result


def score_llm(candidates: list[dict]) -> list[dict]:
    """LLM이 '정보 정리형 글로 쓸 가치'를 0~1로 채점한다.

    매칭은 키워드 문자열이 아니라 **번호(i)** 로 한다 — 2026-08-19 문자열 매칭이
    전멸(LLM이 키워드를 변형 표기)해 전원 0점 → 브랜드 단독 키워드가 선정된 사고의 항체.
    전멸이 다시 발생하면 조용히 넘어가지 않고 예외를 던진다 (스케줄러가 텔레그램 알림).
    """
    listing = "\n".join(
        f"{i}. [{c['category']}] {c['keyword']} (월간검색 {c['volume']:,} / 블로그문서 {c['docs']:,} / 골든 {c['golden']:.2f})"
        for i, c in enumerate(candidates)
    )
    prompt = f"""네이버 블로그에 '정보 정리·분석형 글'(경험담 아님)을 써서 AI 브리핑에 인용되는 것이 목표다.
네이버 메이트는 10개 분야(여행/푸드/레시피/스타일/테크/라이프/컬쳐/미디어/인사이트/취미)별로
선정하므로, 분야 안에서 전문성을 쌓기 좋은 키워드가 장기적으로 유리하다.
아래 키워드 후보마다 쓸 가치를 0~1로 채점하라. 기준:
- 질문형 수요가 있고 사실 기반으로 답할 수 있는 주제인가 (방법/비교/정리/FAQ)
- **구체 질문형(비교·조건·예외·계산·서류·차이)이면 가점** — 2026-08-22 실측(lab.md):
  AI 브리핑은 헤드 텀에선 공식 기관만 인용하고, 공식이 즉답 못 하는 구체 질문에서
  블로그를 인용한다. 헤드 단일어(예: "실업급여신청")는 브리핑에 떠도 인용을 못 가져온다
- 시의성이 너무 짧지 않은가 (한 달 뒤에도 검색될 주제인가)
- 상업성 과열(병원·보험·대출·숙박예약 등 광고 레드오션)이 아닌가
- 브랜드·기업명 단독 키워드는 감점 (공식 자료가 이기므로 개인 블로그 인용 기회가 작다)
- 분야 전문성을 쌓기 좋은 주제군인가

후보 (번호로 응답하라):
{listing}

JSON 배열만 출력: [{{"i": 0, "score": 0.0, "reason": "한 줄 근거"}}]"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="topic-scoring")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    for s in json.loads(raw):
        i = int(s.get("i", -1))
        if 0 <= i < len(candidates):
            candidates[i]["llm_score"] = float(s.get("score", 0))
            candidates[i]["reason"] = s.get("reason", "")
    for c in candidates:
        c.setdefault("llm_score", 0.0)
        c.setdefault("reason", "채점 누락")
    if all(c["llm_score"] == 0 for c in candidates):
        raise RuntimeError("주제 채점 전멸 — LLM 응답과 후보 매칭 실패, 응답 형식 점검 필요")
    return candidates


def shortlist(scored: list[dict], size: int = 10, per_cat: int = 2) -> list[dict]:
    """오케스트레이터에 올릴 숏리스트 — 분야당 최대 per_cat개로 다양성을 강제한 10개."""
    out, counts = [], {}
    for c in sorted(scored, key=lambda c: (-c["llm_score"], -c["golden"])):
        if counts.get(c["category"], 0) >= per_cat:
            continue
        counts[c["category"]] = counts.get(c["category"], 0) + 1
        out.append(c)
        if len(out) >= size:
            break
    return out


def orchestrate_selection(conn: sqlite3.Connection, short: list[dict]) -> dict:
    """선정 오케스트레이터 — 점수 상위가 아니라 '전략에 맞는 조합'을 고른다.

    (사용자 제안 2026-08-19: 서로 다른 주제 10개에서 오케스트레이터가 선택)
    컨텍스트: 국면(Phase 0=분산 의무), 최근 선정 분야 쏠림, 게이트 차단 이력,
    C5의 내일 힌트. 실패 시 결정론 폴백(분야 중복 없는 상위 2+1)이라 발굴은 계속된다.
    """
    from src.steering import decide_phase, load_policy  # 지연 임포트 (순환 방지)
    phase = decide_phase(conn)
    hint = (load_policy(conn).get("tomorrow_hint") or {})
    recent_cats = [f"{r['category']}:{r['n']}" for r in conn.execute(
        "SELECT category, COUNT(*) n FROM topics WHERE status = 'selected' "
        "AND date >= date('now', 'localtime', '-7 days') GROUP BY category ORDER BY n DESC")]
    gate_skips = [r["keyword"] for r in conn.execute(
        "SELECT DISTINCT t.keyword FROM posts p JOIN topics t ON p.topic_id = t.id "
        "WHERE p.status = 'skipped' AND p.created_at >= datetime('now', 'localtime', '-7 days')")]

    n_sel = config.DAILY_SELECT_COUNT
    n_res = config.RESERVE_COUNT
    listing = "\n".join(
        f"{i}. {'🔮[다음달예측] ' if c.get('is_forward') else ''}[{c['category']}] {c['keyword']} "
        f"(골든 {c['golden']:.2f} / 채점 {c['llm_score']:.2f} — {c['reason']})"
        for i, c in enumerate(short))
    has_forward = any(c.get("is_forward") for c in short)
    prompt = f"""너는 블로그 주제 선정 오케스트레이터다. 오늘 쓸 주제 {n_sel}개(selected)와 예비 {n_res}개(reserve)를 골라라.

선정 원칙 (2026-08-26 사용자 전략 확정 — 구체 질문형 롱테일 집중):
숏리스트는 이미 **AI 브리핑이 확인된 구체 질문형 롱테일 위주**로 걸러져 있다
(헤드 키워드는 경쟁 수십만+브리핑이 공식기관만 인용해 우리가 못 이긴다. 우리가
이길 수 있는 유일한 자리는 공식이 즉답 못 하는 구체 질문 = 경쟁 얇음+블로그 인용).
- selected {n_sel}개: **경쟁이 얇고(블로그문서↓) 질문이 구체적인**(조건·예외·계산·서류·차이)
  롱테일을 우선한다. 서로 다른 헤드·상황을 다루도록 골라 주제 중복을 피하라
- 헤드 단일어(예: "실업급여신청", "여행자보험")는 브리핑에 떠도 인용 못 받으니 피하라
- reserve {n_res}개도 같은 기준 (게이트 탈락 시 보충용){'''
- 🔮[다음달예측] 표시는 다음 달에 검색이 급증할 정책 주제다 — 신선한 글로 브리핑 인용을
  선점하려는 것이니, selected에 1~2개 우선 포함하라 (아직 브리핑이 안 떠도 의도된 선점)''' if has_forward else ''}

전략 컨텍스트:
- 국면: Phase {phase['phase']} — {phase['why']}
- 최근 7일 선정 분야 분포: {', '.join(recent_cats) or '없음'}
- 최근 게이트 차단 키워드: {', '.join(gate_skips) or '없음'} — 같은 유형 재선정을 피하라
- 보정(C5)의 내일 힌트: {json.dumps(hint, ensure_ascii=False)}
- 브랜드·기업명 단독 키워드는 선정 금지

숏리스트 (번호로 응답):
{listing}

JSON만 출력: {{"selected": [0, 1, 2], "reserve": [3, 4], "rationale": "선정 조합의 이유 (2~3문장)"}}"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="topic-orchestration")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    pick = json.loads(raw)
    sel = [int(i) for i in pick.get("selected", [])][:n_sel]
    # reserve는 배열이 기본이지만 과거 형식(정수 하나)도 받아준다 (LLM 응답 관용)
    res_raw = pick.get("reserve", [])
    res = [int(i) for i in (res_raw if isinstance(res_raw, list) else [res_raw])][:n_res]
    if len(sel) < n_sel or any(not (0 <= i < len(short)) for i in sel + res):
        raise ValueError(f"오케스트레이터 응답 이상: {pick}")
    # 분야 분산 강제는 제거 (2026-08-26): 구체 질문형 롱테일 집중 전략으로 전환하면서
    # selected가 라이프·인사이트(정책)로 쏠리는 것은 의도된 결과다.
    return {"selected": sel, "reserve": res, "rationale": pick.get("rationale", "")}


def discover(conn: sqlite3.Connection | None = None) -> list[dict]:
    """주제 발굴 전체 실행 — topics 테이블에 기록하고 선정 결과를 반환한다."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        # 같은 날 재실행이면 오늘 발굴분을 갈아엎는다 (중복 selected 방지).
        # 단 [canary] 실험 주제는 보존한다 — 날짜와 무관하게 다음 생성이 한 번 집는다
        # (2026-08-24: 캐너리가 매일 DELETE에 지워져 영영 발행 안 되던 버그의 항체).
        # 바로 커밋 — 트랜잭션을 연 채 LLM 호출(별도 연결의 비용 기록)로 가면 락 충돌
        conn.execute("DELETE FROM topics WHERE date = ? "
                     "AND (rationale IS NULL OR rationale NOT LIKE '%[canary]%')", (today,))
        conn.commit()
        seeds = get_seeds(conn)
        volumes = expand_candidates(seeds)
        skip = recent_keywords(conn)
        head_scored = score_golden(volumes, skip=skip)

        # ① 정책 헤드 → 구체 질문형 롱테일 생성·채점 (우리가 이길 수 있는 유일한 자리 —
        #    헤드는 경쟁 수십만+브리핑이 공식만 인용. 2026-08-26 사용자 전략 확정)
        policy_heads = [c["keyword"] for c in head_scored
                        if c["category"] in POLICY_CATEGORIES][:12]
        try:
            longtails = score_longtail(expand_longtail_questions(policy_heads), skip)
        except Exception as e:
            print(f"롱테일 확장 실패 — 헤드만으로 진행: {type(e).__name__}: {e}")
            longtails = []

        # ①-b 월말(28일~)이면 다음 달 예측 주제를 추가 — 신선도 선점 (2026-08-28 사용자 요청).
        # 예측 주제는 아직 브리핑이 안 뜰 수 있어 게이트를 면제한다(선점 베팅). 경쟁 상한은
        # 신선도로 보상되므로 더 관대하게(2배).
        forward = []
        if date.today().day >= NEXT_MONTH_PREVIEW_FROM_DAY:
            try:
                forward = score_longtail(expand_next_month_forward(), skip,
                                         max_docs=LONGTAIL_MAX_DOCS * 2)
                for f in forward:
                    f["is_forward"] = True
                print(f"다음 달 예측 주제 {len(forward)}개 추가")
            except Exception as e:
                print(f"다음 달 예측 실패 — 건너뜀: {type(e).__name__}: {e}")

        # 예측 → 롱테일 → 헤드 보충으로 후보 풀 구성 → LLM 채점
        lt_keys = {c["keyword"] for c in forward + longtails}
        candidates = forward + longtails + [c for c in head_scored if c["keyword"] not in lt_keys]
        top = score_llm(candidates[:LLM_CANDIDATES])
        # 예측 먼저, 롱테일 다음, 그다음 채점 높은 순·경쟁 얇은 순
        top.sort(key=lambda c: (0 if c.get("is_forward") else 1 if c.get("is_longtail") else 2,
                                -c["llm_score"], c["docs"]))

        # 최종 선정 — 오케스트레이터 (실패 시 결정론 폴백)
        n_sel = config.DAILY_SELECT_COUNT
        n_res = config.RESERVE_COUNT
        # 롱테일은 분야가 라이프·인사이트로 겹치므로 per_cat을 넉넉히 (집중 전략)
        short = shortlist(top, size=14, per_cat=6)
        # shortlist가 llm_score로 재정렬하며 예측 주제를 밀어낼 수 있어 — 앞에 확실히 보강
        fwd_items = [c for c in top if c.get("is_forward")][:3]
        short = fwd_items + [c for c in short if c["keyword"] not in {f["keyword"] for f in fwd_items}]

        # ② 브리핑 게이트 — 브리핑 안 뜨는 키워드 컷. 단 예측 주제(is_forward)는 면제
        #    (9월엔 아직 브리핑이 안 떠서 — 선점 베팅). 전멸 방지 폴백.
        try:
            brief = check_briefing(conn, [c["keyword"] for c in short if not c.get("is_forward")])
            gated = [c for c in short if c.get("is_forward") or brief.get(c["keyword"], True)]
            if len(gated) >= n_sel + n_res:
                short = gated
            else:
                print(f"브리핑 게이트 후 {len(gated)}개뿐 — 부족해 원 shortlist 유지")
        except Exception as e:
            print(f"브리핑 게이트 실패 — 게이트 없이 진행: {type(e).__name__}: {e}")
        try:
            pick = orchestrate_selection(conn, short)
        except Exception as e:
            print(f"오케스트레이터 실패 — 결정론 폴백: {type(e).__name__}: {e}")
            # short는 이미 롱테일·브리핑 순으로 정렬·게이트됨 — 상위에서 순서대로
            pick = {"selected": list(range(min(n_sel, len(short)))),
                    "reserve": list(range(n_sel, min(n_sel + n_res, len(short)))),
                    "rationale": "오케스트레이터 실패 — 게이트된 shortlist 상위 폴백"}

        # 예측 주제 최소 1개 강제 포함 — LLM 프롬프트만으론 자주 누락돼 코드로 보장
        # (윈도우 중 다음 달 신선도 선점이 목적. 2026-08-28)
        fwd_in_short = [i for i, c in enumerate(short) if c.get("is_forward")]
        if fwd_in_short and not any(short[i].get("is_forward") for i in pick["selected"]):
            pick["selected"] = pick["selected"][:n_sel - 1] + [fwd_in_short[0]]
            pick["rationale"] += " [코드: 다음 달 예측 주제 1개 강제 포함]"

        chosen = {short[i]["keyword"]: "selected" for i in pick["selected"]}
        for i in pick["reserve"]:
            if 0 <= i < len(short):
                chosen.setdefault(short[i]["keyword"], "reserve")
        # 검증기 교체 등으로 reserve가 selected와 겹치면 다음 후보로 보충 (n_res개 확보)
        have_res = sum(1 for v in chosen.values() if v == "reserve")
        for c in short:
            if have_res >= n_res:
                break
            if c["keyword"] not in chosen:
                chosen[c["keyword"]] = "reserve"
                have_res += 1

        for c in top:
            status = chosen.get(c["keyword"], "candidate")
            # 예측 주제는 rationale에 표식 — DB·리포트에서 선점 발행을 구분해 추적
            reason = ("[다음달예측] " + c.get("reason", "")) if c.get("is_forward") else c.get("reason", "")
            conn.execute(
                "INSERT INTO topics (date, keyword, category, search_vol, doc_count, "
                "golden_score, llm_score, rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, c["keyword"], c["category"], c["volume"], c["docs"], c["golden"],
                 c["llm_score"], reason, status),
            )
        # 선정 조합의 근거를 결정 로그에 남긴다 (복기 재료 — 전권 위임의 최소 조건)
        conn.execute(
            "INSERT INTO decisions (date, input_summary, decision_json, rationale) VALUES (?, ?, ?, ?)",
            (today, json.dumps({"shortlist": [c["keyword"] for c in short]}, ensure_ascii=False),
             json.dumps({"purpose": "topic-orchestration",
                         "selected": [short[i]["keyword"] for i in pick["selected"]]},
                        ensure_ascii=False),
             pick["rationale"]))
        conn.commit()
        return top
    finally:
        if own:
            conn.close()
