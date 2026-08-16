"""하드 가드레일 검사 — 시스템(자가 보정 LLM 포함)이 절대 못 넘는 선의 실행부.

값 자체는 config.py에 고정돼 있고, 여기는 그 값으로 실제 검사를 수행한다.
파이프라인의 해당 단계는 반드시 이 함수들을 거쳐야 한다:
  발행 직전  → check_daily_publish_limit()
  LLM 호출 전 → check_monthly_budget()  (llm.py 게이트웨이가 자동 호출)
"""

import sqlite3

from src import config


class GuardrailViolation(Exception):
    """가드레일에 걸렸을 때 발생 — 잡아서 스킵/정지 처리하고 리포트에 남긴다."""


def published_today(conn: sqlite3.Connection) -> int:
    """오늘 발행된(발행 시도 성공) 글 수."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM posts "
        "WHERE date(published_at) = date('now', 'localtime') "
        "AND status IN ('published', 'verified')"
    ).fetchone()
    return row["n"]


def check_daily_publish_limit(conn: sqlite3.Connection) -> None:
    """가드레일 ①: 일 발행 상한. 초과 상태면 예외를 던져 발행을 차단한다."""
    n = published_today(conn)
    if n >= config.DAILY_PUBLISH_LIMIT:
        raise GuardrailViolation(
            f"일 발행 상한 도달 ({n}/{config.DAILY_PUBLISH_LIMIT}건) — 오늘은 더 발행하지 않는다"
        )


def month_cost_usd(conn: sqlite3.Connection) -> float:
    """이번 달 LLM·이미지 비용 합계 (달러)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs "
        "WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now', 'localtime')"
    ).fetchone()
    return row["total"]


def check_monthly_budget(conn: sqlite3.Connection) -> None:
    """가드레일 ②: 월 비용 상한. 도달하면 예외 — 호출자는 파이프라인을 정지하고 알린다."""
    spent = month_cost_usd(conn)
    if spent >= config.MONTHLY_BUDGET_USD:
        raise GuardrailViolation(
            f"월 비용 상한 도달 (${spent:.2f} / ${config.MONTHLY_BUDGET_USD:.2f}"
            f" ≈ {config.MONTHLY_BUDGET_KRW:,}원) — 파이프라인 자동 정지"
        )


def budget_remaining_usd(conn: sqlite3.Connection) -> float:
    """남은 예산 (달러). 리포트와 '비용 근접 시 이미지 생략' 판단에 쓴다."""
    return max(0.0, config.MONTHLY_BUDGET_USD - month_cost_usd(conn))
