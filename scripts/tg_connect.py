"""텔레그램 봇 연결 유틸 — 토큰을 받아 chat_id 자동 감지 → .env 저장 → 테스트 발송.

실행: uv run python scripts/tg_connect.py <봇토큰>
위저드(setup.py)의 텔레그램 단계와 같은 일을 비대화식으로 한다 (토큰 재발급 때도 사용).
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib.util

# 위저드 모듈을 불러와 함수를 재사용한다 (스크립트라 일반 import가 안 됨)
_spec = importlib.util.spec_from_file_location("setup_wizard", Path(__file__).parent / "setup.py")
_wizard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wizard)


def main() -> None:
    if len(sys.argv) < 2:
        print("사용법: uv run python scripts/tg_connect.py <봇토큰>")
        sys.exit(1)
    token = sys.argv[1].strip()

    chat_id = _wizard.detect_chat_id(token)
    if not chat_id:
        print("chat_id 감지 실패 — 봇이 받은 메시지가 아직 없습니다.")
        print("텔레그램에서 t.me/blogssalmukbot 대화방을 열어 아무 메시지나 보낸 뒤 다시 실행하세요.")
        sys.exit(2)
    print(f"chat_id 감지: {chat_id}")

    if _wizard.verify_telegram(token, chat_id):
        _wizard.save_env("TELEGRAM_BOT_TOKEN", token)
        _wizard.save_env("TELEGRAM_CHAT_ID", chat_id)
        print("테스트 메시지 발송 성공 — .env에 저장 완료 (P5 확보)")
    else:
        print("테스트 발송 실패 — 토큰이 유효한지 확인하세요.")
        sys.exit(3)


if __name__ == "__main__":
    main()
