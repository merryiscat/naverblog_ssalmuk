"""C3 발행 — 게이트 통과 글을 스마트에디터 ONE으로 실제 발행한다.

핵심 기법 (2026-08-17 실에디터 정찰로 확립, docs/log 참조):
- SE ONE은 마크다운·HTML 모드가 없다. 대신 **input_buffer iframe에 합성 paste 이벤트로
  HTML을 주입**하면 에디터가 표·문단·굵은 소제목을 네이티브 컴포넌트로 변환한다 (실검증됨).
- 발행은 2단계: 툴바 "발행" → 설정 팝업(전체공개·검색허용 확인, 태그) → 최종 "발행".
- 검색허용은 기본 ON이지만 매번 검증한다 (P2 요구사항 — 인용 대상의 전제).
- 발행 성공을 믿지 않는다: 비로그인 컨텍스트로 공개 URL을 열어 실게시를 확인 (가짜 성공 함정).

수동 개입이 필요한 상황(캡차·기기확인·셀렉터 깨짐)이면 스크린샷을 남기고 예외를 던진다 —
호출자(스케줄러)가 텔레그램 소환을 보낸다.
"""

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import markdown as md_lib
from playwright.sync_api import sync_playwright

from src import config, db, guardrails

ERROR_SHOT_DIR = config.DATA_DIR / "error_shots"


class PublishError(Exception):
    """발행 실패 — 메시지에 원인, 스크린샷 경로 포함 가능."""


def md_to_html(body_md: str) -> str:
    """마크다운 본문 → SE ONE에 붙여넣을 HTML.

    표(tables) 확장 포함. h1은 글 제목과 중복되므로 h2로 낮춘다.
    """
    html = md_lib.markdown(body_md, extensions=["tables"])
    return html.replace("<h1>", "<h2>").replace("</h1>", "</h2>")


def build_final_body(body_md: str, sources: list[dict]) -> str:
    """본문의 출처 각주 [n]을 유지하고, 말미에 '참고 자료' 절을 붙인다.

    공식 가이드 원칙 3(원작자·원문 링크 명시) 대응. 링크 텍스트로 URL을 그대로 적는다.
    """
    if not sources:
        return body_md
    lines = ["", "## 참고 자료", ""]
    for i, s in enumerate(sources):
        lines.append(f"[{i+1}] {s['title']} — {s['url']}")
        lines.append("")
    return body_md + "\n" + "\n".join(lines)


def _shot(page, name: str) -> str:
    """오류 시점 스크린샷 저장 — 소환 알림에 첨부할 증거."""
    ERROR_SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = ERROR_SHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    try:
        page.screenshot(path=str(path))
    except Exception:
        return "(스크린샷 실패)"
    return str(path)


def _dismiss_popups(page) -> None:
    """글쓰기 진입 시 뜰 수 있는 팝업 정리.

    핵심: '작성 중인 글이 있습니다' 이어쓰기 팝업(.se-popup-alert-confirm)은
    '취소'를 눌러 새 글로 시작한다 (확인 = 이전 임시저장 불러오기라 절대 금지).
    팝업은 에디터 로딩보다 늦게 뜰 수 있어 몇 초간 반복 확인한다.
    """
    for _ in range(6):  # 최대 ~6초 폴링
        handled = page.evaluate(
            """() => {
                const popup = document.querySelector('.se-popup-alert-confirm, .se-popup-alert');
                if (popup && popup.offsetParent !== null) {
                    const cancel = [...popup.querySelectorAll('button')]
                        .find(b => b.textContent.trim() === '취소');
                    if (cancel) { cancel.click(); return 'cancel-clicked'; }
                    const ok = [...popup.querySelectorAll('button')]
                        .find(b => ['확인', '닫기'].includes(b.textContent.trim()));
                    if (ok) { ok.click(); return 'ok-clicked'; }
                }
                return 'none';
            }"""
        )
        if handled != "none":
            time.sleep(1)
            continue  # 팝업이 연쇄로 뜰 수 있어 한 번 더 확인
        time.sleep(1)
        # 팝업 dim 레이어가 완전히 사라졌으면 종료
        dim = page.evaluate(
            "() => { const d = document.querySelector('.se-popup-dim'); "
            "return d && d.offsetParent !== null; }")
        if not dim:
            break


def _paste_html(page, html: str) -> None:
    """input_buffer iframe에 합성 paste로 HTML 주입 (실검증된 기법)."""
    ok = page.evaluate(
        """(html) => {
            const buf = document.querySelector('iframe[id^="input_buffer"]');
            if (!buf) return 'no-buffer';
            const bdoc = buf.contentDocument;
            const target = bdoc.querySelector('[contenteditable]') || bdoc.body;
            const dt = new DataTransfer();
            dt.setData('text/html', html);
            dt.setData('text/plain', ' ');
            target.dispatchEvent(new ClipboardEvent('paste',
                { clipboardData: dt, bubbles: true, cancelable: true }));
            return 'ok';
        }""",
        html,
    )
    if ok != "ok":
        raise PublishError(f"본문 주입 실패: input_buffer iframe 없음 ({ok}) — 에디터 개편 의심")


def _verify_public(url: str) -> bool:
    """비로그인 새 컨텍스트로 공개 URL을 열어 실게시 확인 (가짜 성공 방지)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()  # storage_state 없이 = 비로그인
        try:
            page.goto(url, timeout=30000)
            # 글 본문 iframe(mainFrame) 또는 본문 영역이 있으면 게시된 것
            page.wait_for_selector("iframe#mainFrame, .se-main-container", timeout=15000)
            return True
        except Exception:
            return False
        finally:
            browser.close()


def publish(post_id: int, conn: sqlite3.Connection | None = None, *,
            headless: bool = True, tags: list[str] | None = None) -> dict:
    """posts 테이블의 gated 글 하나를 발행한다.

    반환: {status: 'verified'|'published', url}
    예외: GuardrailViolation(일 상한), PublishError(자동화 실패 — 소환 대상)
    """
    own = conn is None
    conn = conn or db.connect()
    try:
        # 가드레일 ①: 일 발행 상한 — 코드가 강제
        guardrails.check_daily_publish_limit(conn)

        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row or row["status"] != "gated":
            raise PublishError(f"발행 대상 아님: post {post_id} (status={row['status'] if row else '없음'})")
        title = row["title"]
        body_md = Path(row["body_path"]).read_text(encoding="utf-8")
        # 파일 첫 줄의 '# 제목'은 에디터 제목과 중복되므로 제거
        body_md = re.sub(r"^# .+\n+", "", body_md)
        sources = json.loads(row["sources_json"]) if row["sources_json"] else []
        html = md_to_html(build_final_body(body_md, sources))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(config.SESSION_PATH),
                user_agent=config.BROWSER_UA,  # HeadlessChrome UA는 세션 거부됨
            )
            page = context.new_page()
            try:
                page.goto(f"https://blog.naver.com/{config.NAVER_BLOG_ID}/postwrite",
                          timeout=45000)
                time.sleep(3)  # 에디터 로딩
                if "nidlogin" in page.url:
                    raise PublishError("세션 만료 — 재로그인 필요 (소환)")
                _dismiss_popups(page)

                # 제목 입력 — documentTitle 컴포넌트 클릭 후 타이핑
                page.locator(".se-component.se-documentTitle").click()
                time.sleep(0.5)
                page.keyboard.type(title, delay=30)

                # 본문 — 본문 영역 클릭 후 HTML 주입
                page.locator(".se-component.se-text").last.click()
                time.sleep(0.5)
                _paste_html(page, html)
                time.sleep(1.5)

                # 발행 1단계 — 툴바 발행 버튼
                # 주의: has-text는 부분 일치라 '예약 발행 0건'(숨김)에 걸린다
                #       → 텍스트 정확 일치 + 화면 표시 중인 버튼만 JS로 클릭
                clicked = page.evaluate(
                    """() => {
                        const btns = [...document.querySelectorAll('button')]
                            .filter(b => b.textContent.trim() === '발행'
                                      && b.offsetParent !== null);
                        if (!btns.length) return 'none';
                        btns[0].click();
                        return 'ok';
                    }""")
                if clicked != "ok":
                    raise PublishError("툴바 발행 버튼을 찾지 못함 — 에디터 개편 의심")
                time.sleep(1.5)

                # 발행 팝업: 전체공개·검색허용 검증 (P2 — 인용 대상의 전제 조건)
                state = page.evaluate(
                    """() => {
                        const radios = [...document.querySelectorAll('input[name=open_type]')];
                        const labels = [...document.querySelectorAll('label, span')];
                        const findChk = (word) => {
                            const el = labels.find(l => l.textContent.trim().startsWith(word));
                            if (!el) return null;
                            const box = el.closest('div, li');
                            const chk = box && box.querySelector('input[type=checkbox]');
                            return chk ? chk.checked : null;
                        };
                        return { openAll: radios.length ? radios[0].checked : null,
                                 search: findChk('검색허용') };
                    }"""
                )
                if state["openAll"] is False:
                    page.evaluate("() => document.querySelector('input[name=open_type]').click()")
                if state["search"] is False:
                    raise PublishError("검색허용이 꺼져 있음 — 확인 필요 (소환)")

                # 태그 입력 (선택)
                for tag in (tags or [])[:5]:
                    try:
                        ti = page.locator("input[placeholder*='태그 입력']")
                        ti.click(timeout=3000)
                        ti.type(tag)
                        page.keyboard.press("Enter")
                    except Exception:
                        break  # 태그는 실패해도 발행 진행

                # 발행 2단계 — 팝업의 최종 발행 버튼 (표시 중인 '발행' 중 마지막)
                clicked = page.evaluate(
                    """() => {
                        const btns = [...document.querySelectorAll('button')]
                            .filter(b => b.textContent.trim() === '발행'
                                      && b.offsetParent !== null);
                        if (btns.length < 1) return 'none';
                        btns[btns.length - 1].click();
                        return 'ok';
                    }""")
                if clicked != "ok":
                    raise PublishError("발행 팝업의 최종 발행 버튼을 찾지 못함")

                # 발행 완료 → 글 보기 페이지로 이동 대기
                page.wait_for_url(
                    re.compile(r"blog\.naver\.com/.+/\d+|PostView"), timeout=30000)
                url = page.url
            except PublishError:
                raise
            except Exception as e:
                shot = _shot(page, "publish-fail")
                raise PublishError(f"발행 자동화 실패: {e} (스크린샷: {shot})")
            finally:
                browser.close()

        # 실게시 검증 (비로그인) — 성공 전엔 published, 확인되면 verified
        conn.execute(
            "UPDATE posts SET status='published', publish_url=?, "
            "published_at=datetime('now', 'localtime') WHERE id=?", (url, post_id))
        conn.commit()

        if _verify_public(url):
            conn.execute(
                "UPDATE posts SET status='verified', verified_at=datetime('now', 'localtime') "
                "WHERE id=?", (post_id,))
            conn.commit()
            return {"status": "verified", "url": url}
        return {"status": "published", "url": url}  # 검증 실패 — 호출자가 알림 (재시도 금지)
    finally:
        if own:
            conn.close()
