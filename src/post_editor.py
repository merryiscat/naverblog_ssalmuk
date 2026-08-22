"""글 수정 실행 에이전트 — 기발행 글을 저장된 원문으로 재구성해 재게시한다.

오케스트레이터 v2(관리자·실행 분리, 사용자 확정 2026-08-22)의 실행 에이전트 중 하나.
설정은 blog_actions가, 발행 글 본문은 이 모듈이 담당한다.

용도: 본문 '재구성'으로 해결되는 결함 — 각주 원시 노출([1][3]), 구식 서식 등.
원문 마크다운(body_path)과 출처(sources_json)가 DB에 있으므로, 최신 발행 파이프라인
(publisher.build_final_body — 각주→매체명 변환 포함)으로 본문을 다시 만들어
수정 화면에서 통째로 교체한다. 임의 문구 수정은 하지 않는다 — 재구성만.

파괴적 자동화 규칙 (CLAUDE.md — 8/19 오삭제 사고의 항체) 준수:
  ① 수정 직전 대상 재검증 — 에디터에 로드된 제목이 DB의 글 제목과 일치해야 진행
  ② 수정 후 공개 화면을 비로그인으로 재조회해 반영을 검증 (각주 패턴 소멸 확인)
  ③ 실패 시 자동 재시도 금지 — 보고 후 중단 (호출자가 사람에게 알림)
  ※ 원문이 DB·파일에 남아 있어 수정 실패 시에도 같은 절차로 복구 가능하다.

진입점 (정찰 2026-08-22): https://blog.naver.com/PostUpdateForm.naver?blogId=..&logNo=..
— 최초 발행과 동일한 SE ONE 에디터가 기존 글을 로드한 상태로 뜬다 (input_buffer 존재 실확인).
"""

import json
import re
import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from src import config, db
from src.publisher import (PublishError, _attach_images, _dismiss_popups, _paste_html,
                           _shot, _verify_public, build_final_body, md_to_html)

EDIT_URL = "https://blog.naver.com/PostUpdateForm.naver?blogId={blog_id}&logNo={log_no}"

# 각주 원시 노출 패턴 — 수정 후 공개 화면에서 이 패턴이 사라졌는지 검증에 쓴다
FOOTNOTE_PATTERN = r"\[\d+\]"


def _log_no(publish_url: str) -> str:
    """발행 URL에서 logNo 추출 — 쿼리형·경로형 둘 다 지원."""
    if "logNo=" in publish_url:
        return publish_url.split("logNo=")[1].split("&")[0]
    return publish_url.rstrip("/").split("/")[-1]


def _public_text(url: str) -> str:
    """비로그인으로 공개 글 본문 텍스트를 읽는다 (수정 반영 검증용)."""
    with sync_playwright() as pl:
        browser = pl.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)
            page.wait_for_selector("iframe#mainFrame, .se-main-container", timeout=15000)
            return page.evaluate(
                """() => {
                    const iframe = document.querySelector('iframe#mainFrame');
                    const doc = iframe ? iframe.contentDocument : document;
                    const main = doc.querySelector('.se-main-container') || doc.body;
                    return main ? main.innerText : '';
                }""")
        except Exception:
            return ""
        finally:
            browser.close()


def update_post(post_id: int, conn: sqlite3.Connection | None = None, *,
                headless: bool = True) -> dict:
    """글 하나를 저장된 원문으로 재구성해 재게시한다.

    반환: {ok, url, detail}. 실패는 PublishError — 재시도하지 않는다 (규칙 ③).
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row or row["status"] not in ("published", "verified") or not row["publish_url"]:
            raise PublishError(f"수정 대상 아님: post {post_id} "
                               f"(status={row['status'] if row else '없음'})")
        title = row["title"]
        log_no = _log_no(row["publish_url"])
        body_md = Path(row["body_path"]).read_text(encoding="utf-8")
        body_md = re.sub(r"^# .+\n+", "", body_md)  # 첫 줄 '# 제목'은 에디터 제목과 중복
        sources = json.loads(row["sources_json"]) if row["sources_json"] else []
        images = json.loads(row["images_json"]) if row["images_json"] else []
        html = md_to_html(build_final_body(body_md, sources))

        with sync_playwright() as pl:
            browser = pl.chromium.launch(headless=headless)
            context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                          user_agent=config.BROWSER_UA)
            page = context.new_page()
            try:
                page.goto(EDIT_URL.format(blog_id=config.NAVER_BLOG_ID, log_no=log_no),
                          timeout=45000)
                time.sleep(5)
                if "nidlogin" in page.url:
                    raise PublishError("세션 만료 — 재로그인 필요 (소환)")
                _dismiss_popups(page)

                # 규칙 ①: 대상 재검증 — 에디터에 로드된 제목이 DB 제목과 일치해야 한다.
                # logNo만 믿지 않는다 (8/19 오삭제 사고: 식별자 미검증 클릭이 원인)
                loaded = page.evaluate(
                    """() => document.querySelector(
                        '.se-documentTitle .se-text-paragraph')?.textContent.trim() || ''""")
                if not loaded or loaded.strip() != title.strip():
                    raise PublishError(
                        f"대상 재검증 실패 — 에디터 제목 '{loaded[:30]}' ≠ DB 제목 "
                        f"'{title[:30]}' (post {post_id}, logNo {log_no}) — 중단")

                # 본문 전체 선택·삭제 → 재구성 본문 주입.
                # 제목은 별도 컴포넌트(.se-documentTitle)라 본문 Ctrl+A에 포함되지 않는다.
                page.locator(".se-component.se-text").first.click()
                time.sleep(0.5)
                page.keyboard.press("Control+a")
                time.sleep(0.3)
                page.keyboard.press("Delete")
                time.sleep(1)

                # 이미지(대표 썸네일) 재첨부 — 삭제로 함께 지워졌으므로 발행과 같은 순서:
                # 본문 최상단 이미지 = 썸네일
                if images:
                    _attach_images(page, [p for p in images if p and Path(p).exists()])

                _paste_html(page, html)
                time.sleep(1.5)

                # 발행(수정 완료) 2단계 — 최초 발행과 동일한 팝업 플로우.
                # 카테고리·주제는 기존값이 유지되므로 팝업에서 건드리지 않는다
                for step in ("툴바", "팝업"):
                    clicked = page.evaluate(
                        """() => {
                            const btns = [...document.querySelectorAll('button')]
                                .filter(b => b.textContent.trim() === '발행'
                                          && b.offsetParent !== null);
                            if (!btns.length) return 'none';
                            btns[btns.length - 1].click();
                            return 'ok';
                        }""")
                    if clicked != "ok":
                        raise PublishError(f"수정 발행 버튼({step})을 찾지 못함 — 중단")
                    time.sleep(1.5)

                page.wait_for_url(
                    re.compile(r"blog\.naver\.com/.+/\d+|PostView"), timeout=30000)
                url = page.url
            except PublishError:
                raise
            except Exception as e:
                shot = _shot(page, f"edit-fail-{post_id}")
                raise PublishError(f"글 수정 자동화 실패: {e} (스크린샷: {shot}) — 재시도 금지")
            finally:
                browser.close()

        # 규칙 ②: 공개 화면 재조회 검증 — 글이 살아 있고 각주 패턴이 사라졌는지
        time.sleep(3)
        if not _verify_public(row["publish_url"]):
            return {"ok": False, "url": url,
                    "detail": "수정 후 공개 확인 실패 — 사람 확인 필요 (재시도 안 함)"}
        text = _public_text(row["publish_url"])
        leftover = len(re.findall(FOOTNOTE_PATTERN, text))
        detail = "재게시·검증 완료" + (f" (⚠️ 각주 패턴 {leftover}건 잔존)" if leftover else " (각주 패턴 0건)")
        return {"ok": True, "url": url, "detail": detail}
    finally:
        if own:
            conn.close()
