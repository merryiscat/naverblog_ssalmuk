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

# 콜드 스타트용 기본 시드 — 운영이 시작되면 일일 보정(C5)이 policy로 교체한다
DEFAULT_SEEDS = ["캠핑 초보", "자취 요리", "강아지 건강", "국내 여행", "홈트레이닝",
                 "재테크 기초", "노트북 추천", "정부 지원금"]

# 쿼리 예산 — 문서수 조회(검색 API)는 검색량 상위 이 개수만
DOC_COUNT_BUDGET = 40
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


def get_seeds(conn: sqlite3.Connection) -> list[str]:
    """시드 키워드 — 최신 policy에 있으면 그것을, 없으면 기본값."""
    row = conn.execute("SELECT policy_json FROM policy ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        seeds = json.loads(row["policy_json"]).get("seeds")
        if seeds:
            return seeds
    return DEFAULT_SEEDS


def recent_keywords(conn: sqlite3.Connection, days: int = 30) -> set[str]:
    """최근에 이미 다룬(선정된) 키워드 — 중복 주제 방지."""
    rows = conn.execute(
        "SELECT keyword FROM topics WHERE status IN ('selected', 'used') "
        "AND date >= date('now', 'localtime', ?)", (f"-{days} days",)
    ).fetchall()
    return {r["keyword"] for r in rows}


def expand_candidates(seeds: list[str]) -> dict[str, int]:
    """시드를 연관키워드로 확장. 반환: {키워드: 월간 총검색량(PC+모바일)}."""
    volumes: dict[str, int] = {}
    for i in range(0, len(seeds), 5):  # 키워드도구는 한 번에 최대 5개
        for row in keyword_stats(seeds[i:i + 5]):
            kw = row["relKeyword"].strip()
            vol = _to_int(row.get("monthlyPcQcCnt", 0)) + _to_int(row.get("monthlyMobileQcCnt", 0))
            if len(kw) >= 2 and vol >= 300:  # 검색량이 너무 작으면 인용 기회도 작다
                volumes[kw] = max(volumes.get(kw, 0), vol)
    return volumes


def score_golden(volumes: dict[str, int], skip: set[str]) -> list[dict]:
    """검색량 상위 후보의 문서수를 조회해 골든 점수를 매긴다."""
    ranked = sorted(volumes.items(), key=lambda x: -x[1])
    out = []
    for kw, vol in ranked[:DOC_COUNT_BUDGET]:
        if kw in skip:
            continue
        docs = doc_count(kw)
        out.append({
            "keyword": kw, "volume": vol, "docs": docs,
            "golden": vol / max(docs, 1),
        })
    out.sort(key=lambda c: -c["golden"])
    return out


def score_llm(candidates: list[dict]) -> list[dict]:
    """LLM이 '정보 정리형 글로 쓸 가치'를 0~1로 채점한다."""
    listing = "\n".join(
        f"- {c['keyword']} (월간검색 {c['volume']:,} / 블로그문서 {c['docs']:,} / 골든 {c['golden']:.2f})"
        for c in candidates
    )
    prompt = f"""네이버 블로그에 '정보 정리·분석형 글'(경험담 아님)을 써서 AI 브리핑에 인용되는 것이 목표다.
아래 키워드 후보마다 쓸 가치를 0~1로 채점하라. 기준:
- 질문형 수요가 있고 사실 기반으로 답할 수 있는 주제인가 (방법/비교/정리/FAQ)
- 시의성이 너무 짧지 않은가 (한 달 뒤에도 검색될 주제인가)
- 상업성 과열(병원·보험·대출 등 광고 레드오션)이 아닌가
- 전문성을 쌓기 좋은 주제군인가

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
        seeds = get_seeds(conn)
        volumes = expand_candidates(seeds)
        candidates = score_golden(volumes, skip=recent_keywords(conn))
        top = score_llm(candidates[:LLM_CANDIDATES])
        # 최종 순위 = LLM 점수 우선, 동점이면 골든 점수
        top.sort(key=lambda c: (-c["llm_score"], -c["golden"]))

        for rank, c in enumerate(top):
            status = "selected" if rank < 2 else ("reserve" if rank == 2 else "candidate")
            conn.execute(
                "INSERT INTO topics (date, keyword, search_vol, doc_count, golden_score, "
                "llm_score, rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (today, c["keyword"], c["volume"], c["docs"], c["golden"],
                 c["llm_score"], c["reason"], status),
            )
        conn.commit()
        return top
    finally:
        if own:
            conn.close()
