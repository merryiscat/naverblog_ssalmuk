"""세션 품질 점검 — '로그인 상태 유지'가 실제로 적용됐는지 확인한다.

실행: uv run python scripts/check_session.py
판정: 인증 쿠키(NID_AUT)의 만료 시각이 장기(만료일 있음)면 유지 체크 성공,
      세션 쿠키(-1, 브라우저 닫으면 소멸)면 유지 체크가 안 된 것.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config


def main() -> None:
    if not config.SESSION_PATH.exists():
        print("세션 파일이 없습니다 — 위저드 3단계가 완료되지 않았습니다.")
        sys.exit(1)

    state = json.loads(config.SESSION_PATH.read_text(encoding="utf-8"))
    cookies = {c["name"]: c for c in state.get("cookies", [])}

    for name in ("NID_AUT", "NID_SES"):
        c = cookies.get(name)
        if not c:
            print(f"{name}: 없음 ⚠️")
            continue
        exp = c.get("expires", -1)
        if exp and exp > 0:
            print(f"{name}: 만료 {datetime.fromtimestamp(exp):%Y-%m-%d %H:%M} — 장기 쿠키 ✅")
        else:
            print(f"{name}: 세션 쿠키(만료 없음) — '로그인 상태 유지' 미적용 가능성 ⚠️")

    ok = "NID_AUT" in cookies
    print("\n판정:", "인증 쿠키 보유 — 발행 가능 상태" if ok else "인증 쿠키 없음 — 재로그인 필요")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
