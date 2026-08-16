"""C1 주제 발굴 — 오늘 쓸 주제를 데이터로 고른다.

흐름 (usecases.md C1):
  ① 시드 키워드(보정 정책이 관리, 없으면 기본값)를 검색광고 키워드도구에 넣어
     연관키워드 수백 개로 확장 — 월간 검색량 확보
  ② 검색량 상위 후보만 검색 API로 블로그 문서수 조회 (쿼리 절약)
  ③ 골든키워드 점수 = 월간 검색량 ÷ 문서수 (수요 대비 공급이 빈 곳)
  ④ LLM(판단 모델)이 '정보형 글로 쓸 가치'를 채점 — 골든 점수 상위만 투입
  ⑤ 최종 상위 1~2개 selected, 다음 1개 reserve로 topics 테이블에 기록
"""

import json
import sqlite3
from datetime import date

from src import config, db, llm
from src.naver_api import doc_count, keyword_stats

# 네이버 메이트 공식 10개 분야 (보도자료 2026-07-15) — 선정이 분야별로 이뤄지므로
# 시드를 이 체계에 정렬한다. 분야별 시드 2개는 콜드 스타트용이며,
# 운영이 시작되면 일일 보정(C5)이 policy로 시드·타깃 분야를 조정한다.
DEFAULT_SEEDS_BY_CATEGORY = {
    "여행": ["국내여행", "해외여행준비물"],
    "푸드": ["맛집추천", "제철음식"],
    "레시피": ["자취요리", "에어프라이어요리"],
    "스타일": ["여름코디", "패션기초"],
    "테크": ["노트북추천", "스마트홈"],
    "라이프": ["정부지원금", "자취꿀팁"],
    "컬쳐": ["전시회추천", "독서모임"],
    "미디어": ["넷플릭스추천", "드라마정보"],
    "인사이트": ["재테크기초", "청약방법"],
    "취미": ["캠핑초보", "홈트레이닝"],
}

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


def score_llm(candidates: list[dict]) -> list[dict]:
    """LLM이 '정보 정리형 글로 쓸 가치'를 0~1로 채점한다."""
    listing = "\n".join(
        f"- [{c['category']}] {c['keyword']} (월간검색 {c['volume']:,} / 블로그문서 {c['docs']:,} / 골든 {c['golden']:.2f})"
        for c in candidates
    )
    prompt = f"""네이버 블로그에 '정보 정리·분석형 글'(경험담 아님)을 써서 AI 브리핑에 인용되는 것이 목표다.
네이버 메이트는 10개 분야(여행/푸드/레시피/스타일/테크/라이프/컬쳐/미디어/인사이트/취미)별로
선정하므로, 분야 안에서 전문성을 쌓기 좋은 키워드가 장기적으로 유리하다.
아래 키워드 후보마다 쓸 가치를 0~1로 채점하라. 기준:
- 질문형 수요가 있고 사실 기반으로 답할 수 있는 주제인가 (방법/비교/정리/FAQ)
- 시의성이 너무 짧지 않은가 (한 달 뒤에도 검색될 주제인가)
- 상업성 과열(병원·보험·대출·숙박예약 등 광고 레드오션)이 아닌가
- 브랜드·기업명 단독 키워드는 감점 (공식 자료가 이기므로 개인 블로그 인용 기회가 작다)
- 분야 전문성을 쌓기 좋은 주제군인가

후보:
{listing}

JSON 배열만 출력: [{{"keyword": "...", "score": 0.0, "reason": "한 줄 근거"}}]"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="topic-scoring")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    scores = {s["keyword"]: s for s in json.loads(raw)}
    for c in candidates:
        s = scores.get(c["keyword"], {})
        c["llm_score"] = float(s.get("score", 0))
        c["reason"] = s.get("reason", "채점 누락")
    return candidates


def discover(conn: sqlite3.Connection | None = None) -> list[dict]:
    """주제 발굴 전체 실행 — topics 테이블에 기록하고 선정 결과를 반환한다."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        # 같은 날 재실행이면 오늘 발굴분을 갈아엎는다 (중복 selected 방지).
        # 바로 커밋 — 트랜잭션을 연 채 LLM 호출(별도 연결의 비용 기록)로 가면 락 충돌
        conn.execute("DELETE FROM topics WHERE date = ?", (today,))
        conn.commit()
        seeds = get_seeds(conn)
        volumes = expand_candidates(seeds)
        candidates = score_golden(volumes, skip=recent_keywords(conn))
        top = score_llm(candidates[:LLM_CANDIDATES])
        # 최종 순위 = LLM 점수 우선, 동점이면 골든 점수
        top.sort(key=lambda c: (-c["llm_score"], -c["golden"]))

        for rank, c in enumerate(top):
            status = "selected" if rank < 2 else ("reserve" if rank == 2 else "candidate")
            conn.execute(
                "INSERT INTO topics (date, keyword, category, search_vol, doc_count, "
                "golden_score, llm_score, rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, c["keyword"], c["category"], c["volume"], c["docs"], c["golden"],
                 c["llm_score"], c["reason"], status),
            )
        conn.commit()
        return top
    finally:
        if own:
            conn.close()
