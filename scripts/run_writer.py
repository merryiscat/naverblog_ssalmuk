"""C2 글 생성 단독 실행 — 오늘 selected 주제 하나로 초안을 만들어본다.

실행: uv run python scripts/run_writer.py [키워드]
키워드를 주면 그 주제로, 없으면 오늘 selected 중 첫 번째로 생성한다.
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, guardrails, writer


def main() -> None:
    conn = db.connect()

    if len(sys.argv) > 1:
        kw = sys.argv[1]
        row = conn.execute("SELECT * FROM topics WHERE keyword = ? ORDER BY id DESC", (kw,)).fetchone()
        topic = dict(row) if row else {"keyword": kw, "category": "일반"}
    else:
        row = conn.execute(
            "SELECT * FROM topics WHERE status = 'selected' AND date = date('now', 'localtime') "
            "ORDER BY llm_score DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("오늘 selected 주제가 없습니다. 먼저 run_topics.py를 실행하세요.")
            sys.exit(1)
        topic = dict(row)

    print(f"주제: {topic['keyword']} (분야: {topic.get('category')})")
    result = writer.generate(topic, conn)

    print(f"\n결과: {result['status']}")
    if result["status"] == "gated":
        print(f"제목: {result['title']}")
        print(f"본문: {result['body_path']} (시도 {result['attempts']}회)")
        print(f"게이트 총점: {result['gate']['total']} — {result['gate']['scores']}")
        print(f"개선 피드백: {result['gate']['feedback']}")
    else:
        print(f"사유: {result.get('reason')}")
        if result.get("gate"):
            print(f"마지막 채점: {result['gate']['scores']}")

    print(f"\n이번 달 비용 누계: ${guardrails.month_cost_usd(conn):.4f}")
    conn.close()


if __name__ == "__main__":
    main()
