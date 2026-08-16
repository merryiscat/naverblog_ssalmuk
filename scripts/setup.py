"""C0 셋업 위저드 — 선결 과제 P1~P9를 단계별로 안내·대행한다.

실행: uv run python scripts/setup.py
언제든 다시 실행해도 된다 — 이미 완료된 단계는 자동으로 건너뛴다.

단계 (usecases.md 선결 과제와 1:1 대응):
  1. Playwright 브라우저(chromium) 설치
  2. .env 생성 + API 키 입력·검증 (P3 오픈API, P4 검색광고, P6 OpenAI, P5 텔레그램)
  3. 네이버 로그인 → 세션 저장 (P1)
  4. 블로그 AI 활용 설정 ON (P2)
  5. 블로그 프로필·카테고리 정리 (P7)
  6. 프로필 인용수 UI 확인 (P8)
  ※ P9(선정자 목록 공개 여부)는 웹 조사가 필요해 Claude 세션에서 함께 확인한다.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import set_key

from src import config

ENV_PATH = config.ROOT / ".env"
STATE_PATH = config.DATA_DIR / "setup_state.json"  # 수동 확인 단계의 완료 기록


# ---------------------------------------------------------------------------
# 공용 도우미
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """수동 확인 단계(P2·P7·P8)의 완료 기록을 읽는다."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    config.ensure_dirs()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def ask(prompt: str) -> str:
    """사용자 입력을 받는다. 빈 입력(그냥 Enter)은 '건너뛰기'로 쓴다."""
    return input(f"{prompt}: ").strip()


def save_env(key: str, value: str) -> None:
    """API 키를 .env에 저장하고, 실행 중인 프로세스에도 반영한다."""
    set_key(ENV_PATH, key, value)
    import os
    os.environ[key] = value


# ---------------------------------------------------------------------------
# 1. Playwright 브라우저
# ---------------------------------------------------------------------------

def step_playwright() -> bool:
    header("1단계 — Playwright 브라우저 (chromium)")
    # 설치 여부는 실제로 띄워봐야 확실하지만, 설치 명령은 이미 설치돼 있으면
    # 금방 끝나므로 그냥 매번 돌린다 (멱등).
    print("chromium을 설치/확인합니다... (이미 설치돼 있으면 금방 끝남)")
    r = subprocess.run(
        ["uv", "run", "playwright", "install", "chromium"],
        cwd=config.ROOT, capture_output=True, text=True,
    )
    ok = r.returncode == 0
    print("완료" if ok else f"실패:\n{r.stderr}")
    return ok


# ---------------------------------------------------------------------------
# 2. .env + API 키 입력·검증
# ---------------------------------------------------------------------------

def verify_openai(key: str) -> bool:
    """OpenAI 키가 실제로 동작하는지 모델 목록 조회로 확인."""
    r = httpx.get("https://api.openai.com/v1/models",
                  headers={"Authorization": f"Bearer {key}"}, timeout=15)
    return r.status_code == 200


def verify_naver_openapi(cid: str, secret: str) -> bool:
    """네이버 오픈API 키 확인 — 블로그 검색 1건 호출."""
    r = httpx.get(
        "https://openapi.naver.com/v1/search/blog.json",
        params={"query": "테스트", "display": 1},
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
        timeout=15,
    )
    return r.status_code == 200


def verify_telegram(token: str, chat_id: str) -> bool:
    """텔레그램 봇 확인 — 실제로 테스트 메시지를 보내본다."""
    r = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "✅ naverblog_ssalmuk 셋업 테스트 — 이 메시지가 보이면 연결 성공"},
        timeout=15,
    )
    return r.status_code == 200


def detect_chat_id(token: str) -> str | None:
    """봇에게 온 최근 메시지에서 chat_id를 자동으로 찾는다.

    사용자가 봇에게 아무 메시지나 먼저 보내둬야 잡힌다 (getUpdates 활용).
    """
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
        if r.status_code != 200:
            return None
        for update in reversed(r.json().get("result", [])):
            chat = update.get("message", {}).get("chat", {})
            if chat.get("id"):
                return str(chat["id"])
    except httpx.HTTPError:
        pass
    return None


def step_env_keys() -> dict:
    """비어 있는 키만 골라 발급 안내 → 입력 → 즉석 검증. 반환: 키별 상태."""
    header("2단계 — API 키 (.env)")
    if not ENV_PATH.exists():
        ENV_PATH.write_text((config.ROOT / ".env.example").read_text(encoding="utf-8"),
                            encoding="utf-8")
        print(".env 파일을 새로 만들었습니다.")

    status: dict[str, str] = {}

    # --- OpenAI (P6) ---
    if config.OPENAI_API_KEY:
        status["openai"] = "설정됨"
    else:
        print("\n[OpenAI] https://platform.openai.com/api-keys 에서 키 발급")
        print("  (Project Odin에서 쓰던 키를 재사용해도 됩니다)")
        v = ask("OPENAI_API_KEY (건너뛰려면 Enter)")
        if v:
            ok = verify_openai(v)
            if ok:
                save_env("OPENAI_API_KEY", v)
            status["openai"] = "검증 통과" if ok else "검증 실패 — 키 확인 필요"
            print(f"  → {status['openai']}")
        else:
            status["openai"] = "건너뜀"

    # --- 네이버 오픈API (P3) — 2026-08 신규 발급 중단 확인됨 ---
    # 신규 앱 등록의 '사용 API' 목록에서 검색·데이터랩이 제거됐고 (실화면 확인),
    # 검색 API 신규 제휴도 중단 명시. 기존에 발급받은 키가 있는 경우에만 입력받는다.
    # 없어도 파이프라인은 검색광고 API + 스크래핑으로 동작한다 (usecases.md P3).
    if config.NAVER_CLIENT_ID and config.NAVER_CLIENT_SECRET:
        status["naver_openapi"] = "설정됨 (기존 발급 키)"
    else:
        print("\n[네이버 오픈API] 2026-08 기준 검색·데이터랩 신규 발급이 중단됐습니다.")
        print("  과거에 발급받아 둔 키가 있을 때만 입력하세요. 없으면 그냥 Enter —")
        print("  검색광고 API와 스크래핑으로 대체 동작합니다.")
        cid = ask("NAVER_CLIENT_ID (기존 키 보유자만, 없으면 Enter)")
        if cid:
            secret = ask("NAVER_CLIENT_SECRET")
            ok = verify_naver_openapi(cid, secret)
            if ok:
                save_env("NAVER_CLIENT_ID", cid)
                save_env("NAVER_CLIENT_SECRET", secret)
            status["naver_openapi"] = "검증 통과" if ok else "검증 실패 — 키 확인 필요"
            print(f"  → {status['naver_openapi']}")
        else:
            status["naver_openapi"] = "없음 — 검색광고 API+스크래핑으로 대체 (정상)"

    # --- 검색광고 API (P4) — 서명 로직이 필요해 즉석 검증은 C1에서 ---
    if config.SEARCHAD_API_KEY:
        status["searchad"] = "설정됨 (검증은 C1 구현 시)"
    else:
        print("\n[검색광고 API] https://searchad.naver.com 광고주 가입")
        print("  → 도구 > API 사용 관리에서 키 발급. 화면 용어와 대응:")
        print("     액세스라이선스 = SEARCHAD_API_KEY / 비밀키 = SEARCHAD_SECRET_KEY / CUSTOMER_ID = 그대로")
        v = ask("SEARCHAD_API_KEY = 액세스라이선스 (건너뛰려면 Enter)")
        if v:
            save_env("SEARCHAD_API_KEY", v)
            save_env("SEARCHAD_SECRET_KEY", ask("SEARCHAD_SECRET_KEY"))
            save_env("SEARCHAD_CUSTOMER_ID", ask("SEARCHAD_CUSTOMER_ID"))
            status["searchad"] = "저장됨 (검증은 C1 구현 시)"
        else:
            status["searchad"] = "건너뜀 — C1은 상대 지표 모드로 가동됨"

    # --- 텔레그램 (P5) ---
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        status["telegram"] = "설정됨"
    else:
        print("\n[텔레그램] @BotFather에게 /newbot 으로 봇 생성 → 완료 메시지의 토큰 복사")
        print("  (봇 유저네임(@...봇이름)이 아니라 '123456789:AAE...' 형태의 토큰입니다)")
        token = ask("TELEGRAM_BOT_TOKEN (건너뛰려면 Enter)")
        if token:
            # chat_id 자동 감지 — 봇에게 먼저 말을 걸어둬야 잡힌다
            chat_id = detect_chat_id(token)
            while not chat_id:
                print("  아직 봇이 받은 메시지가 없습니다.")
                print("  텔레그램에서 내 봇을 검색해 대화방을 열고 아무 메시지나 보낸 뒤 Enter...")
                if input().strip().lower() == "q":  # q 입력 시 수동 입력으로 전환
                    chat_id = ask("TELEGRAM_CHAT_ID 직접 입력")
                    break
                chat_id = detect_chat_id(token)
            print(f"  chat_id 감지: {chat_id}")
            ok = verify_telegram(token, chat_id)
            if ok:
                save_env("TELEGRAM_BOT_TOKEN", token)
                save_env("TELEGRAM_CHAT_ID", chat_id)
            status["telegram"] = "테스트 메시지 발송 성공" if ok else "발송 실패 — 토큰/chat_id 확인"
            print(f"  → {status['telegram']}")
        else:
            status["telegram"] = "건너뜀"

    return status


# ---------------------------------------------------------------------------
# 3. 네이버 로그인 → 세션 저장 (P1)
# ---------------------------------------------------------------------------

def naver_logged_in(cookies: list[dict]) -> bool:
    """네이버 로그인 성공 판정 — 인증 쿠키(NID_AUT)가 있으면 로그인 상태다."""
    return any(c["name"] == "NID_AUT" for c in cookies)


def session_valid() -> bool:
    """저장된 세션 파일이 있고, 그 안에 인증 쿠키가 남아 있는지 (간이 검사)."""
    if not config.SESSION_PATH.exists():
        return False
    state = json.loads(config.SESSION_PATH.read_text(encoding="utf-8"))
    return naver_logged_in(state.get("cookies", []))


def step_naver_login() -> bool:
    header("3단계 — 네이버 로그인 세션 저장 (P1)")
    if session_valid():
        print("저장된 세션이 이미 있습니다 — 건너뜁니다.")
        print("(세션을 새로 만들려면 data/session/ 폴더를 지우고 다시 실행)")
        return True

    print("브라우저가 열립니다. 직접 로그인해 주세요 (2단계 인증·캡차 포함).")
    print("로그인이 감지되면 자동으로 세션을 저장합니다. (최대 5분 대기)")
    input("준비되면 Enter...")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")

        # 2초마다 인증 쿠키가 생겼는지 확인 — 생기면 로그인 완료
        ok = False
        for _ in range(150):  # 150회 × 2초 = 5분
            time.sleep(2)
            if naver_logged_in(context.cookies()):
                ok = True
                break

        if ok:
            config.ensure_dirs()
            context.storage_state(path=str(config.SESSION_PATH))
            print(f"세션 저장 완료 → {config.SESSION_PATH}")
        else:
            print("5분 안에 로그인이 감지되지 않았습니다. 다시 실행해 주세요.")
        browser.close()
    return ok


# ---------------------------------------------------------------------------
# 4~6. 수동 확인 단계 (P2 · P7 · P8) — 페이지를 열어주고 확인을 기록
# ---------------------------------------------------------------------------

def open_with_session(url: str, wait_message: str) -> None:
    """저장된 로그인 세션으로 페이지를 열어주고, 사용자가 볼 동안 대기한다."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(config.SESSION_PATH))
        page = context.new_page()
        page.goto(url)
        input(f"{wait_message} — 끝나면 Enter...")
        browser.close()


def step_manual(state: dict, key: str, title: str, url: str, guide: str,
                record_answer: bool = False) -> None:
    """수동 확인 단계 공통 처리 — 완료 기록이 있으면 건너뛴다."""
    header(title)
    if state.get(key):
        print(f"이미 완료로 기록됨 ({state[key]}) — 건너뜁니다.")
        return
    if not session_valid():
        print("네이버 세션이 없어 이 단계를 진행할 수 없습니다. 3단계를 먼저 완료하세요.")
        return
    print(guide)
    open_with_session(url, "브라우저에서 확인/설정해 주세요")
    if record_answer:
        answer = ask("확인 결과를 적어주세요 (예: '인용수 표시 위치 = 프로필 상단')")
        state[key] = answer or "확인함 (상세 미기록)"
    else:
        done = ask("완료했으면 y, 아니면 Enter")
        if done.lower() == "y":
            state[key] = "완료"
    save_state(state)


# ---------------------------------------------------------------------------
# 메인 — 체크리스트 출력 후 단계별 진행
# ---------------------------------------------------------------------------

def main() -> None:
    print("naverblog_ssalmuk 셋업 위저드")
    print("이미 완료된 단계는 자동으로 건너뜁니다. 언제든 중단하고 다시 실행해도 됩니다.")

    state = load_state()

    step_playwright()
    key_status = step_env_keys()
    login_ok = step_naver_login()

    if login_ok:
        # 4단계 (P2) — 할 일 없음이 확인되어 자동 통과 (2026-08-17 실화면 조사)
        # · 블로그 단위 '검색 허용' 스위치는 관리 메뉴에 존재하지 않음
        #   (콘텐츠 공유 설정 = CCL·출처·우클릭, 블로그 정보 = 이름·프로필뿐)
        # · 검색 노출은 글 단위 발행 옵션(전체공개+검색허용, 기본 ON) → C3 발행
        #   모듈이 발행 때마다 보장한다
        # · 'AI 활용 동의'는 약관 차원 — 로그인 중 팝업이 뜨면 동의
        header("4단계 — 공개·검색 허용 (P2): 자동 통과")
        print("블로그 단위 설정이 없어 사용자가 할 일이 없습니다.")
        print("검색 노출은 글 단위 발행 옵션이며, 발행 모듈이 매번 '전체공개+검색허용'을 보장합니다.")
        state["p2_ai_consent"] = "해당 없음 — 글 단위 발행 옵션으로 C3에서 보장 (2026-08-17 확인)"
        save_state(state)
        step_manual(
            state, "p7_profile",
            "5단계 — 블로그 프로필·카테고리 정리 (P7)",
            "https://admin.blog.naver.com",
            "방치 상태였다면: 블로그명·소개글·프로필 이미지를 정리하고,\n"
            "글을 담을 카테고리를 2~3개 만들어 주세요 (주제는 나중에 시스템이 정합니다).",
        )
        step_manual(
            state, "p8_citation_ui",
            "6단계 — 프로필 인용수 UI 확인 (P8) ※ 측정 루프의 1차 지표",
            "https://blog.naver.com",
            "내 블로그 프로필에서 'AI 브리핑 인용수'가 표시되는 위치를 찾아주세요.\n"
            "(2026-06부터 제공된다고 알려져 있으나 실계정 확인이 필요합니다)",
            record_answer=True,
        )

    # --- 최종 체크리스트 ---
    header("셋업 현황 요약")
    rows = [
        ("P1 네이버 세션", "저장됨" if session_valid() else "미완료"),
        ("P2 AI 활용 설정", state.get("p2_ai_consent", "미완료")),
        ("P3 오픈API 키", key_status.get("naver_openapi", "설정됨")),
        ("P4 검색광고 API", key_status.get("searchad", "설정됨")),
        ("P5 텔레그램", key_status.get("telegram", "설정됨")),
        ("P6 OpenAI 키", key_status.get("openai", "설정됨")),
        ("P7 프로필 정리", state.get("p7_profile", "미완료")),
        ("P8 인용수 UI", state.get("p8_citation_ui", "미확인")),
        ("P9 선정자 목록", "Claude 세션에서 웹 조사로 확인 (위저드 범위 밖)"),
    ]
    for name, val in rows:
        print(f"  {name:<14} : {val}")
    print("\n미완료 항목은 위저드를 다시 실행하면 그 단계만 진행됩니다.")


if __name__ == "__main__":
    main()
