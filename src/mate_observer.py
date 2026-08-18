"""C7 메이트 관찰·분석 에이전트 — 도구를 쥔 LLM이 메이트 생태계를 탐색한다.

멀티 에이전트 로드맵 2순위 (docs/status.md 확정 2026-08-18). 다른 역할과의 경계:
  왕복 루프(writer)  — 발행 전 우리 초안을 검토
  블로그 검수(예정)   — 발행 후 우리 블로그 화면을 검수
  이 에이전트        — **남의 성공**을 본다: 메이트 프로그램 소식·정책 변화 수집 +
                       선정자 블로그를 뜯어 "뭐가 먹혔는지"를 정책 힌트로 만들어 C5에 공급

고정 파이프라인이 아니라 function calling 루프인 이유 (usecases C7):
선정자 명단의 공개 위치·형식이 유동적이라 탐색 경로를 미리 못 박을 수 없다 —
검색→열람→분석의 갈래 선택을 에이전트 재량에 맡기고, 예산(TOOL_BUDGET)으로 묶는다.

주 1회(일요일 오후, 스케줄러) 실행. 읽기 전용 브라우징 — 가드레일 ③(상호작용 금지) 무관.
"""

import json
import sqlite3
import time
from datetime import date

from playwright.sync_api import sync_playwright

from src import config, db, llm
from src.competitors import parse_post_structure
from src.naver_api import openapi_search
from src.writer import _strip_tags

TOOL_BUDGET = 14          # 회당 도구 호출 총량 — 계정 보호(검색·열람 볼륨)와 비용의 상한
PAGE_TEXT_LIMIT = 3500    # 페이지 열람 결과를 LLM에 넘길 최대 글자 수

TOOLS = [
    {"type": "function", "function": {
        "name": "search_naver",
        "description": "네이버 검색 API. kind: news(뉴스) / webkr(웹문서) / blog(블로그)",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["news", "webkr", "blog"]},
            "query": {"type": "string"}},
            "required": ["kind", "query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "URL을 열어 본문 텍스트를 읽는다 (앞부분만, 요약 아님)",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "analyze_blog_post",
        "description": "네이버 블로그 글 URL의 구조(글자수·표·이미지·헤딩·발행일)와 본문 앞부분을 분석한다",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
]

MISSION = """너는 네이버 블로그 자동 운영 시스템의 '메이트 관찰' 에이전트다. 오늘: {today}
우리 목표: 네이버 메이트(월 30만원 지원, 10개 분야에서 매월 선정)에 선정되는 것.
우리 블로그: 정보 정리·분석형 글 (분야 미수렴 — 탐색 중).

도구(검색·페이지 열람·블로그 구조 분석)로 다음을 조사하라:
1. 메이트 프로그램 최신 소식 — 최근 선정 발표, 정책·기준 변화 (뉴스·공식 채널)
2. 실제 선정자(메이트) 블로그를 2~3개 찾아 분석 — 어떤 분야·주제·글 스타일이 선정되는가
   (선정자 명단·조명 페이지·인터뷰·선정 후기 검색 → 블로그 URL 확보 → analyze_blog_post)
3. 우리 전략에 반영할 점 도출

규칙:
- 도구 호출은 총 {budget}회 이내. 넓게 검색해 갈래를 잡고, 유망한 것만 깊이 파라
- 찾은 사실(소스 URL 포함)과 너의 추정을 구분하라
- 조사를 마치면 도구 호출 없이 최종 보고를 JSON으로만 출력:
{{"mate_news": ["최근 소식·변화 — 각 항목에 소스 URL"],
 "selection_observed": {{"where": "선정자 명단/조명을 확인한 위치 URL (못 찾으면 null)",
                        "fields": "관찰된 선정 분야 분포·경향"}},
 "analyzed_blogs": [{{"url": "", "field": "", "takeaway": "이 블로그에서 배울 점 (구조·주제·스타일)"}}],
 "policy_hints": ["C5 보정에 넣을 구체 힌트 — 분야·주제·글 스타일 방향"],
 "confidence": "high|medium|low — 이번 조사의 신뢰도와 이유"}}"""


def _run_tool(page, name: str, args: dict) -> str:
    """도구 실행 — 실패해도 에이전트가 다음 수를 두도록 오류를 문자열로 돌려준다."""
    try:
        if name == "search_naver":
            items = openapi_search(args["kind"], args["query"], display=6).get("items", [])
            return json.dumps([{
                "title": _strip_tags(i.get("title", "")),
                "snippet": _strip_tags(i.get("description", ""))[:160],
                "url": i.get("link", ""),
                "date": i.get("pubDate", i.get("postdate", "")),
            } for i in items], ensure_ascii=False)
        if name == "fetch_page":
            page.goto(args["url"], timeout=30000)
            time.sleep(2)
            return (page.evaluate("() => document.body.innerText") or "")[:PAGE_TEXT_LIMIT]
        if name == "analyze_blog_post":
            st = parse_post_structure(page, args["url"])
            st["body_preview"] = (page.evaluate("() => document.body.innerText") or "")[:1200]
            return json.dumps(st, ensure_ascii=False)
        return f"알 수 없는 도구: {name}"
    except Exception as e:
        return f"도구 실행 실패: {type(e).__name__}: {e}"


def observe(conn: sqlite3.Connection | None = None) -> dict:
    """조사 전체 실행 — 에이전트 루프를 돌리고 보고를 mate_watch에 기록한다."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    messages = [{"role": "user", "content": MISSION.format(today=today, budget=TOOL_BUDGET)}]
    calls, report = 0, None
    try:
        with sync_playwright() as pl:
            browser = pl.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                          user_agent=config.BROWSER_UA)
            page = context.new_page()
            try:
                while True:
                    # 예산 소진이면 도구를 잠그고 최종 보고를 강제한다 (무한 루프 방지)
                    choice = "auto" if calls < TOOL_BUDGET else "none"
                    msg = llm.chat_tools(config.MODEL_AGENT, messages, TOOLS,
                                         purpose="mate-observe", tool_choice=choice)
                    if msg.tool_calls:
                        messages.append(msg)
                        for tc in msg.tool_calls:
                            calls += 1
                            args = json.loads(tc.function.arguments or "{}")
                            result = _run_tool(page, tc.function.name, args)
                            messages.append({"role": "tool", "tool_call_id": tc.id,
                                             "content": result})
                            time.sleep(2)  # 검색·열람 간격 — 계정 보호가 관찰보다 우선
                        continue
                    raw = (msg.content or "").strip()
                    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    try:
                        report = json.loads(raw)
                    except json.JSONDecodeError:
                        report = {"parse_error": raw[:800]}
                    break
            finally:
                browser.close()

        conn.execute(
            "INSERT INTO mate_watch (date, report_json, tool_calls) VALUES (?, ?, ?)",
            (today, json.dumps(report, ensure_ascii=False), calls))
        conn.commit()
        return {"report": report, "tool_calls": calls}
    finally:
        if own:
            conn.close()


def recent_summary(conn: sqlite3.Connection, days: int = 14) -> dict:
    """C5 보정 입력용 — 최근 관찰 보고의 핵심 (없으면 빈 dict)."""
    row = conn.execute(
        "SELECT report_json FROM mate_watch WHERE date >= date('now', 'localtime', ?) "
        "ORDER BY id DESC LIMIT 1", (f"-{days} days",)).fetchone()
    if not row:
        return {}
    r = json.loads(row["report_json"] or "{}")
    return {"policy_hints": r.get("policy_hints", []),
            "fields_observed": (r.get("selection_observed") or {}).get("fields"),
            "confidence": r.get("confidence")}
