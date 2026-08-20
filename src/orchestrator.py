"""검수 오케스트레이터 — 검수 에이전트의 의견을 받아 '무엇을 실행할지' 결정한다.

배경 (2026-08-20 사용자 위임): 검수 에이전트의 지적이 "사용자 결정 대기"로 쌓이는
구조를 없앤다 — 의견은 오케스트레이터가 결정하고, 수행 에이전트(blog_actions)가 실행한다.
사람은 결과 보고(텔레그램)만 받는다.

구조는 프로젝트 에이전트 공통 원칙(고민→검토→실행→복기)을 따른다:
  고민 — 검수 보고 + 현재 블로그 설정 + 최근 발행 글(콘텐츠 정체성)을 모은다
  검토 — 최상위 판단 모델(gpt-5.4)이 액션 카탈로그 안에서만 결정. 코드가 화이트리스트·
         값 제약을 재검증하고, 정체성 항목(블로그명·별명)은 회당 1회 변경 제한
  실행 — 수행 에이전트가 반영하고 실제 값으로 검증
  복기 — 결정·결과를 decisions에 기록. 직전 결정을 다음 판단의 입력으로 넣어
         같은 항목을 계속 뒤집는 진동(oscillation)을 막는다

실행 시점: 블로그 검수(화·목·토 23:15) 직후 — 검수 보고가 가장 신선할 때.
"""

import json
import sqlite3
from datetime import date

from src import blog_actions, config, db, llm

DECIDE_PROMPT = """너는 네이버 블로그 운영 오케스트레이터다. 방금 나온 블로그 검수 보고를 보고,
아래 액션 카탈로그 안에서 지금 실행할 것을 결정하라. 사람의 확인 없이 바로 실행된다 —
확신이 없는 변경은 하지 말고 miss로 남겨라.

## 블로그 맥락
- 목표: 네이버 AI 브리핑 인용 → 메이트 선정. 정보 정리형 글을 자동 발행하는 블로그다
- 최근 발행 글 (콘텐츠 정체성의 근거): {recent_posts}
- 현재 블로그 설정: {current}
- 블로그 카테고리: 메이트 10분야(여행/푸드/레시피/스타일/테크/라이프/컬쳐/미디어/인사이트/취미)

## 검수 보고 (오늘 {today})
{report}

## 직전 오케스트레이터 결정 (복기 — 같은 항목을 계속 뒤집는 진동 금지)
{prev_decision}

## 액션 카탈로그 (이 밖의 실행 수단은 없다)
{catalog}

## 결정 원칙
- 블로그명·별명은 블로그의 얼굴이다 — 콘텐츠 정체성(정보 정리·전 분야 가이드)과 어긋날 때만,
  검색 노출에 유리한 명확한 이름으로 바꾼다. 이미 괜찮으면 유지
- 소개글은 "무엇을 다루는 블로그인지 + 기준(출처 표기, 최신 기준일)"이 드러나게
- 주제(내 블로그 주제)는 실제 콘텐츠 분포와 맞는 것으로
- 프로필 이미지는 make_profile_image로 생성만 가능 (업로드는 수동) — 이미지가 없다는
  지적이 있을 때만 1회 생성
- 코드 수정이 필요한 지적(렌더링 깨짐 등)과 여기 카탈로그로 해결 불가한 지적은
  actions에 넣지 말고 manual로 분류하라

JSON만 출력:
{{"actions": [{{"action": "액션명", "value": "값", "why": "한 줄 근거"}}],
 "manual": [{{"what": "카탈로그로 해결 불가한 지적", "why": "왜 수동인지"}}],
 "rationale": "전체 결정의 근거 2~3문장"}}"""


def _recent_posts(conn: sqlite3.Connection, n: int = 5) -> str:
    rows = conn.execute(
        "SELECT p.title, t.category FROM posts p LEFT JOIN topics t ON p.topic_id = t.id "
        "WHERE p.status IN ('published', 'verified') ORDER BY p.published_at DESC LIMIT ?",
        (n,)).fetchall()
    return "; ".join(f"[{r['category'] or '?'}] {r['title'][:40]}" for r in rows) or "없음"


def _prev_decision(conn: sqlite3.Connection) -> str:
    """직전 결정을 액션별 성공/실패로 요약 — 실패한 액션의 재시도는 진동이 아니다."""
    row = conn.execute(
        "SELECT date, decision_json, rationale FROM decisions "
        "WHERE decision_json LIKE '%blog-curation%' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "(첫 실행 — 이전 결정 없음)"
    try:
        d = json.loads(row["decision_json"])
        parts = [f"{r.get('action')}={'성공' if r.get('ok') else '실패:' + str(r.get('detail'))[:50]}"
                 for r in d.get("results", [])]
        summary = "; ".join(parts) or row["decision_json"][:300]
    except json.JSONDecodeError:
        summary = row["decision_json"][:300]
    return (f"{row['date']}: {summary} — 근거: {row['rationale'][:150]}\n"
            f"(주의: '실패'한 액션은 실제 반영되지 않았다 — 재시도는 진동이 아니라 필요한 일이다. "
            f"'성공'한 항목을 다시 뒤집는 것만 진동이다)")


def _identity_change_guard(conn: sqlite3.Connection, actions: list[dict]) -> list[dict]:
    """정체성 항목(블로그명·별명)은 14일에 1회만 — LLM이 결정해도 코드가 제동.

    블로그명이 자주 바뀌면 그 자체가 신뢰도 감점 요인이다 (독자·심사자 관점).
    """
    # 최근 14일 내 '성공'한 정체성 변경만 집계 — 실패 기록이 재시도를 막으면 안 된다
    changed: set[str] = set()
    for row in conn.execute(
            "SELECT decision_json FROM decisions WHERE decision_json LIKE '%blog-curation%' "
            "AND date >= date('now', 'localtime', '-14 days')"):
        try:
            for r in json.loads(row["decision_json"]).get("results", []):
                if r.get("ok") and r.get("action") in ("set_blog_name", "set_nickname"):
                    changed.add(r["action"])
        except json.JSONDecodeError:
            continue
    guarded = []
    for a in actions:
        if a["action"] in changed:
            a = {**a, "_blocked": f"{a['action']}은 최근 14일 내 이미 변경 성공 — 진동 방지로 차단"}
        guarded.append(a)
    return guarded


def curate(report: dict, conn: sqlite3.Connection | None = None) -> dict:
    """검수 보고 → 결정 → 실행 → 기록. 반환: {actions, results, manual, rationale}."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        current = blog_actions.read_current()
        catalog = "\n".join(f"- {name}: {desc}"
                            for name, (desc, _) in blog_actions.ACTION_CATALOG.items())
        prompt = DECIDE_PROMPT.format(
            recent_posts=_recent_posts(conn), current=json.dumps(current, ensure_ascii=False),
            today=today, report=json.dumps(report, ensure_ascii=False)[:3000],
            prev_decision=_prev_decision(conn), catalog=catalog)
        raw = llm.chat(config.MODEL_STEER, prompt, purpose="blog-curation")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decision = json.loads(raw)

        actions = _identity_change_guard(conn, decision.get("actions", []))
        blocked = [a for a in actions if a.get("_blocked")]
        runnable = [a for a in actions if not a.get("_blocked")]

        results = []
        if runnable:
            try:
                results = blog_actions.apply(runnable)
            except Exception as e:  # 실행이 죽어도 결정 기록은 남긴다 (복기 재료)
                results = [{"action": "(전체)", "ok": False,
                            "detail": f"{type(e).__name__}: {str(e)[:200]}"}]

        outcome = {
            "purpose": "blog-curation",
            "actions": [{k: v for k, v in a.items() if k != "_blocked"} for a in runnable],
            "blocked": [a["_blocked"] for a in blocked],
            "results": results,
            "manual": decision.get("manual", []),
        }
        conn.execute(
            "INSERT INTO decisions (date, input_summary, decision_json, rationale) "
            "VALUES (?, ?, ?, ?)",
            (today, json.dumps({"inspection_issues": len(report.get("issues", [])),
                                "current": current}, ensure_ascii=False),
             json.dumps(outcome, ensure_ascii=False),
             decision.get("rationale", "")))
        conn.commit()
        return {**outcome, "rationale": decision.get("rationale", "")}
    finally:
        if own:
            conn.close()
