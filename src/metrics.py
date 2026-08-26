"""C4 측정 — 매일의 계기판 데이터를 수집한다.

수집 항목 (loop-design.md 계기판):
  ① 프로필 인용수 3수치 (누적/당월/선정기준월) — 위젯 툴팁 파싱 (실측한 셀렉터)
     ※ "100 미만"은 숫자가 아니므로 원문 그대로 보존, 숫자면 정수로도 저장
  ② 발행 글의 블로그 검색 순위 — 검색 API로 내 글 위치 확인 (스크래핑 아님)
  ③ 글별 ai_cited — 타깃 쿼리의 검색 페이지에서 AI 브리핑 존재 여부 +
     브리핑 출처에 내 블로그가 있는지 (초기 국면의 1차 신호)
  ④ 방문자수 — 모바일 블로그 공개 API (오늘/누적/이웃수, 로그인·브라우저 불필요).
     애드포스트 임계 판단 재료 (pending: 광고 도입 조건, 2026-08-18)

②④는 API라 부담 없고, ①③은 브라우저라 글 수만큼만 최소로 연다 (쿼리 예산).
"""

import json
import re
import sqlite3
import time
import urllib.request
from datetime import date, datetime

from playwright.sync_api import sync_playwright

from src import config, db, notify
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
    # 툴팁 형식: "누적 인용수 : X  {N}월 인용수 : Y  {N-2}월 인용수 ({N}월 대상자 선정 기준) : Z"
    # ⚠ 값과 다음 라벨 사이에 공백이 없을 수 있다("...130 8월...130 6월..." → "1306월")
    # → 다음 라벨의 '월 숫자'를 알아야 값 경계를 안다. 당월·기준월(당월-2)을 계산해
    # lookahead로 경계를 고정한다 (2026-08-26: 8월 인용수 130이 1306으로 오파싱된 버그 항체).
    mon = date.today().month
    basis = mon - 2 if mon > 2 else mon + 10
    for key, pat in (
        ("cumulative", rf"누적 인용수\s*:\s*([\d,]+\s*미만|[\d,]+?)(?=\s*{mon}월|\s*$)"),
        ("this_month", rf"{mon}월 인용수\s*:\s*([\d,]+\s*미만|[\d,]+?)(?=\s*{basis}월|\s*\(|\s*$)"),
        ("basis_month", r"선정 기준\)\s*:\s*([\d,]+\s*미만|[\d,]+)"),
    ):
        m = re.search(pat, tip)
        if m:
            result[key] = m.group(1).strip()
            result[key + "_n"] = _parse_count(m.group(1))
    return result


def collect_visitors() -> dict:
    """모바일 블로그 공개 API에서 방문자수를 가져온다 (측정 실패는 비치명 — 빈 dict).

    C4가 21:30±30분에 돌므로 dayVisitorCount ≈ 당일 방문자 거의 전체.
    """
    url = f"https://m.blog.naver.com/api/blogs/{config.NAVER_BLOG_ID}"
    try:
        # Referer 없으면 403 (2026-08-19 실측) — 모바일 블로그에서 온 요청처럼 보여야 응답
        req = urllib.request.Request(url, headers={
            "User-Agent": config.BROWSER_UA,
            "Referer": f"https://m.blog.naver.com/{config.NAVER_BLOG_ID}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))["result"]
        return {"today": data.get("dayVisitorCount"),
                "total": data.get("totalVisitorCount"),
                "subscribers": data.get("subscriberCount")}
    except Exception as e:
        print(f"방문자수 수집 실패 (측정은 계속): {type(e).__name__}: {e}")
        return {}


def check_rank(keyword: str) -> int | None:
    """블로그 검색 API에서 내 글의 순위 (1-base). 30위 밖이면 None."""
    items = openapi_search("blog", keyword, display=30).get("items", [])
    for i, item in enumerate(items):
        if MY_BLOG_PREFIX in item.get("link", ""):
            return i + 1
    return None


def check_indexed(title: str) -> bool:
    """제목 검색으로 색인 여부를 판정한다 — 순위(check_rank)와 별개 (2026-08-22).

    rank=None은 '미색인'과 '30위 밖'을 구분하지 못한다. 글 제목은 사실상 고유
    문자열이므로 제목 검색 결과에 내 글이 나오면 색인된 것으로 본다.
    발행→색인 래그 실측(first_indexed_at)의 근거.
    """
    try:
        # 따옴표 구문 검색 우선 (정확 일치) — 미지원·무결과면 일반 검색 폴백
        for q in (f'"{title}"', title[:50]):
            items = openapi_search("blog", q, display=30).get("items", [])
            if any(MY_BLOG_PREFIX in i.get("link", "") for i in items):
                return True
        return False
    except Exception as e:
        print(f"색인 확인 실패 (측정은 계속): {type(e).__name__}: {e}")
        return False


def check_ai_cited(page, keyword: str) -> dict:
    """검색 페이지에서 AI 브리핑 유무 + 내 블로그 인용 여부를 관찰한다.

    브리핑 박스는 안정적 클래스 `.api_subject_bx`로 특정한다 (2026-08-26 DOM 규명 —
    기존 '부모 타고 링크>3개' 휴리스틱이 엉뚱한 컨테이너에서 멈춰 인용을 놓쳤다).
    ※ per-keyword는 우리가 체크하는 소수 쿼리의 표본일 뿐 — 프로필 인용수 위젯이 총량의
    권위 신호다(수백 개 롱테일 쿼리의 인용은 여기서 못 잡는다).
    """
    page.goto(f"https://search.naver.com/search.naver?query={keyword}", timeout=45000)
    time.sleep(2)
    return page.evaluate(
        """(myPrefix) => {
            const text = document.body.innerText;
            if (!text.includes('AI 브리핑')) return { briefing: false, cited: false, approx: false };
            const label = [...document.querySelectorAll('*')].find(
                e => e.children.length === 0 && e.textContent.trim() === 'AI 브리핑');
            if (!label) return { briefing: true, cited: false, approx: true };
            // AI 브리핑 라벨을 감싸는 안정적 컨테이너
            let box = label.closest('.api_subject_bx');
            if (!box) {
                box = label;
                for (let i = 0; box && i < 12; i++) {
                    if (box.classList && box.classList.contains('api_subject_bx')) break;
                    box = box.parentElement;
                }
            }
            if (!box) return { briefing: true, cited: false, approx: true };
            const cited = [...box.querySelectorAll('a')]
                .some(a => (a.href || '').includes(myPrefix));
            return { briefing: true, cited, approx: false };
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
            "SELECT p.id, p.title, p.publish_url, p.published_at, p.first_indexed_at, "
            "t.keyword FROM posts p "
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

                    # 색인 판정 — 아직 미확인 글만 제목 검색. 최초 확인 시
                    # first_indexed_at 기록 + 발행→색인 래그를 텔레그램으로 (2026-08-22)
                    indexed = 1 if post["first_indexed_at"] else 0
                    if not indexed and post["title"] and check_indexed(post["title"]):
                        indexed = 1
                        conn.execute(
                            "UPDATE posts SET first_indexed_at = datetime('now', 'localtime') "
                            "WHERE id = ?", (post["id"],))
                        lag = ""
                        if post["published_at"]:
                            days = (datetime.now()
                                    - datetime.fromisoformat(post["published_at"])).days
                            lag = f" (발행 후 {days}일)"
                        notify.send(f"🔎 검색 색인 확인: {post['title'][:40]}{lag}")

                    # 최초 인용은 Phase 전환 트리거 — 침묵 금지, 즉시 알림 (2026-08-22)
                    if cited.get("cited"):
                        prior = conn.execute(
                            "SELECT COUNT(*) AS n FROM rankings "
                            "WHERE post_id = ? AND ai_cited = 1", (post["id"],)).fetchone()["n"]
                        if prior == 0:
                            notify.send(f"🎉 AI 브리핑 인용 확인! '{kw}'\n"
                                        f"{post['title'][:40]}\n"
                                        f"(Phase 전환 트리거 — 오늘 밤 보정에 반영됨)")

                    conn.execute(
                        "INSERT OR REPLACE INTO rankings "
                        "(post_id, date, keyword, rank, ai_cited, indexed) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (post["id"], today, kw, rank, 1 if cited.get("cited") else 0, indexed))
                    ranks.append({"keyword": kw, "rank": rank, "indexed": bool(indexed), **cited})
                    time.sleep(3)  # 검색 페이지 관찰 간격 (사람 같은 속도)
            finally:
                browser.close()

        # 프로필 인용수 위젯(네이버 공식 카운트)이 우리 인용의 진짜 신호다 — per-keyword
        # 검색 파싱은 놓칠 수 있으므로(실측: 위젯 130인데 per-keyword 0), 이 값의 증가를
        # 축하 알림으로 올린다. 메이트 선정 기준 자체가 월간 인용수라 이게 곧 목표 지표다.
        cum_now = citations.get("cumulative_n")
        prev = conn.execute(
            "SELECT citations FROM metrics WHERE citations IS NOT NULL "
            "AND date < ? ORDER BY date DESC LIMIT 1", (today,)).fetchone()
        prev_cum = prev["citations"] if prev else None
        if cum_now and cum_now > (prev_cum or 0):
            month = citations.get("this_month", "?")
            notify.send(
                f"🎉 AI 브리핑 인용수 상승! 누적 {cum_now}"
                + (f" (직전 {prev_cum})" if prev_cum else " (첫 집계 — 100 돌파)")
                + f"\n8월 인용수 {month} — 메이트 선정 기준 지표\n"
                f"(per-keyword 감지는 별개로 점검 필요)")

        visitors = collect_visitors()
        conn.execute(
            "INSERT OR REPLACE INTO metrics (date, citations, visitors, details_json) "
            "VALUES (?, ?, ?, ?)",
            (today, cum_now, visitors.get("today"),
             json.dumps({"citations": citations, "ranks": ranks, "visitors": visitors},
                        ensure_ascii=False)))
        conn.commit()
        return {"citations": citations, "ranks": ranks, "visitors": visitors}
    finally:
        if own:
            conn.close()
