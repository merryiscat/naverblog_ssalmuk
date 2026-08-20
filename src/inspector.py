"""블로그 검수 에이전트 — 발행 후 우리 블로그의 '실제 화면'을 독자 눈으로 본다.

멀티 에이전트 로드맵 3순위 (docs/status.md 확정 2026-08-18). 존재 이유:
2026-08-18 신선도 사고·각주 노출·이미지 부재는 전부 **사람이 화면을 직접 봐서야**
발견됐다 — 발행 전 게이트(왕복 루프)가 텍스트를 아무리 봐도, 렌더링된 결과물과
블로그 전체 인상은 화면을 열어야만 보인다. 이 에이전트가 그 마지막 그물이다.

구조는 프로젝트 에이전트 공통 원칙(고민→검토→실행→복기, 사용자 확정 2026-08-19)을 따른다:
  고민 — 무엇을 볼지: 블로그 메인 + 최근 발행 글 (DB에서 선정)
  검토 — 실행해도 되나: 읽기 전용 브라우징뿐이라 위험 없음 (가드레일 ③ 무관)
  실행 — 스크린샷 → 비전 LLM이 독자·메이트 심사자 관점 검수
  복기 — **지난 검수의 지적을 이번 화면에서 재확인** → resolved/persisting 추적.
         같은 지적이 반복되면 C5 보정과 텔레그램에서 우선순위가 올라간다

주 3회 (화·목·토 23:15±15분 — 당일 발행분이 다 올라온 뒤, C4·C5와 무충돌).
결과는 다음날 C5 보정 입력(self_inspection)으로 들어간다.
"""

import json
import sqlite3
import time
from datetime import date

from playwright.sync_api import sync_playwright

from src import config, db, llm

INSPECT_POSTS = 2      # 최근 발행 글 몇 개를 검수할지
VIEWPORT = {"width": 1280, "height": 1600}

CHECKLIST = """너는 네이버 블로그 품질 검수자다. 첨부 스크린샷은 우리가 자동 운영하는
블로그의 실제 화면이다 — 1장째: 블로그 메인, 이후: 최근 발행 글 (글마다 상단·중단 2장).
독자와 네이버 메이트 심사자의 눈으로 문제를 찾아라. 오늘: {today}

점검 항목:
- 렌더링: 마크다운 원문 노출(##, |표|, ** 등), 표·인용구 깨짐, 각주 번호([1] 등)가
  실링크 없이 원시 텍스트로 노출
- 카테고리 매칭: 글 상단의 카테고리명이 글 주제의 분야와 맞는가
  (예: 정책 글이 '여행' 카테고리에 있으면 지적)
- 신선도: 제목·본문의 기준 시점이 과거 연도("2024년 기준" 등)로 표기
- 시각 요소: 이미지 없는 글, 밋밋한 첫인상, 썸네일 부재
- 신뢰도: 프로필(이미지·소개), 카테고리 구성(이름·개수), 블로그명과 콘텐츠의 정합
- 그 외 독자가 이탈하거나 심사자가 감점할 만한 모든 것

{prev_block}

JSON만 출력:
{{"issues": [{{"where": "메인 | 글 제목 일부", "what": "문제", "severity": "high|mid|low",
  "fix": "고치는 방법 — 코드 수정 / 수동 작업 구분"}}],
 "resolved": ["지난 지적 중 이번 화면에서 해결 확인된 것"],
 "persisting": ["지난 지적 중 여전히 남아 있는 것"],
 "positives": ["잘 되고 있는 점"],
 "overall": "심사자 관점 전체 인상 한 문단"}}"""

PREV_BLOCK = """복기 — 지난 검수({prev_date})의 지적 사항이다. 각각 이번 화면에서
해결됐는지 반드시 재확인해 resolved/persisting으로 분류하라:
{prev_issues}"""


def _recent_posts(conn: sqlite3.Connection) -> list[dict]:
    """고민 단계 — 검수 대상 선정: 가장 최근 발행 글부터 (URL + 목표 카테고리)."""
    rows = conn.execute(
        "SELECT p.publish_url, p.title, t.category FROM posts p "
        "LEFT JOIN topics t ON p.topic_id = t.id "
        "WHERE p.status IN ('published', 'verified') "
        "AND p.publish_url IS NOT NULL ORDER BY p.published_at DESC LIMIT ?",
        (INSPECT_POSTS,)).fetchall()
    posts = []
    for r in rows:
        # 발행 직후 저장된 URL의 잡다한 파라미터 제거 → 독자가 보는 PostView로 정규화
        u = r["publish_url"]
        if "logNo=" in u:
            log_no = u.split("logNo=")[1].split("&")[0]
            u = (f"https://blog.naver.com/PostView.naver"
                 f"?blogId={config.NAVER_BLOG_ID}&logNo={log_no}")
        posts.append({"url": u, "title": r["title"], "category": r["category"]})
    return posts


def capture(conn: sqlite3.Connection) -> tuple[list[bytes], list[dict]]:
    """실행 단계 1 — 비로그인(독자 관점) 브라우저로 화면을 찍는다.

    스크린샷과 함께 **코드 대조 결과**도 돌려준다: 글에 실제 표시된 카테고리를
    DOM으로 읽어 DB의 목표 분야와 기계 대조 — 비전 LLM에 기대지 않는 결정론 체크.
    (2026-08-21: 전 글이 '여행'에 발행된 오배정을 검수가 놓친 사고의 항체 —
    체크리스트에 없는 항목은 비전이 안 보고, 값 대조는 애초에 코드의 일)
    """
    shots, checks = [], []
    with sync_playwright() as pl:
        browser = pl.chromium.launch(headless=True)
        # 세션 없이 — 독자가 보는 그대로 (관리 버튼·내 계정 요소 없이)
        context = browser.new_context(viewport=VIEWPORT, user_agent=config.BROWSER_UA)
        page = context.new_page()
        try:
            page.goto(f"https://blog.naver.com/{config.NAVER_BLOG_ID}", timeout=45000)
            time.sleep(3)
            shots.append(page.screenshot())
            for post in _recent_posts(conn):
                page.goto(post["url"], timeout=45000)
                time.sleep(2)
                shots.append(page.screenshot())  # 상단
                # 코드 대조 — 글 상단 카테고리 표시 vs DB 목표 분야
                shown = page.evaluate(
                    """() => {
                        const el = document.querySelector(
                            ".blog2_series, .cate, a[href*='categoryNo']");
                        return el ? el.textContent.trim() : '';
                    }""")
                if post["category"] and shown and shown != post["category"]:
                    checks.append({
                        "where": "코드 대조",
                        "what": f"'{post['title'][:25]}' 글이 '{shown}' 카테고리에 있음 "
                                f"(목표: {post['category']})",
                        "severity": "high",
                        "fix": "발행 카테고리 반영 검증(publisher) 로그 확인 + 글 카테고리 이동"})
                page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.45)")
                time.sleep(1)
                shots.append(page.screenshot())  # 중단
        finally:
            browser.close()
    return shots, checks


def _prev_issues(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """복기 재료 — 직전 검수의 미해결 지적 (없으면 빈 값)."""
    row = conn.execute(
        "SELECT date, report_json FROM inspections ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "", []
    r = json.loads(row["report_json"] or "{}")
    # 직전 보고의 issues + persisting이 이번에 재확인할 목록
    items = [f"{i.get('where', '')}: {i.get('what', '')}" for i in r.get("issues", [])]
    items += [p for p in r.get("persisting", []) if p not in items]
    return row["date"], items


def inspect(conn: sqlite3.Connection | None = None) -> dict:
    """검수 전체 실행 — 캡처 → 비전 검수 → 복기 → inspections 기록."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        prev_date, prev_items = _prev_issues(conn)
        prev_block = (PREV_BLOCK.format(prev_date=prev_date,
                                        prev_issues="\n".join(f"- {i}" for i in prev_items))
                      if prev_items else "(첫 검수 — 지난 지적 없음)")

        shots, code_checks = capture(conn)
        raw = llm.chat_vision(config.MODEL_INSPECTOR,
                              CHECKLIST.format(today=today, prev_block=prev_block),
                              shots, purpose="blog-inspect")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            report = {"parse_error": raw[:800]}
        # 코드 대조 결과를 issues에 합류 — 텔레그램 보고·복기·오케스트레이션이 함께 처리
        if code_checks:
            report.setdefault("issues", []).extend(code_checks)

        conn.execute(
            "INSERT INTO inspections (date, report_json, shots) VALUES (?, ?, ?)",
            (today, json.dumps(report, ensure_ascii=False), len(shots)))
        conn.commit()
        return {"report": report, "shots": len(shots)}
    finally:
        if own:
            conn.close()


def recent_summary(conn: sqlite3.Connection, days: int = 4) -> dict:
    """C5 보정 입력용 — 최근 검수의 미해결 지적. 반복 지적은 그 사실을 명시한다."""
    row = conn.execute(
        "SELECT report_json FROM inspections WHERE date >= date('now', 'localtime', ?) "
        "ORDER BY id DESC LIMIT 1", (f"-{days} days",)).fetchone()
    if not row:
        return {}
    r = json.loads(row["report_json"] or "{}")
    return {
        "issues": [{"where": i.get("where"), "what": i.get("what"),
                    "severity": i.get("severity"), "fix": i.get("fix")}
                   for i in r.get("issues", [])],
        "persisting": r.get("persisting", []),  # 반복 지적 — 우선 처리 대상
        "overall": r.get("overall"),
    }
