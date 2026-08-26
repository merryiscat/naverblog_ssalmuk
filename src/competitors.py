"""C6 경쟁 블로그 관찰 — AI 브리핑에 실제 인용되는 글을 뜯어 배우고, 공백을 찾는다.

주 2회(스케줄러) 실행. 두 가지 산출물 (usecases C6 + mate-analysis 전략):
  ① 인용 글 구조 패턴 — 브리핑 출처 블로그 글의 헤딩·표·이미지·글자수·발행일
     → 보정(C5)의 재료 (우리 템플릿과의 격차 확인)
  ② 인용 공백 스나이핑 — 브리핑은 뜨는데 가장 신선한 출처가 STALE_DAYS 이상 낡은 쿼리
     → 그 주제를 새 글로 치면 인용을 가져올 1순위 기회 (영화구름 사례로 실증)

쿼리 예산: 회당 키워드 OBSERVE_BUDGET개, 검색 간 간격 수 초 — 계정 보호가 관찰보다 우선.
"""

import json
import re
import sqlite3
import time
from datetime import date

from playwright.sync_api import sync_playwright

from src import config, db

OBSERVE_BUDGET = 8     # 회당 관찰 키워드 수
STALE_DAYS = 21        # 출처가 이보다 낡으면 '공백' 판정
MY_BLOG_PREFIX = f"blog.naver.com/{config.NAVER_BLOG_ID}"


def pick_keywords(conn: sqlite3.Connection) -> list[str]:
    """관찰 대상 키워드 — 최근 7일 주제 풀 상위 + 발행 글 키워드."""
    rows = conn.execute(
        "SELECT DISTINCT keyword FROM topics WHERE date >= date('now', 'localtime', '-7 days') "
        "AND status IN ('selected', 'reserve', 'candidate') ORDER BY llm_score DESC LIMIT ?",
        (OBSERVE_BUDGET,)).fetchall()
    return [r["keyword"] for r in rows]


def observe_keyword(page, keyword: str) -> dict:
    """검색 페이지에서 브리핑 존재·인용 출처 링크를 수집한다."""
    page.goto(f"https://search.naver.com/search.naver?query={keyword}", timeout=45000)
    time.sleep(2)
    return page.evaluate(
        """() => {
            const text = document.body.innerText;
            if (!text.includes('AI 브리핑')) return { briefing: false, sources: [] };
            const label = [...document.querySelectorAll('*')].find(
                e => e.children.length === 0 && e.textContent.trim() === 'AI 브리핑');
            if (!label) return { briefing: true, sources: [], scoped: false };
            // AI 브리핑 박스를 안정적 클래스로 특정 (2026-08-26 DOM 규명)
            let box = label.closest('.api_subject_bx');
            if (!box) {
                box = label;
                for (let i = 0; box && i < 12; i++) {
                    if (box.classList && box.classList.contains('api_subject_bx')) break;
                    box = box.parentElement;
                }
            }
            if (!box) return { briefing: true, sources: [], scoped: false };
            const links = [...box.querySelectorAll('a')].map(a => a.href)
                .filter(h => /blog\\.naver\\.com\\/[^/]+\\/\\d+/.test(h));
            return { briefing: true, sources: [...new Set(links)].slice(0, 4), scoped: true };
        }""")


def parse_post_structure(page, url: str) -> dict:
    """인용된 블로그 글의 구조를 파싱한다 (모바일 뷰 — 파싱이 단순)."""
    m_url = url.replace("blog.naver.com", "m.blog.naver.com").split("?")[0]
    page.goto(m_url, timeout=45000)
    time.sleep(2)
    return page.evaluate(
        """() => {
            const doc = document;
            const text = doc.body.innerText || '';
            // 발행일: '2026. 7. 1.' 형태
            const dm = text.match(/20\\d{2}\\.\\s?\\d{1,2}\\.\\s?\\d{1,2}\\./);
            return {
                chars: text.length,
                tables: doc.querySelectorAll('table, .se-table').length,
                images: doc.querySelectorAll('.se-image, .se-component img').length,
                headings: doc.querySelectorAll('.se-sectionTitle, .se-section-sectionTitle, b, strong').length,
                date: dm ? dm[0] : null,
                title: doc.title.slice(0, 80),
            };
        }""")


def _days_old(date_str: str | None) -> int | None:
    if not date_str:
        return None
    m = re.match(r"(20\d{2})\.\s?(\d{1,2})\.\s?(\d{1,2})", date_str)
    if not m:
        return None
    return (date.today() - date(int(m[1]), int(m[2]), int(m[3]))).days


def observe(conn: sqlite3.Connection | None = None) -> dict:
    """관찰 전체 실행 — competitors 기록 + 공백 기회 목록 반환."""
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    opportunities, patterns, no_briefing = [], [], []
    try:
        keywords = pick_keywords(conn)
        with sync_playwright() as pl:
            browser = pl.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                          user_agent=config.BROWSER_UA)
            page = context.new_page()
            try:
                for kw in keywords:
                    obs = observe_keyword(page, kw)
                    if not obs["briefing"]:
                        no_briefing.append(kw)
                        conn.execute(
                            "INSERT INTO competitors (date, keyword, url, structure_json) "
                            "VALUES (?, ?, '', ?)",
                            (today, kw, json.dumps({"briefing": False})))
                        time.sleep(4)
                        continue

                    freshest = None
                    for url in obs["sources"]:
                        if MY_BLOG_PREFIX in url:
                            continue  # 내 글이 인용 중 — 최고의 신호, 측정(C4)이 담당
                        st = parse_post_structure(page, url)
                        st["days_old"] = _days_old(st.get("date"))
                        conn.execute(
                            "INSERT INTO competitors (date, keyword, url, structure_json) "
                            "VALUES (?, ?, ?, ?)",
                            (today, kw, url, json.dumps(st, ensure_ascii=False)))
                        patterns.append({"keyword": kw, **st})
                        if st["days_old"] is not None and (freshest is None or st["days_old"] < freshest):
                            freshest = st["days_old"]
                        time.sleep(3)

                    # 공백 판정: 브리핑은 뜨는데 가장 신선한 출처가 낡음 → 스나이핑 기회
                    if freshest is not None and freshest >= STALE_DAYS:
                        opportunities.append({"keyword": kw, "freshest_days": freshest})
                    time.sleep(4)
            finally:
                browser.close()
        conn.commit()
        return {"observed": len(keywords), "opportunities": opportunities,
                "patterns": patterns, "no_briefing": no_briefing}
    finally:
        if own:
            conn.close()


def recent_summary(conn: sqlite3.Connection, days: int = 7) -> dict:
    """C5 보정 입력용 — 최근 관찰 요약 (공백 기회·브리핑 없는 키워드)."""
    rows = conn.execute(
        "SELECT keyword, url, structure_json FROM competitors "
        "WHERE date >= date('now', 'localtime', ?)", (f"-{days} days",)).fetchall()
    opps, no_brief = [], []
    for r in rows:
        st = json.loads(r["structure_json"] or "{}")
        if not r["url"] and st.get("briefing") is False:
            no_brief.append(r["keyword"])
        elif st.get("days_old") is not None and st["days_old"] >= STALE_DAYS:
            opps.append({"keyword": r["keyword"], "days_old": st["days_old"]})
    return {"stale_opportunities": opps, "no_briefing_keywords": list(set(no_brief))}
