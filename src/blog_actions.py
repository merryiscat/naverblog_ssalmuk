"""수행 에이전트 — 검수 오케스트레이터의 결정을 블로그 설정에 실제 반영한다.

원칙 (2026-08-20 사용자 위임 "검수 의견 → 오케스트레이터 결정 → 수행"):
- **화이트리스트 액션만** 실행한다. 여기 없는 조작(삭제·이웃·댓글 등)은 코드가 없어서
  못 한다 — 이것이 가드레일이다. 삭제류는 어떤 형태로도 추가하지 않는다 (mistakes.md 08-19).
- 파괴적 자동화 규칙 준수: ①실행 전 대상 페이지(blogId)를 재검증 ②dialog는 예상
  메시지만 수락 ③저장 후 **페이지를 다시 열어 실제 값으로 검증** — 실패면 보고 후 중단.
- 모든 변경은 되돌릴 수 있는 설정값이다 (블로그명·별명·소개글·주제). 프로필 이미지
  업로드는 구식 팝업 업로더라 v1 자동화 제외 — 이미지 생성까지만 하고 수동 안내.

대상 페이지: admin.blog.naver.com/AdminUserBasic.naver (2026-08-20 정찰로 셀렉터 확보)
  #papername(블로그명 ≤한글25자) / #frmNickname(별명 ≤한글10자) /
  #frmIntroduce(소개글 ≤한글200자) / 라디오 name=paperSubjectSeq(내 블로그 주제)

저장 방식 (2026-08-21 판명): 화면의 확인 버튼(._btnConfirm)은 단독 페이지에서
parent 프레임의 layeredAlert가 없어 **무동작**한다. 실제 저장은 cgiform을 직렬화해
/UserBasicInfoUpdateAsync.naver?blogId=…에 POST — 응답 JSON isSuccess로 판정하고,
페이지를 다시 열어 실제 값까지 재확인한다.
"""

import json
import time

from playwright.sync_api import sync_playwright

from src import config

ADMIN_URL = "https://admin.blog.naver.com/AdminUserBasic.naver?blogId={blog_id}"

# 정찰(2026-08-20)로 확인한 '내 블로그 주제' 선택지 — 이 밖의 값은 실행 거부
VALID_SUBJECTS = [
    "문학·책", "영화", "미술·디자인", "공연·전시", "음악", "드라마", "스타·연예인",
    "만화·애니", "방송", "일상·생각", "육아·결혼", "반려동물", "좋은글·이미지",
    "패션·미용", "인테리어·DIY", "요리·레시피", "상품리뷰", "원예·재배", "게임",
    "스포츠", "사진", "자동차", "취미", "국내여행", "세계여행", "맛집",
    "IT·컴퓨터", "사회·정치", "건강·의학", "비즈니스·경제", "어학·외국어",
]

# 액션 카탈로그 — {액션명: (설명, 값 최대 길이 또는 None)}
# 오케스트레이터 프롬프트와 실행 검증이 공유하는 단일 출처
ACTION_CATALOG = {
    "set_blog_name": ("블로그명 변경 (한글 25자 이내)", 25),
    "set_nickname": ("별명 변경 (한글 10자 이내)", 10),
    "set_intro": ("프로필 소개글 변경 (한글 200자 이내)", 200),
    "set_subject": (f"내 블로그 주제 변경 — 다음 중 하나만: {', '.join(VALID_SUBJECTS)}", None),
    "make_profile_image": ("프로필 이미지 생성 (업로드는 수동 — 이미지 묘사를 값으로)", 300),
}

class ActionError(Exception):
    """수행 실패 — 원인 문자열 포함. 호출자(오케스트레이터)가 보고에 싣는다."""


def validate(actions: list[dict]) -> list[str]:
    """실행 전 정적 검증 — 화이트리스트·값 제약. 문제 목록을 돌려준다 (비면 통과)."""
    problems = []
    for a in actions:
        name, value = a.get("action"), str(a.get("value", ""))
        if name not in ACTION_CATALOG:
            problems.append(f"{name}: 화이트리스트에 없는 액션")
            continue
        maxlen = ACTION_CATALOG[name][1]
        if maxlen and len(value) > maxlen:
            problems.append(f"{name}: 값이 {maxlen}자 초과 ({len(value)}자)")
        if name == "set_subject" and value not in VALID_SUBJECTS:
            problems.append(f"set_subject: '{value}'는 선택지에 없음")
        if not value.strip():
            problems.append(f"{name}: 값이 비어 있음")
    return problems


def read_current() -> dict:
    """현재 블로그 설정을 읽는다 — 오케스트레이터의 판단 재료 + 실행 후 검증에 사용."""
    with sync_playwright() as pl:
        browser = pl.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                      user_agent=config.BROWSER_UA)
        page = context.new_page()
        try:
            page.goto(ADMIN_URL.format(blog_id=config.NAVER_BLOG_ID), timeout=45000)
            time.sleep(3)
            if "nidlogin" in page.url:
                raise ActionError("세션 만료 — 재로그인 필요")
            return page.evaluate(
                """() => ({
                    blog_name: document.querySelector('#papername')?.value || '',
                    nickname: document.querySelector('#frmNickname')?.value || '',
                    intro: document.querySelector('#frmIntroduce')?.value || '',
                    subject: document.querySelector('#subject_btn')?.textContent.trim() || '',
                })""")
        finally:
            browser.close()


def _make_profile_image(description: str) -> str:
    """프로필 이미지 생성 — data/images/profile.png 저장. 업로드는 수동 (팝업 업로더 v1 제외)."""
    from src import llm  # 지연 임포트 — 비용 게이트웨이 경유
    img = llm.generate_image(
        f"네이버 블로그 프로필 사진. {description}. "
        f"글자·텍스트 절대 없음, 정사각형 구도 중앙 배치, 미니멀하고 친근한 일러스트",
        purpose="profile-image", size="1024x1024")
    config.ensure_dirs()
    path = config.IMAGES_DIR / "profile.png"
    path.write_bytes(img)
    return str(path)


def apply(actions: list[dict]) -> list[dict]:
    """액션 목록을 실행한다. 반환: 액션별 {action, value, ok, detail}.

    폼 액션은 한 세션에서 값을 채우고 한 번 저장한 뒤, 페이지를 새로 열어
    실제 반영값을 재확인한다 (저장 성공을 믿지 않는다 — publisher와 같은 원칙).
    """
    problems = validate(actions)
    if problems:
        raise ActionError("정적 검증 실패: " + "; ".join(problems))

    results = []
    form_actions = [a for a in actions if a["action"] != "make_profile_image"]
    image_actions = [a for a in actions if a["action"] == "make_profile_image"]

    # ① 이미지 생성 (브라우저 불필요 — 실패해도 폼 액션은 계속)
    for a in image_actions:
        try:
            path = _make_profile_image(str(a["value"]))
            results.append({"action": a["action"], "value": a["value"], "ok": True,
                            "detail": f"생성됨: {path} — 블로그 정보 페이지에서 수동 업로드 필요"})
        except Exception as e:
            results.append({"action": a["action"], "value": a["value"], "ok": False,
                            "detail": f"{type(e).__name__}: {e}"})

    if not form_actions:
        return results

    # ② 폼 액션 — 한 세션에서 채우고 저장
    save_error = None
    with sync_playwright() as pl:
        browser = pl.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(config.SESSION_PATH),
                                      user_agent=config.BROWSER_UA)
        page = context.new_page()
        try:
            page.goto(ADMIN_URL.format(blog_id=config.NAVER_BLOG_ID), timeout=45000)
            time.sleep(3)
            # 파괴적 자동화 규칙 ①: 대상 재검증 — 내 blogId의 설정 페이지가 맞는지
            if config.NAVER_BLOG_ID not in page.url or "AdminUserBasic" not in page.url:
                raise ActionError(f"대상 페이지 불일치: {page.url}")

            failed_fills = {}
            for a in form_actions:
                name, value = a["action"], str(a["value"])
                try:
                    if name == "set_blog_name":
                        page.fill("#papername", value)
                    elif name == "set_nickname":
                        page.fill("#frmNickname", value)
                    elif name == "set_intro":
                        page.fill("#frmIntroduce", value)
                    elif name == "set_subject":
                        # 커스텀 셀렉트 — 옵션 라벨이 화면에 안 보여 UI 클릭 불가.
                        # 숨은 라디오 입력을 JS로 직접 클릭 (change 핸들러는 그대로 발화)
                        hit = page.evaluate(
                            """(subj) => {
                                const label = [...document.querySelectorAll('label._subject-label')]
                                    .find(l => l.textContent.trim() === subj);
                                if (!label) return 'no-label';
                                const input = document.getElementById(label.htmlFor);
                                if (!input) return 'no-input';
                                input.click();
                                return 'ok';
                            }""", value)
                        if hit != "ok":
                            raise ActionError(f"주제 라디오 접근 실패: {hit}")
                        time.sleep(0.5)
                except Exception as e:  # 한 액션 실패가 나머지 반영을 막지 않는다
                    failed_fills[name] = f"{type(e).__name__}: {e}"

            # 저장 — cgiform 직렬화 → 실제 저장 엔드포인트에 POST (모듈 docstring 참조)
            resp = page.evaluate(
                """async () => {
                    const form = document.cgiform;
                    if (!form) return {status: 0, text: 'no-cgiform'};
                    const params = new URLSearchParams(new FormData(form));
                    const res = await fetch('/UserBasicInfoUpdateAsync.naver?blogId='
                                            + form.blogId.value,
                                            {method: 'POST', body: params,
                                             credentials: 'include'});
                    return {status: res.status, text: (await res.text()).slice(0, 500)};
                }""")
            try:
                ok_json = json.loads(resp["text"])
                if ok_json.get("isSuccess") != "success":
                    save_error = ok_json.get("errorMsg") or resp["text"][:200]
            except (json.JSONDecodeError, TypeError):
                save_error = f"HTTP {resp['status']}: {resp['text'][:200]}"
            time.sleep(1)
        finally:
            browser.close()

    # ③ 검증 — 새 세션으로 다시 읽어 실제 반영값 확인
    current = read_current()
    field_of = {"set_blog_name": "blog_name", "set_nickname": "nickname",
                "set_intro": "intro", "set_subject": "subject"}
    for a in form_actions:
        if a["action"] in failed_fills:
            results.append({"action": a["action"], "value": a["value"], "ok": False,
                            "detail": f"입력 실패: {failed_fills[a['action']][:150]}"})
            continue
        actual = current.get(field_of[a["action"]], "")
        ok = actual.strip() == str(a["value"]).strip()
        results.append({"action": a["action"], "value": a["value"], "ok": ok,
                        "detail": "반영 확인" if ok else f"반영 안 됨 — 현재값: '{actual}'"
                        + (f" (저장 오류: {save_error})" if save_error and not ok else "")})
    return results
