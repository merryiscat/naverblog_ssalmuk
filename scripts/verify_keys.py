"""전체 API 키 일괄 검증 — 실호출로 P1·P3·P4·P5·P6 확보 상태를 확인한다.

실행: uv run python scripts/verify_keys.py
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src import config
from src.naver_api import doc_count, keyword_stats


def check(name: str, fn) -> bool:
    try:
        detail = fn()
        print(f"  [OK] {name}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        print(f"  [실패] {name} — {type(e).__name__}: {e}")
        return False


def main() -> None:
    print("API 키 일괄 검증\n" + "=" * 50)
    results = []

    # P6 OpenAI — 모델 목록 조회
    results.append(check("OpenAI", lambda: (
        httpx.get("https://api.openai.com/v1/models",
                  headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                  timeout=15).raise_for_status(), "인증 통과")[1]))

    # P3 오픈API 검색 — 문서수 조회 (재활용 키의 핵심 용도)
    results.append(check("오픈API 검색", lambda: f"'네이버 블로그' 문서수 = {doc_count('네이버 블로그'):,}건"))

    # P4 검색광고 키워드 도구 — 월간 검색량
    def searchad():
        rows = keyword_stats(["캠핑"])
        if not rows:
            raise RuntimeError("응답은 왔으나 keywordList가 비어 있음")
        top = rows[0]
        return f"'{top['relKeyword']}' 월간 검색량 PC {top['monthlyPcQcCnt']} / 모바일 {top['monthlyMobileQcCnt']} (연관 {len(rows)}개)"
    results.append(check("검색광고 키워드도구", searchad))

    # P5 텔레그램 — getMe (메시지 발송 없이 토큰만 확인)
    results.append(check("텔레그램", lambda: (
        httpx.get(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe",
                  timeout=15).raise_for_status(), "토큰 유효")[1]))

    # P1 네이버 세션 — 파일 존재 + 인증 쿠키 보유 (실접속 검증은 발행 모듈에서)
    def session():
        state = json.loads(config.SESSION_PATH.read_text(encoding="utf-8"))
        if not any(c["name"] == "NID_AUT" for c in state.get("cookies", [])):
            raise RuntimeError("세션 파일에 인증 쿠키(NID_AUT) 없음")
        return "세션 파일 유효 (인증 쿠키 보유)"
    results.append(check("네이버 로그인 세션", session))

    print("=" * 50)
    print(f"통과 {sum(results)}/{len(results)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
