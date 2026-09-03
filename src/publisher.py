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

# 분야 → 네이버 블로그 '주제' 매핑 (발행 팝업의 32개 주제 중 — 2026-08-17 실팝업 목록)
# 글마다 주제를 지정하면 메이트 분야 자동 분류에 신호를 준다
NAVER_SUBJECT_BY_FIELD = {
    "미디어": "방송", "테크": "IT·컴퓨터", "인사이트": "비즈니스·경제",
    "여행": "국내여행", "푸드": "맛집", "레시피": "요리·레시피",
    "라이프": "일상·생각", "스타일": "패션·미용", "컬쳐": "공연·전시", "취미": "취미",
}

# 발행 카테고리 라우팅 — 완전 정책 특화(2026-08-30)에 맞춘 상위 축 5개 (사용자 확정 2026-08-31).
# 내부 분야(라이프/인사이트)와 분리 — 그건 주제 매핑·팔레트에 그대로 쓰고, 발행 카테고리만
# 키워드로 아래로 분류한다. 순서=우선순위(위에서 먼저 매칭). '블로그 안내'는 공지·대표글
# 전용이라 자동 배정하지 않는다. 어느 규칙에도 안 맞는 정부 정보 글(불법사이트·안전 안내 등)은
# 기본값 '생활·정책정보'로 간다 (레퍼런스 발굴이 축 밖 주제를 물어오는 것의 집, 2026-08-31).
# 카테고리명은 사용자가 관리화면에 만들 이름과 정확히 일치해야 한다.
_PUBLISH_CATEGORY_RULES = [
    ("퇴직연금·노후자금", ("퇴직연금", "퇴직금", "irp", "dc형", "확정기여", "확정급여",
                          "중도인출", "연금저축", "국민연금", "노후")),
    ("소상공인·자영업", ("소상공인", "노란우산", "자영업", "정책자금", "사업자", "창업",
                        "폐업", "간이과세", "부가세", "하도급")),
    ("노동·고용", ("실업급여", "4대보험", "주휴수당", "국민취업", "근로장려금", "최저임금",
                  "고용보험", "육아휴직", "실업", "구직", "퇴사", "연차", "노동")),
    ("지원금·수당", ("지원금", "수당", "바우처", "보조금", "장려금", "출산", "출생", "육아",
                    "양육", "아동", "청년", "문화패스", "행복페이", "복지", "돌봄")),
]
DEFAULT_PUBLISH_CATEGORY = "생활·정책정보"


def _publish_category(text: str) -> str:
    """글 키워드·제목으로 발행 카테고리(위 4개 중 하나)를 정한다 — 없으면 기본(생활·정책정보)."""
    t = (text or "").lower()
    for name, kws in _PUBLISH_CATEGORY_RULES:
        if any(k in t for k in kws):
            return name
    return DEFAULT_PUBLISH_CATEGORY


class PublishError(Exception):
    """발행 실패 — 메시지에 원인, 스크린샷 경로 포함 가능."""


def md_to_html(body_md: str) -> str:
    """마크다운 본문 → SE ONE에 붙여넣을 HTML.

    표(tables) 확장 포함. h1은 글 제목과 중복되므로 h2로 낮춘다.
    """
    html = md_lib.markdown(body_md, extensions=["tables"])
    return html.replace("<h1>", "<h2>").replace("</h1>", "</h2>")


# 흔한 출처 도메인 → 읽기 좋은 매체·기관명 (인라인 "(출처: …)" 표기용).
# 정부·공공 도메인은 그대로도 신뢰 신호라 매핑 없으면 도메인을 쓴다.
_MEDIA_NAMES = {
    "namu.wiki": "나무위키", "ko.wikipedia.org": "위키백과", "en.wikipedia.org": "위키백과",
    "n.news.naver.com": "네이버뉴스", "news.naver.com": "네이버뉴스",
    "sentv.co.kr": "서울경제TV",
    "blog.naver.com": "네이버블로그", "post.naver.com": "네이버포스트",
    "easylaw.go.kr": "찾기쉬운 생활법령정보", "bokjiro.go.kr": "복지로",
    "work24.go.kr": "고용24", "bizinfo.go.kr": "기업마당",
    "hometax.go.kr": "홈택스", "nts.go.kr": "국세청", "gov.kr": "정부24",
    "moel.go.kr": "고용노동부", "mohw.go.kr": "보건복지부",
    "semas.or.kr": "소상공인시장진흥공단", "kinfa.or.kr": "서민금융진흥원",
}


def _source_label(s: dict) -> str:
    """출처 표기용 매체·기관명 — 흔한 도메인은 한글 이름으로, 없으면 도메인."""
    from urllib.parse import urlparse
    d = urlparse(s.get("url", "")).netloc.replace("www.", "")
    # ols.semas.or.kr 처럼 서브도메인이 붙어도 매핑되게 뒤에서부터 확인
    for dom, name in _MEDIA_NAMES.items():
        if d == dom or d.endswith("." + dom):
            return name
    return d or (s.get("title", "") or "")[:20]


def _footnotes_to_labels(body_md: str, sources: list[dict]) -> str:
    """본문의 각주 번호 [n]을 '그 소스로 가는 하이퍼링크가 걸린 매체명'으로 바꾼다.

    2026-08-22: [n] → "(출처: 도메인)" 평문 (독자에겐 번호가 무의미했던 근본 수정).
    2026-08-31: 출처가 너무 뭉뚱그려진다는 지적 → 정밀화. (1) 매체명에 그 정확한
    기사 URL을 하이퍼링크(클릭 검증 가능), (2) '2개+외' 절단 제거(뒷받침 소스 전부 표기).
    SE ONE이 붙여넣기 링크를 se-link 노드로 보존함을 실측 확인. md_to_html이 인라인
    <a>를 그대로 통과시키므로 앵커를 직접 emit한다. 소스가 실제 뒷받침하는 번호만 남긴다.
    """
    def run_to_label(match: re.Match) -> str:
        parts, seen = [], set()
        for n in re.findall(r"\[(\d+)\]", match.group(0)):
            i = int(n) - 1
            if 0 <= i < len(sources) and sources[i].get("url"):
                lb = _source_label(sources[i])
                if lb and lb not in seen:
                    seen.add(lb)
                    parts.append(f'<a href="{sources[i]["url"]}">{lb}</a>')
        if not parts:
            return ""  # 매칭되는 소스가 없는 번호는 그냥 제거
        return "(출처: " + ", ".join(parts) + ")"

    # ① "(출처: [3])"·"(출처: [1], [4])" 형태 — 괄호째 매체명으로 치환
    body = re.sub(r"\(출처:\s*(?:\[\d+\][,\s]*)+\)", run_to_label, body_md)
    # ② 남은 bare 각주 런 "[1][3][11]" — 하나의 출처 표기로 축약
    body = re.sub(r"(?:\[\d+\])+", run_to_label, body)
    return body


def build_final_body(body_md: str, sources: list[dict]) -> str:
    """본문 각주 [n]을 매체명 표기로 바꾸고, 말미에 '참고 자료' 절을 붙인다.

    공식 가이드 원칙 3(원작자·원문 링크 명시) 대응. 링크 텍스트로 URL을 그대로 적는다.
    """
    if not sources:
        return body_md
    body = _footnotes_to_labels(body_md, sources)
    lines = ["", "## 참고 자료", ""]
    for s in sources:
        lines.append(f"- {s['title']} — {s['url']}")
        lines.append("")
    return body + "\n" + "\n".join(lines)


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


def _attach_images(page, image_paths: list[str]) -> int:
    """툴바 '사진' 버튼 → 파일 선택으로 이미지 삽입 (정찰 2026-08-19: file chooser 방식 실검증).

    커서 위치에 들어가므로 제목 입력 직후에 부르면 본문 최상단 = 대표 이미지(썸네일)가 된다.
    실패해도 발행은 계속한다 — 이미지는 치명 요소가 아님.
    """
    done = 0
    for path in image_paths:
        if not Path(path).exists():
            continue
        try:
            with page.expect_file_chooser(timeout=8000) as fc:
                page.locator('button[data-name="image"].se-image-toolbar-button').click()
            fc.value.set_files(path)
            time.sleep(6)  # 업로드 완료 대기
            done += 1
        except Exception as e:
            print(f"이미지 첨부 실패 (계속 진행): {type(e).__name__}: {e}")
            break
    return done


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


def _select_category_and_subject(page, category: str, field: str | None) -> str:
    """발행 팝업에서 카테고리(분야명 동일 카테고리)와 주제를 지정한다.

    반환: 선택 후 드롭다운 버튼에 실제 표시된 카테고리명 (반영 검증용 — 호출자가
    목표와 비교해 미일치 시 알린다). UI가 어긋나면 기본값 그대로 발행 — 치명 요소가 아님.

    2026-08-21 재작성: 기존 휴리스틱("publish 근처의 짧은 텍스트 버튼")은 실발행에서
    한 번도 작동하지 않아 전 글이 기본 카테고리(여행)로 발행됐다 (mistakes.md).
    정찰로 확보한 실셀렉터 사용: 버튼 aria-label='카테고리 목록 버튼',
    항목은 드롭다운 안의 정확 일치 label (role=button).
    """
    shown = ""
    # ① 카테고리: 드롭다운 열기 → 정확 일치 라벨 클릭 → 버튼 표시 텍스트로 검증
    try:
        opened = page.evaluate(
            """() => {
                const btn = document.querySelector("button[aria-label='카테고리 목록 버튼']");
                if (!btn) return 'no-button';
                btn.click();
                return 'opened';
            }""")
        if opened == "opened":
            time.sleep(0.8)
            page.evaluate(
                """(cat) => {
                    const label = [...document.querySelectorAll('label')]
                        .find(l => l.offsetParent !== null && l.textContent.trim() === cat);
                    if (label) label.click();
                }""", category)
            time.sleep(0.8)
        shown = page.evaluate(
            """() => document.querySelector("button[aria-label='카테고리 목록 버튼']")
                     ?.textContent.trim() || ''""")
    except Exception:
        pass

    # ② 주제: '주제 선택 안 함 >' 링크 → 매핑된 주제 라벨 → 확인
    #    주제는 내부 분야(field=라이프/인사이트)로 매핑한다 — 발행 카테고리와 분리(2026-08-31)
    subject = NAVER_SUBJECT_BY_FIELD.get(field)
    if not subject:
        return shown
    try:
        opened = page.evaluate(
            """() => {
                const link = [...document.querySelectorAll('a')].find(
                    a => a.offsetParent !== null && a.closest('[class*="set_theme"]'));
                if (!link) return 'no-link';
                link.click();
                return 'opened';
            }""")
        if opened != "opened":
            return shown
        time.sleep(0.8)
        page.evaluate(
            """(subj) => {
                const label = [...document.querySelectorAll('label')]
                    .find(l => l.offsetParent !== null && l.textContent.trim() === subj);
                if (label) label.click();
                const ok = [...document.querySelectorAll('button')].find(
                    b => b.offsetParent !== null && b.textContent.trim() === '확인');
                if (ok) ok.click();
            }""", subject)
        time.sleep(0.5)
    except Exception:
        pass
    return shown


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

        row = conn.execute(
            "SELECT p.*, t.category, t.keyword FROM posts p LEFT JOIN topics t ON p.topic_id = t.id "
            "WHERE p.id = ?", (post_id,)).fetchone()
        if not row or row["status"] != "gated":
            raise PublishError(f"발행 대상 아님: post {post_id} (status={row['status'] if row else '없음'})")
        title = row["title"]
        category = row["category"]  # 분야명 = 카테고리명 (없으면 기본 카테고리로 발행)
        body_md = Path(row["body_path"]).read_text(encoding="utf-8")
        # 파일 첫 줄의 '# 제목'은 에디터 제목과 중복되므로 제거
        body_md = re.sub(r"^# .+\n+", "", body_md)
        sources = json.loads(row["sources_json"]) if row["sources_json"] else []
        images = json.loads(row["images_json"]) if row["images_json"] else []
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

                # 대표 이미지 — 제목 직후 = 본문 최상단, 첫 이미지가 썸네일이 된다
                if images:
                    attached = _attach_images(page, images)
                    print(f"이미지 첨부: {attached}/{len(images)}장")

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

                # 카테고리 선택 — 분야명과 같은 카테고리가 있으면 지정 (없으면 기본 유지)
                # 주제 지정 — 분야→네이버 주제 매핑 (실패해도 발행은 진행)
                # 선택 후 표시값을 검증해 미일치면 결과에 경고를 싣는다 (8/19~20 전 글이
                # 기본 카테고리로 발행된 사고의 항체 — 조용한 실패 금지)
                # 발행 카테고리: 키워드·제목으로 정책 상위 축 4개 중 하나로 라우팅(2026-08-31).
                # 주제는 내부 분야(category=라이프/인사이트)로 매핑 — 둘을 분리해 넘긴다.
                pub_category = _publish_category(f"{row['keyword'] or ''} {title}")
                shown = _select_category_and_subject(page, pub_category, category)
                category_warn = None
                if shown != pub_category:
                    category_warn = f"카테고리 미반영 (목표 {pub_category} / 실제 {shown or '기본값'})"

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

        # 콘텐츠 메모리 색인 — 발행 즉시 임베딩해 다음 발굴이 '관련 과거 글'로 조회 (2026-09-04)
        try:
            from src import memory
            memory.index_post(conn, post_id)
        except Exception as e:
            print(f"콘텐츠 메모리 색인 실패 (발행은 완료): {type(e).__name__}: {e}")

        if _verify_public(url):
            conn.execute(
                "UPDATE posts SET status='verified', verified_at=datetime('now', 'localtime') "
                "WHERE id=?", (post_id,))
            conn.commit()
            return {"status": "verified", "url": url, "category_warn": category_warn}
        # 검증 실패 — 호출자가 알림 (재시도 금지)
        return {"status": "published", "url": url, "category_warn": category_warn}
    finally:
        if own:
            conn.close()
