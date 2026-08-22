"""오케스트레이터 — 에이전트들의 보고·요청을 취합해 실행 에이전트에게 작업을 지시하는 관리자.

v2 (사용자 확정 2026-08-22): 오케스트레이터는 **관리만** 한다 — 지적을 취합·분류해
작업 지시(order)로 변환하고, 실행은 실행 에이전트들이 맡는다:
  fix_settings → blog_actions  (블로그 설정: 이름·별명·소개·주제·프로필 이미지 생성)
  fix_post     → post_editor   (기발행 글 재구성 재게시 — 각주 노출 등 본문 결함)
  prevent      → blog_actions  (생성 정책 힌트 주입 — 다음 글부터 재발 방지)
  manual       → 사람          (원인 분석+지시서, 주간 리마인더 합산)

관리 핵심 원칙: **교정과 예방은 쌍이다.** 산출물 결함(각주·이미지·표·문체)이 오면
기존 글 교정(fix_post)과 새 글 재발 방지(prevent)를 함께 지시한다 — 사용자 지적
2026-08-22: "각주 수정사항이 왔을 때 실행 에이전트에게 수정하라 함과 동시에
새 글에서 반복되지 않도록 요청했었어야지".

이행 추적: 모든 지시는 resolution_attempts(작업 대장)에 남고, 다음 검수의
resolved/persisting이 심판한다 (resolver.update_outcomes). 제동은 코드가 강제한다:
같은 지적 2회 실패 → 수동 전환 / 정책 힌트 7일 냉각기 / 정체성 항목 14일 1회 /
fix_post 회차당 2건·심판 전 재수정 금지.

실행 시점: 블로그 검수(화·목·토 23:15) 직후.
"""

import json
import sqlite3
from collections import Counter
from datetime import date

from src import blog_actions, config, db, llm, post_editor, resolver
from src.publisher import PublishError

MAX_FIX_POSTS_PER_CYCLE = 2  # 회차당 글 수정 상한 — 과도한 재게시(어뷰징 신호) 방지

MANAGE_PROMPT = """너는 네이버 블로그 자동 운영의 오케스트레이터(관리자)다. 방금 나온 검수 보고와
과거 이행 이력을 취합해, 실행 에이전트들에게 내릴 작업 지시(orders)를 결정하라.
너는 직접 실행하지 않는다 — 지시는 코드가 실행 에이전트에 라우팅하며, 사람 확인 없이 실행된다.
확신 없는 지시는 내리지 말고 manual로 분류하라.

## 블로그 맥락
- 목표: 네이버 AI 브리핑 인용 → 메이트 선정. 정보 정리형 글을 자동 발행하는 블로그다
- 최근 발행 글 (fix_post 대상 후보 — post_id로 지정): {recent_posts}
- 현재 블로그 설정: {current}

## 검수 보고 (오늘 {today})
{report}

## 이행 이력 (작업 대장 — 같은 지적은 같은 issue_key 재사용, 실패한 접근 반복 금지)
{history}

## 지시 종류와 실행 에이전트
1. fix_settings — 설정 실행 에이전트. 가능한 액션:
{settings_catalog}
2. fix_post — 글 수정 실행 에이전트. post_id를 지정하면 그 글을 저장된 원문으로
   최신 발행 파이프라인(각주→매체명 변환·이미지 포함)에 태워 통째로 재게시한다.
   본문 '재구성'으로 해결되는 결함(각주 원시 노출, 구식 서식)에만 지시. value 불필요
3. prevent — 생성 정책 힌트 (다음 글부터 반영). 액션:
   - set_writer_hint: 작문 지침 힌트 (한글 200자) — 문체·구조·표 가독성·출처 표기 등
   - set_image_style_hint: 대표 이미지 스타일 힌트 (한글 200자) — 썸네일 다양성 등
4. manual — 위 수단으로 불가(코드 수정 필요, 스킨 등): 원인 분석 + 사람용 구체 지시서

## 관리 원칙 (위반은 관리 실패다)
- **교정과 예방은 쌍**: 산출물 결함 지적에는 기존 글 교정(fix_post)과 재발 방지(prevent)를
  함께 지시하라. 단, 이미 해당 결함이 파이프라인에서 수정 배포된 경우(이행 이력 참조)
  교정만 지시하면 된다
- 설정류(블로그명·소개·프로필)는 fix_settings — 콘텐츠 정체성과 어긋날 때만 변경
- 같은 issue_key에서 실패(failed)한 접근·값 반복 금지, 실패 2회면 반드시 manual
- fix_post는 정말 본문 재구성으로 해결되는 결함에만 — 최대 {max_fix} 글

JSON만 출력:
{{"orders": [
  {{"kind": "fix_settings", "issue_key": "슬러그", "action": "액션명", "value": "값", "why": "한 줄"}},
  {{"kind": "fix_post", "issue_key": "슬러그", "post_id": 0, "why": "한 줄"}},
  {{"kind": "prevent", "issue_key": "슬러그", "action": "set_writer_hint", "value": "힌트", "why": "한 줄"}},
  {{"kind": "manual", "issue_key": "슬러그", "what": "지적", "instruction": "사람용 구체 지시서", "why": "왜 수동인지"}}
 ],
 "rationale": "취합·결정의 근거 2~3문장"}}"""


def _recent_posts(conn: sqlite3.Connection, n: int = 8) -> str:
    rows = conn.execute(
        "SELECT p.id, p.title, t.category FROM posts p LEFT JOIN topics t ON p.topic_id = t.id "
        "WHERE p.status IN ('published', 'verified') ORDER BY p.published_at DESC LIMIT ?",
        (n,)).fetchall()
    return "; ".join(f"[post_id {r['id']}] ({r['category'] or '?'}) {r['title'][:35]}"
                     for r in rows) or "없음"


def _history(conn: sqlite3.Connection, n: int = 20) -> str:
    """작업 대장 요약 — issue_key 재사용·실패 반복 금지·이미 수정된 것의 판단 재료."""
    rows = conn.execute(
        "SELECT date, issue_key, approach, result, substr(attempt_json, 1, 70) aj "
        "FROM resolution_attempts ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return "\n".join(f"- {r['date']} [{r['issue_key']}] {r['approach']} → {r['result']} ({r['aj']})"
                     for r in rows) or "(없음)"


def _identity_change_guard(conn: sqlite3.Connection, orders: list[dict]) -> list[dict]:
    """정체성 항목(블로그명·별명)은 14일에 1회만 — LLM이 지시해도 코드가 제동."""
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
    out = []
    for o in orders:
        if o.get("action") in changed:
            o = {**o, "_blocked": f"{o['action']}은 최근 14일 내 이미 변경 성공 — 진동 방지 차단"}
        out.append(o)
    return out


def manage(report: dict, conn: sqlite3.Connection | None = None) -> dict:
    """취합 → 작업 지시 → 실행 에이전트 라우팅 → 대장 기록.

    반환: {orders, results, manual, blocked, rationale}
    """
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        current = blog_actions.read_current()
        settings_catalog = "\n".join(
            f"   - {name}: {desc}" for name, (desc, _) in blog_actions.ACTION_CATALOG.items()
            if name not in blog_actions.POLICY_HINT_ACTIONS)
        prompt = MANAGE_PROMPT.format(
            recent_posts=_recent_posts(conn), current=json.dumps(current, ensure_ascii=False),
            today=today, report=json.dumps(report, ensure_ascii=False)[:3000],
            history=_history(conn), settings_catalog=settings_catalog,
            max_fix=MAX_FIX_POSTS_PER_CYCLE)
        raw = llm.chat(config.MODEL_STEER, prompt, purpose="blog-curation")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decision = json.loads(raw)
        orders = decision.get("orders", [])

        # ── 코드 제동 (LLM 지시 위의 안전장치) ──
        hist = [dict(r) for r in conn.execute(
            "SELECT issue_key, approach, attempt_json, result, created_at "
            "FROM resolution_attempts ORDER BY id DESC LIMIT 40")]
        gave_up_keys = {h["issue_key"] for h in hist if h["result"] == "gave_up"}
        failed_counts = Counter(h["issue_key"] for h in hist if h["result"] == "failed")
        # 심판(다음 검수) 대기 중인 fix_post 대상 — 재수정 금지
        pending_fix_posts = set()
        for h in hist:
            if h["approach"] == "fix_post" and h["result"] == "tried":
                try:
                    pending_fix_posts.add(json.loads(h["attempt_json"]).get("post_id"))
                except (json.JSONDecodeError, TypeError):
                    pass

        orders = _identity_change_guard(conn, orders)
        runnable, blocked, manual_orders = [], [], []
        fix_post_count, seen_keys = 0, set()
        for o in orders:
            key = str(o.get("issue_key") or "unknown").strip()
            kind = o.get("kind")
            if o.get("_blocked"):
                blocked.append(o["_blocked"])
                continue
            if key in seen_keys:
                continue  # 같은 지적은 회차당 1지시
            seen_keys.add(key)
            if key in gave_up_keys and kind != "manual":
                blocked.append(f"[{key}] 이미 수동 전환된 지적 — 자동 재시도 금지")
                continue
            if failed_counts.get(key, 0) >= resolver.MAX_FAILED_BEFORE_GIVEUP and kind != "manual":
                manual_orders.append({**o, "kind": "manual",
                                      "what": o.get("why", ""), "instruction":
                                      f"자동 접근 {failed_counts[key]}회 실패 — 사람 확인 필요"})
                continue
            if kind == "prevent" and resolver._hint_in_cooldown(hist, o.get("action", "")):
                blocked.append(f"[{key}] {o.get('action')} 냉각기(7일) — 보류")
                continue
            if kind == "fix_post":
                if fix_post_count >= MAX_FIX_POSTS_PER_CYCLE:
                    blocked.append(f"[{key}] fix_post 회차 상한({MAX_FIX_POSTS_PER_CYCLE}건) — 보류")
                    continue
                if o.get("post_id") in pending_fix_posts:
                    blocked.append(f"[{key}] post {o.get('post_id')} 수정 효과 심판 대기 중 — 재수정 금지")
                    continue
                fix_post_count += 1
            if kind == "manual":
                manual_orders.append(o)
                continue
            runnable.append(o)

        # ── 실행 에이전트 라우팅 ──
        results = []
        action_orders = [o for o in runnable if o["kind"] in ("fix_settings", "prevent")]
        if action_orders:
            try:
                acts = [{"action": o["action"], "value": o.get("value", "")} for o in action_orders]
                res = blog_actions.apply(acts)
                for o, r in zip(action_orders, res):
                    results.append({**r, "kind": o["kind"], "issue_key": o["issue_key"]})
            except Exception as e:  # 실행이 죽어도 기록은 남긴다
                for o in action_orders:
                    results.append({"kind": o["kind"], "issue_key": o["issue_key"],
                                    "action": o.get("action"), "ok": False,
                                    "detail": f"{type(e).__name__}: {str(e)[:150]}"})
        for o in [o for o in runnable if o["kind"] == "fix_post"]:
            try:
                r = post_editor.update_post(int(o["post_id"]), conn)
                results.append({"kind": "fix_post", "issue_key": o["issue_key"],
                                "action": f"post {o['post_id']} 재게시",
                                "ok": r["ok"], "detail": r["detail"]})
            except Exception as e:  # PublishError 포함 — 재시도 금지, 보고만
                results.append({"kind": "fix_post", "issue_key": o["issue_key"],
                                "action": f"post {o.get('post_id')} 재게시", "ok": False,
                                "detail": f"{type(e).__name__}: {str(e)[:150]}"})

        # ── 작업 대장 기록 — 다음 검수가 심판한다 ──
        by_key = {r["issue_key"]: r for r in results}
        for o in runnable:
            r = by_key.get(o["issue_key"], {})
            conn.execute(
                "INSERT INTO resolution_attempts (date, issue_key, issue_text, diagnosis, "
                "approach, attempt_json, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (today, o["issue_key"], str(o.get("why") or "")[:300], str(o.get("why") or ""),
                 o["kind"],
                 json.dumps({k: v for k, v in o.items() if not k.startswith("_")},
                            ensure_ascii=False),
                 "tried" if r.get("ok") else "failed"))
        for o in manual_orders:
            conn.execute(
                "INSERT INTO resolution_attempts (date, issue_key, issue_text, diagnosis, "
                "approach, attempt_json, result) VALUES (?, ?, ?, ?, 'manual', ?, 'gave_up')",
                (today, str(o.get("issue_key") or "unknown"), str(o.get("what") or "")[:300],
                 str(o.get("why") or ""),
                 json.dumps({"cause": o.get("why"), "instruction": o.get("instruction")},
                            ensure_ascii=False)))

        outcome = {
            "purpose": "blog-curation",  # _prev_decision·정체성 가드의 검색 키 — 유지
            "orders": [{k: v for k, v in o.items() if not k.startswith("_")} for o in runnable],
            "results": results,
            "manual": [{"what": o.get("what"), "why": o.get("why"),
                        "instruction": o.get("instruction")} for o in manual_orders],
            "blocked": blocked,
        }
        conn.execute(
            "INSERT INTO decisions (date, input_summary, decision_json, rationale) "
            "VALUES (?, ?, ?, ?)",
            (today, json.dumps({"inspection_issues": len(report.get("issues", [])),
                                "persisting": len(report.get("persisting", []))},
                               ensure_ascii=False),
             json.dumps(outcome, ensure_ascii=False), decision.get("rationale", "")))
        conn.commit()
        return {**outcome, "rationale": decision.get("rationale", "")}
    finally:
        if own:
            conn.close()
