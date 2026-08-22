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
import sqlite3
from datetime import date

from src import config, db, llm
from src.naver_api import doc_count, keyword_stats

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
        f"{i}. [{c['category']}] {c['keyword']} (골든 {c['golden']:.2f} / 채점 {c['llm_score']:.2f} — {c['reason']})"
        for i, c in enumerate(short))
    prompt = f"""너는 블로그 주제 선정 오케스트레이터다. 오늘 쓸 주제 {n_sel}개(selected)와 예비 {n_res}개(reserve)를 골라라.

선정 비율 (2026-08-22 사용자 확정 — 부분 특화. 실측 근거: AI 브리핑 신호가
정책·지원금 계열에서만 관측, 넷플릭스·여행류는 브리핑 자체가 안 뜨는 쿼리):
- selected {n_sel}개 중 **2개는 정책·지원금형** — 지원금·정책·제도·신청 방법·자격 요건 등
  시점 의존 정보 수요가 있는 키워드 (분야 무관 — 브리핑이 뜨는 유형이 기준)
- 같은 값이면 **구체 질문형(비교·조건·예외·계산·서류)을 헤드 단일어보다 우선** —
  실측(lab.md 원칙 2): 헤드 텀 브리핑은 공식 기관이 점유, 블로그 인용은 구체 질문에서 발생
- 나머지 1개는 **탐색** — 정책·지원금형이 아닌 다른 분야에서 (분산 탐색 창 유지)
- 숏리스트에 정책·지원금형이 2개 미만이면 있는 만큼만 채우고 rationale에 그 사실을 명시
- reserve {n_res}개도 같은 우선순위로 (정책·지원금형 우선 — 게이트 탈락 시 보충용)

전략 컨텍스트:
- 국면: Phase {phase['phase']} — {phase['why']}
- 최근 7일 선정 분야 분포: {', '.join(recent_cats) or '없음'} — 탐색 슬롯은 쏠린 분야를 피하라
- 최근 게이트 차단 키워드: {', '.join(gate_skips) or '없음'} — 같은 유형(공식 소스 없는
  여행지·현장 정보류 등) 재선정을 피하라
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
    # 검증(코드 레벨): Phase 0·1에서 selected 전부가 한 분야로 쏠리는 것만 막는다.
    # (2026-08-22 완화 — 정책·지원금형 2건이 같은 분야(라이프 등)일 수 있으므로
    # '모두 다른 분야' 강제를 풀고, 탐색 슬롯 1개의 분야 분리만 보장)
    if phase["phase"] < 2:
        cats = [short[i]["category"] for i in sel]
        if len(set(cats)) == 1:
            alt = next((i for i, c in enumerate(short)
                        if i not in sel and c["category"] != cats[0]), None)
            if alt is not None:
                pick["rationale"] += (f" [검증기: 전 슬롯 동일 분야 — "
                                      f"{short[sel[-1]]['keyword']}→{short[alt]['keyword']} 교체]")
                sel[-1] = alt
    return {"selected": sel, "reserve": res, "rationale": pick.get("rationale", "")}


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
        top.sort(key=lambda c: (-c["llm_score"], -c["golden"]))

        # 최종 선정 — 오케스트레이터 (실패 시 결정론 폴백: 분야 중복 없는 상위 3+2)
        n_sel = config.DAILY_SELECT_COUNT
        n_res = config.RESERVE_COUNT
        short = shortlist(top)
        try:
            pick = orchestrate_selection(conn, short)
        except Exception as e:
            print(f"오케스트레이터 실패 — 결정론 폴백: {type(e).__name__}: {e}")
            fallback = [c for c in shortlist(top, size=n_sel + n_res, per_cat=1) if c in short]
            pick = {"selected": [short.index(c) for c in fallback[:n_sel]],
                    "reserve": [short.index(c) for c in fallback[n_sel:n_sel + n_res]],
                    "rationale": "오케스트레이터 실패 — 분야 중복 없는 점수 상위 폴백"}

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
            conn.execute(
                "INSERT INTO topics (date, keyword, category, search_vol, doc_count, "
                "golden_score, llm_score, rationale, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, c["keyword"], c["category"], c["volume"], c["docs"], c["golden"],
                 c["llm_score"], c["reason"], status),
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
