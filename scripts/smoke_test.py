"""뼈대 스모크 테스트 — DB 스키마 생성과 가드레일 동작을 확인한다.

실행: uv run python scripts/smoke_test.py
API 키 없이도 돌아간다 (LLM 호출은 하지 않음).
"""

import sys
from pathlib import Path

# Windows 콘솔 기본 인코딩(cp949)은 일부 특수문자를 못 찍으므로 UTF-8로 강제
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, db, guardrails


def main() -> None:
    conn = db.connect()

    # 이전 실행이 중간에 죽었을 경우를 대비해 테스트 데이터부터 정리
    conn.execute("DELETE FROM costs WHERE purpose = 'smoke-test'")
    conn.commit()

    # 테이블이 전부 생겼는지
    tables = sorted(
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not r[0].startswith("sqlite_")
    )
    expected = ["competitors", "costs", "decisions", "metrics", "policy", "posts", "rankings", "topics"]
    assert tables == expected, f"테이블 불일치: {tables}"
    print("테이블 8개 생성 확인:", ", ".join(tables))

    # 가드레일 — 빈 DB에서는 둘 다 통과해야 한다
    guardrails.check_daily_publish_limit(conn)
    guardrails.check_monthly_budget(conn)
    print(f"오늘 발행: {guardrails.published_today(conn)}/{config.DAILY_PUBLISH_LIMIT}건")
    print(f"남은 월 예산: ${guardrails.budget_remaining_usd(conn):.2f} (상한 {config.MONTHLY_BUDGET_KRW:,}원)")

    # 가드레일이 실제로 막는지 — 가짜 데이터로 상한 초과를 재현
    conn.execute(
        "INSERT INTO costs (kind, model, cost_usd, purpose) VALUES ('text', 'test', ?, 'smoke-test')",
        (config.MONTHLY_BUDGET_USD + 1,),
    )
    try:
        guardrails.check_monthly_budget(conn)
        raise AssertionError("예산 초과인데 가드레일이 안 걸림!")
    except guardrails.GuardrailViolation as e:
        print(f"예산 가드레일 차단 확인: {e}")

    conn.execute("DELETE FROM costs WHERE purpose = 'smoke-test'")  # 가짜 데이터 정리
    conn.commit()
    conn.close()
    print("스모크 테스트 전부 통과")


if __name__ == "__main__":
    main()
