"""C1 주제 발굴 단독 실행 — 오늘의 주제를 뽑아 결과를 표로 보여준다.

실행: uv run python scripts/run_topics.py
검색광고·검색 API + LLM 채점 1회를 실제 호출한다 (비용 몇 원 수준, 원장에 기록됨).
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, guardrails, topics


def main() -> None:
    conn = db.connect()
    result = topics.discover(conn)

    print(f"\n주제 발굴 결과 (상위 {len(result)}개)")
    print(f"{'키워드':<24} {'검색량':>8} {'문서수':>10} {'골든':>7} {'LLM':>5}  근거")
    print("-" * 100)
    for i, c in enumerate(result):
        mark = "★" if i < 2 else ("☆" if i == 2 else " ")
        print(f"{mark} {c['keyword']:<22} {c['volume']:>8,} {c['docs']:>10,} "
              f"{c['golden']:>7.2f} {c['llm_score']:>5.2f}  {c['reason'][:40]}")

    print(f"\n이번 달 비용 누계: ${guardrails.month_cost_usd(conn):.4f} "
          f"(남은 예산 ${guardrails.budget_remaining_usd(conn):.2f})")
    conn.close()


if __name__ == "__main__":
    main()
