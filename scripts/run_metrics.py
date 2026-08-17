"""C4 측정 단독 실행 — 오늘의 계기판 스냅샷을 수집해 보여준다.

실행: uv run python scripts/run_metrics.py
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, metrics


def main() -> None:
    conn = db.connect()
    result = metrics.collect(conn)

    c = result["citations"]
    print("프로필 인용수")
    print(f"  위젯: {c.get('widget_raw')}")
    print(f"  누적: {c.get('cumulative')} / 당월: {c.get('this_month')} / 선정기준월: {c.get('basis_month')}")

    print("\n발행 글 관찰")
    if not result["ranks"]:
        print("  (verified 글 없음)")
    for r in result["ranks"]:
        cited = "🎯 인용됨!" if r["cited"] else ("브리핑 있음·미인용" if r["briefing"] else "브리핑 없음")
        approx = " (근사판정)" if r.get("approx") else ""
        rank = f"{r['rank']}위" if r["rank"] else "30위 밖"
        print(f"  [{r['keyword']}] 블로그검색 {rank} / {cited}{approx}")

    conn.close()


if __name__ == "__main__":
    main()
