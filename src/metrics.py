"""C4 측정 — 매일의 계기판 데이터를 수집한다.

수집 항목 (loop-design.md 계기판):
  ① 프로필 인용수 3수치 (누적/당월/선정기준월) — 위젯 툴팁 파싱 (실측한 셀렉터)
     ※ "100 미만"은 숫자가 아니므로 원문 그대로 보존, 숫자면 정수로도 저장
  ② 발행 글의 블로그 검색 순위 — 검색 API로 내 글 위치 확인 (스크래핑 아님)
  ③ 글별 ai_cited — 타깃 쿼리의 검색 페이지에서 AI 브리핑 존재 여부 +
     브리핑 출처에 내 블로그가 있는지 (초기 국면의 1차 신호)

②는 API라 부담 없고, ①③은 브라우저라 글 수만큼만 최소로 연다 (쿼리 예산).
"""

import json
import re
import sqlite3
import time
from datetime import date

from playwright.sync_api import sync_playwright

from src import config, db
from src.naver_api import openapi_search

MY_BLOG_PREFIX = f"blog.naver.com/{config.NAVER_BLOG_ID}"


def _parse_count(raw: str) -> int | None:
    """'100 미만' → None, '1,234' → 1234."""
    m = re.search(r"[\d,]+", raw or "")
    if not m or "미만" in (raw or ""):
        return None
    try:
        return int(m.group().replace(",", ""))
    except ValueError:
        return None


def collect_citations(page) -> dict:
    """블로그 프로필의 인용수 위젯·툴팁을 파싱한다 (실측 셀렉터: mate-analysis)."""
    page.goto(f"https://blog.naver.com/{config.NAVER_BLOG_ID}", timeout=45000)
    time.sleep(2)
    data = page.evaluate(
        """() => {
            const iframe = document.querySelector('iframe#mainFrame');
            const doc = iframe ? iframe.contentDocument : document;
            const widget = doc.querySelector('span.nmate_count');
            const tooltip = doc.querySelector('.nmate_tooltip .info');
            return { widget: widget ? widget.textContent.trim() : null,
                     tooltip: tooltip ? tooltip.textContent.replace(/\\s+/g, ' ').trim() : null };
        }"""
    )
    result = {"widget_raw": data.get("widget"), "tooltip_raw": data.get("tooltip")}
    tip = data.get("tooltip") or ""
    # 툴팁 형식: "누적 인용수 : X 8월 인용수 : Y 6월 인용수 (8월 대상자 선정 기준) : Z"
    # '100 미만'이 먼저 잡히도록 '미만' 패턴을 앞에 둔다 (숫자만 잡히면 오판)
    for key, pat in (("cumulative", r"누적 인용수\s*:\s*([\d,]+\s*미만|[\d,]+)"),
                     ("this_month", r"\d+월 인용수\s*:\s*([\d,]+\s*미만|[\d,]+)"),
                     ("basis_month", r"선정 기준\)\s*:\s*([\d,]+\s*미만|[\d,]+)")):
        m = re.search(pat, tip)
        if m:
            result[key] = m.group(1).strip()
            result[key + "_n"] = _parse_count(m.group(1))
    return result


def check_rank(keyword: str) -> int | None:
    """블로그 검색 API에서 내 글의 순위 (1-base). 30위 밖이면 None."""
    items = openapi_search("blog", keyword, display=30).get("items", [])
    for i, item in enumerate(items):
        if MY_BLOG_PREFIX in item.get("link", ""):
            return i + 1
    return None


def check_ai_cited(page, keyword: str) -> dict:
    """검색 페이지에서 AI 브리핑 유무 + 내 블로그 인용 여부를 관찰한다.

    브리핑 블록을 정확히 못 찾으면 근사 판정임을 approx로 표시한다.
    """
    page.goto(f"https://search.naver.com/search.naver?query={keyword}", timeout=45000)
    time.sleep(2)
    return page.evaluate(
        """(myPrefix) => {
            const text = document.body.innerText;
            const hasBriefing = text.includes('AI 브리핑');
            if (!hasBriefing) return { briefing: false, cited: false, approx: false };
            // 브리핑 블록 추정: 'AI 브리핑' 라벨을 포함한 최상위 섹션
            const label = [...document.querySelectorAll('*')].find(
                e => e.children.length === 0 && e.textContent.trim() === 'AI 브리핑');
            let root = label;
            for (let i = 0; root && i < 8; i++) {
                if (root.querySelectorAll('a').length > 3) break;
                root = root.parentElement;
            }
            if (root) {
                const cited = [...root.querySelectorAll('a')]
                    .some(a => (a.href || '').includes(myPrefix));
                return { briefing: true, cited, approx: false };
            }
            // 블록 특정 실패 — 페이지 전체에서 근사 판정 (하단 일반 결과 오탐 가능)
            const cited = [...document.querySelectorAll('#main_pack a')]
                .some(a => (a.href || '').includes(myPrefix));
            return { briefing: true, cited, approx: true };
        }""",
        MY_BLOG_PREFIX,
    )


def collect(conn: sqlite3.Connection | None = None) -> dict:
    """일일 측정 전체 실행 — metrics·rankings 기록 후 요약 반환."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        posts = conn.execute(
            "SELECT p.id, p.publish_url, t.keyword FROM posts p "
            "LEFT JOIN topics t ON p.topic_id = t.id "
            "WHERE p.status = 'verified'"
        ).fetchall()

        with sync_playwright() as pl:
            browser = pl.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                          user_agent=config.BROWSER_UA)
            page = context.new_page()
            try:
                citations = collect_citations(page)

                ranks = []
                for post in posts:
                    kw = post["keyword"]
                    if not kw:
                        continue
                    rank = check_rank(kw)
                    cited = check_ai_cited(page, kw)
                    conn.execute(
                        "INSERT OR REPLACE INTO rankings (post_id, date, keyword, rank, ai_cited) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (post["id"], today, kw, rank, 1 if cited.get("cited") else 0))
                    ranks.append({"keyword": kw, "rank": rank, **cited})
                    time.sleep(3)  # 검색 페이지 관찰 간격 (사람 같은 속도)
            finally:
                browser.close()

        conn.execute(
            "INSERT OR REPLACE INTO metrics (date, citations, details_json) VALUES (?, ?, ?)",
            (today, citations.get("cumulative_n"),
             json.dumps({"citations": citations, "ranks": ranks}, ensure_ascii=False)))
        conn.commit()
        return {"citations": citations, "ranks": ranks}
    finally:
        if own:
            conn.close()
