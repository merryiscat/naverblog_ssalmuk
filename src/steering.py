"""C5 일일 자가 보정 + 텔레그램 리포트 — 시스템이 스스로 방향을 잡는 밤 루틴.

설계 사양은 docs/loop-design.md:
- 국면(Phase)은 LLM이 아니라 **데이터 조건**이 정한다 (전환 판정은 주간 루프지만
  판정 함수는 여기 있고, 일일 루틴은 현재 국면을 읽기만 한다)
- Phase 0(탐색)에서는 전략 변경 금지 — LLM 결정은 '내일의 힌트' 수준으로 제한
- 결정은 반드시 rationale과 함께 decisions에 남는다 (전권 위임의 최소 조건)
- 하드 가드레일·냉각기 위반 결정은 검증기가 거부한다
"""

import json
import sqlite3
from datetime import date, datetime

from src import config, db, guardrails, llm, notify

PHASE0_WEEKS = 4  # 사용자 확정: 탐색 국면 4주 (또는 첫 ai_cited 중 빠른 쪽)


# ---------------------------------------------------------------------------
# 정책 저장소 (policy 테이블 — 최신 행이 유효)
# ---------------------------------------------------------------------------

def load_policy(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT policy_json FROM policy ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["policy_json"]) if row else {}


def save_policy(conn: sqlite3.Connection, policy: dict) -> None:
    conn.execute("INSERT INTO policy (policy_json) VALUES (?)",
                 (json.dumps(policy, ensure_ascii=False),))
    conn.commit()


# ---------------------------------------------------------------------------
# 국면 판정 — 데이터 조건 (loop-design.md 규칙 그대로)
# ---------------------------------------------------------------------------

def decide_phase(conn: sqlite3.Connection) -> dict:
    """현재 국면과 근거를 데이터에서 계산한다."""
    first_pub = conn.execute(
        "SELECT MIN(published_at) AS d FROM posts WHERE status IN ('published','verified')"
    ).fetchone()["d"]
    # 인용 신호는 두 갈래 — per-keyword 검색 감지(rankings)는 놓칠 수 있어(2026-08-26 실측:
    # 위젯 130인데 per-keyword 0), 프로필 인용수 위젯(네이버 공식 카운트, metrics.citations)을
    # 권위 신호로 함께 본다. 둘 중 하나라도 인용을 가리키면 Phase 0을 벗어난다.
    cited_kw = conn.execute(
        "SELECT COUNT(*) AS n FROM rankings WHERE ai_cited = 1").fetchone()["n"]
    prof = conn.execute(
        "SELECT MAX(citations) AS c FROM metrics WHERE citations IS NOT NULL").fetchone()["c"]
    cited = cited_kw or (prof or 0)
    cat_cited = conn.execute(
        "SELECT t.category, COUNT(DISTINCT r.post_id) AS n FROM rankings r "
        "JOIN posts p ON r.post_id = p.id JOIN topics t ON p.topic_id = t.id "
        "WHERE r.ai_cited = 1 GROUP BY t.category ORDER BY n DESC").fetchone()

    if not first_pub:
        return {"phase": 0, "why": "발행 이력 없음 — 탐색 시작 전"}
    weeks = (datetime.now() - datetime.fromisoformat(first_pub)).days / 7
    if cat_cited and cat_cited["n"] >= 2:
        return {"phase": 2, "why": f"분야 '{cat_cited['category']}'에서 인용 글 {cat_cited['n']}건 — 수렴",
                "target_category": cat_cited["category"]}
    if cited >= 1 or weeks >= PHASE0_WEEKS:
        sig = (f"프로필 인용수 {prof}" if prof else f"per-keyword {cited_kw}건")
        return {"phase": 1, "why": f"{sig}, 발행 {weeks:.1f}주차 — 가중 국면"}
    return {"phase": 0, "why": f"발행 {weeks:.1f}주차, 인용 신호 없음 — 탐색 유지 (4주까지)"}


# ---------------------------------------------------------------------------
# 일일 입력 수집
# ---------------------------------------------------------------------------

def gather_input(conn: sqlite3.Connection) -> dict:
    today = date.today().isoformat()
    topics = [dict(r) for r in conn.execute(
        "SELECT keyword, category, golden_score, llm_score, status FROM topics "
        "WHERE date = ? AND status IN ('selected','reserve')", (today,))]
    posts = [dict(r) for r in conn.execute(
        "SELECT id, title, status, published_at FROM posts "
        "WHERE date(created_at) = ?", (today,))]
    ranks = [dict(r) for r in conn.execute(
        "SELECT keyword, rank, ai_cited FROM rankings WHERE date = ?", (today,))]
    metric = conn.execute("SELECT * FROM metrics WHERE date = ?", (today,)).fetchone()
    # 인용수 일 증분 — 오늘 누적 - 직전 기록된 누적 (리포트에 '오늘 +N'을 찍기 위함, 2026-08-30).
    # metrics.citations 컬럼 = 누적 인용수 정수(cumulative_n). 위젯이 '100 미만'이면 None이라 건너뛴다.
    cur_cum = metric["citations"] if metric else None
    prev_cum_row = conn.execute(
        "SELECT citations FROM metrics WHERE citations IS NOT NULL AND date < ? "
        "ORDER BY date DESC LIMIT 1", (today,)).fetchone()
    prev_cum = prev_cum_row["citations"] if prev_cum_row else None
    citation_delta = (cur_cum - prev_cum) if (isinstance(cur_cum, int)
                                              and isinstance(prev_cum, int)) else None
    # AI 검색 검토 상세(색인·브리핑·인용) — rankings에 없는 briefing까지 details_json에서
    ai_review = (json.loads(metric["details_json"]).get("ranks", []) if metric else [])
    from src.competitors import recent_summary  # 순환 임포트 방지 (지연 임포트)
    from src.inspector import recent_summary as inspect_summary
    from src.mate_observer import recent_summary as mate_summary
    return {
        "topics": topics, "posts": posts, "ranks": ranks, "ai_review": ai_review,
        "competitor_watch": recent_summary(conn),  # 공백 기회·브리핑 없는 키워드
        "mate_watch": mate_summary(conn),  # 메이트 선정자 관찰 힌트 (C7, 주 1회 갱신)
        "self_inspection": inspect_summary(conn),  # 블로그 화면 검수 지적 (주 3회 갱신)
        "citations": json.loads(metric["details_json"])["citations"] if metric else None,
        "citation_delta": citation_delta,  # 전일 대비 인용 증분 (없으면 None)
        "visitors": json.loads(metric["details_json"]).get("visitors") if metric else None,
        "inflow": json.loads(metric["details_json"]).get("inflow") if metric else None,
        "cost_usd": guardrails.month_cost_usd(conn),
        "budget_usd": config.MONTHLY_BUDGET_USD,
        "published_total": conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE status IN ('published','verified')"
        ).fetchone()["n"],
    }


# ---------------------------------------------------------------------------
# LLM 보정 결정 + 검증
# ---------------------------------------------------------------------------

def llm_decide(phase: dict, data: dict, policy: dict) -> dict:
    """최상위 판단 모델이 내일의 방향을 제안한다. Phase 0은 결정 공간이 좁다."""
    scope = ("Phase 0(탐색): 전략 변경 금지. 결정할 수 있는 것은 ①내일 탐색할 주제 유형 힌트 "
             "②관찰에서 눈에 띈 것 메모뿐이다. 시드·분야·빈도 변경을 제안해도 적용되지 않는다."
             if phase["phase"] == 0 else
             "Phase 1+: 시드 키워드 추가/제거, 분야 가중, 템플릿 힌트를 제안할 수 있다. "
             "단 같은 항목은 주 1회만 변경되며(냉각기), 발행 빈도 상한·비용 상한은 불변이다.")
    prompt = f"""너는 네이버 블로그 자동 운영 시스템의 일일 보정 담당이다.
목표: AI 브리핑 인용 획득 → 네이버 메이트 선정. 오늘 데이터를 보고 내일 방향을 정하라.

현재 국면: Phase {phase['phase']} — {phase['why']}
결정 범위: {scope}

오늘 데이터:
{json.dumps(data, ensure_ascii=False, indent=1, default=str)}

현재 정책: {json.dumps(policy, ensure_ascii=False)}

특별 규칙: competitor_watch.stale_opportunities(브리핑은 뜨는데 출처가 낡은 쿼리)가 있으면
내일 힌트에 최우선 반영하라 — 신선한 글로 인용을 가져올 확률이 가장 높은 표적이다.
no_briefing_keywords는 인용 기회가 없는 키워드이므로 유사 주제 회피를 권고하라.
mate_watch.policy_hints는 실제 메이트 선정자 블로그 분석에서 나온 것이다 —
분야·주제·스타일 방향을 정할 때 참고하라 (confidence가 low면 가볍게만).
self_inspection은 우리 블로그 실제 화면을 검수한 결과다 — persisting(반복 지적)이
있으면 alerts로 사람에게 올리고, 글 생성으로 고칠 수 있는 것은 내일 힌트에 반영하라.
inflow.queries는 사람들이 **실제로 우리 글에 들어올 때 친 검색어**다 (유입경로는 inflow.paths).
이게 검색전략의 핵심 재료다 — 유입 검색어를 보고 ①어떤 제도·주제가 실제로 검색을 부르는지
파악해 그 계열을 더 깊게 파고 ②각 검색어의 인접 구체 질문(같은 제도의 다른 조건·대상·상황·서류)을
tomorrow_hint에 넣어 검색 유입을 넓혀라. 유입 검색어가 특정 제도에 몰리면 그 제도 특화를 권고하라.

JSON만 출력:
{{"rationale": "오늘 데이터에서 읽은 것과 결정 이유 (2~4문장)",
 "tomorrow_hint": {{"explore": "내일 탐색 발행의 주제 유형/방향 힌트", "exploit": "활용 발행 힌트 (성과 데이터 없으면 null)"}},
 "seed_changes": {{"add": [], "remove": []}},
 "category_weights": {{}},
 "alerts": ["사람에게 알릴 이상 신호 (없으면 빈 배열)"]}}"""
    raw = llm.chat(config.MODEL_STEER, prompt, purpose="daily-steering")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def apply_decision(conn: sqlite3.Connection, phase: dict, decision: dict, policy: dict) -> list[str]:
    """검증기 — 규칙 안에서만 정책에 반영한다. 반환: 거부·적용 내역."""
    log = []
    policy["phase"] = phase["phase"]
    policy["tomorrow_hint"] = decision.get("tomorrow_hint")

    if phase["phase"] == 0:
        # 탐색 국면: 전략 변경은 전부 거부 (기록만)
        if decision.get("seed_changes", {}).get("add") or decision.get("seed_changes", {}).get("remove"):
            log.append("시드 변경 제안 거부 (Phase 0 — 전략 변경 금지)")
        if decision.get("category_weights"):
            log.append("분야 가중 제안 거부 (Phase 0)")
    else:
        # 냉각기: 같은 항목은 주 1회만 (loop-design 규칙 4)
        last = policy.get("last_changes", {})
        today = date.today().isoformat()
        for key in ("seed_changes", "category_weights"):
            if not decision.get(key) or decision[key] in ({}, {"add": [], "remove": []}):
                continue
            last_day = last.get(key)
            if last_day and (date.today() - date.fromisoformat(last_day)).days < 7:
                log.append(f"{key} 변경 거부 (냉각기 — 최근 변경 {last_day})")
                continue
            policy[key] = decision[key]
            last[key] = today
            log.append(f"{key} 적용")
        policy["last_changes"] = last
        if phase.get("target_category"):
            policy["target_categories"] = [phase["target_category"]]
            log.append(f"분야 수렴: {phase['target_category']}")

    save_policy(conn, policy)
    return log


# ---------------------------------------------------------------------------
# 일일 리포트
# ---------------------------------------------------------------------------

def compose_report(phase: dict, data: dict, decision: dict, applied: list[str]) -> str:
    lines = [f"📊 일일 리포트 {date.today():%m/%d}", ""]
    if data["posts"]:
        for p in data["posts"]:
            mark = {"verified": "✅", "published": "📝", "skipped": "⏭️"}.get(p["status"], "·")
            lines.append(f"{mark} {p['title'][:40]} ({p['status']})")
    else:
        lines.append("오늘 발행 없음")
    c = data.get("citations") or {}
    delta = data.get("citation_delta")
    delta_str = f" (오늘 {delta:+d})" if isinstance(delta, int) else ""
    lines += ["", f"인용수: 누적 {c.get('cumulative', '?')}{delta_str} / 당월 {c.get('this_month', '?')}"]
    v = data.get("visitors") or {}
    if v:
        lines.append(f"방문자: 오늘 {v.get('today', '?')} / 누적 {v.get('total', '?')}"
                     f" / 이웃 {v.get('subscribers', '?')}")
    inf = data.get("inflow") or {}
    if inf.get("queries"):
        top = " · ".join(q["query"] for q in inf["queries"][:5])
        lines.append(f"🔎 유입검색어 {inf.get('total', '?')}건 — Top: {top}")
    # AI 검색 검토 결과 — 발행 글마다 색인/브리핑 노출/우리 글 인용 상태를 명확히 (2026-08-24)
    review = data.get("ai_review") or data["ranks"]
    if review:
        n = len(review)
        n_idx = sum(1 for r in review if r.get("indexed"))
        n_brief = sum(1 for r in review if r.get("briefing"))
        n_cited = sum(1 for r in review if r.get("cited") or r.get("ai_cited"))
        lines += ["", f"🔎 AI 검색 검토 — 색인 {n_idx}/{n} · 브리핑 노출 {n_brief} · 우리글 인용 {n_cited}"]
        for r in review:
            idx = "색인✓" if r.get("indexed") else "미색인"
            if r.get("briefing"):
                br = "🎯인용됨" if (r.get("cited") or r.get("ai_cited")) else "브리핑○/인용✗"
            elif "briefing" in r:
                br = "브리핑✗(기회없음)"
            else:
                br = ("🎯인용" if r.get("ai_cited") else "인용✗")
            lines.append(f"· {r['keyword']}: {idx} / {br}")
    lines += ["", f"국면: Phase {phase['phase']} — {phase['why']}",
              f"보정: {decision.get('rationale', '')}"]
    if applied:
        lines.append("적용: " + "; ".join(applied))
    for alert in decision.get("alerts", []):
        lines.append(f"⚠️ {alert}")
    lines += ["", f"비용: ${data['cost_usd']:.2f} / ${data['budget_usd']:.2f} "
                  f"(누적 발행 {data['published_total']}건)"]
    return "\n".join(lines)


def run_daily(conn: sqlite3.Connection | None = None) -> dict:
    """일일 보정 전체 실행 — 야간 루틴의 마지막 단계."""
    own = conn is None
    conn = conn or db.connect()
    try:
        phase = decide_phase(conn)
        data = gather_input(conn)
        policy = load_policy(conn)
        decision = llm_decide(phase, data, policy)
        applied = apply_decision(conn, phase, decision, policy)

        conn.execute(
            "INSERT INTO decisions (date, input_summary, decision_json, rationale) "
            "VALUES (?, ?, ?, ?)",
            (date.today().isoformat(),
             json.dumps({"phase": phase, "posts": len(data["posts"]),
                         "ranks": data["ranks"]}, ensure_ascii=False),
             json.dumps({"decision": decision, "applied": applied}, ensure_ascii=False),
             decision.get("rationale", "")))
        conn.commit()

        report = compose_report(phase, data, decision, applied)
        sent = notify.send(report)
        return {"phase": phase, "decision": decision, "applied": applied,
                "report": report, "telegram_sent": sent}
    finally:
        if own:
            conn.close()
