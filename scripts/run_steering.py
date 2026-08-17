"""C5 일일 보정 단독 실행 — 오늘 데이터로 보정을 돌리고 리포트를 보낸다.

실행: uv run python scripts/run_steering.py
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, steering


def main() -> None:
    conn = db.connect()
    result = steering.run_daily(conn)
    print(result["report"])
    print(f"\n텔레그램 발송: {'성공' if result['telegram_sent'] else '실패'}")
    conn.close()


if __name__ == "__main__":
    main()
