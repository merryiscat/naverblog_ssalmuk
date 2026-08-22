"""작업 대장·이행 추적 유틸 — 오케스트레이터 v2(관리자)의 심판·제동 부품.

v1(2026-08-22 오전)에서는 이 모듈이 반복 지적 진단·해결 시도까지 직접 했으나,
같은 날 오케스트레이터 v2 개편(관리자·실행 분리)으로 진단·지시는 오케스트레이터가
흡수했다. 여기 남은 것은 공용 부품:
  update_outcomes  — 지난 지시(tried)를 이번 검수의 resolved/persisting과 대조해
                     verified/failed 판정. 심판은 검수 화면 — 별도 확인 로직 없음
  pending_manuals  — 수동 전환(gave_up) 대기 목록 (주간 리마인더가 합산)
  _hint_in_cooldown — 정책 힌트 7일 냉각기 판정 (오케스트레이터의 제동에 사용)

수동 전환(gave_up)은 DB(resolution_attempts)가 원본이다 — 서버가 docs 파일에 쓰면
배포 시 덮여 유실되므로 파일에는 쓰지 않고, 주간 리마인더(job_manual_queue)가
DB를 합산해 보낸다. 사람이 처리하면 다음 검수의 resolved 판정이 자동으로 지운다.
"""

import json
import sqlite3
from datetime import datetime, timedelta

MAX_FAILED_BEFORE_GIVEUP = 2   # 서로 다른 접근으로 이 횟수 실패하면 수동 전환
HINT_COOLDOWN_DAYS = 7         # 같은 정책 힌트 액션의 재변경 냉각기


def _norm(s) -> str:
    return "".join(str(s).split())


def _matches(a: str, b: str) -> bool:
    """지적 문구 대조 — 공백 제거 후 부분 일치 (검수 LLM이 문구를 조금씩 바꿔 쓰므로)."""
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


def update_outcomes(report: dict, conn: sqlite3.Connection) -> list[str]:
    """지난 시도의 효과 판정 — 이번 검수 보고가 심판이다. 반환: 텔레그램 보고용 한 줄들.

    tried  → resolved에 있으면 verified, persisting에 있으면 failed (없으면 판정 유보)
    gave_up → resolved에 있으면 verified — 사람이 처리한 항목이 리마인더에서 자동 소거
    """
    resolved = [str(r) for r in report.get("resolved", [])]
    persisting = [str(p) for p in report.get("persisting", [])]
    log = []
    rows = conn.execute(
        "SELECT id, issue_key, issue_text, result FROM resolution_attempts "
        "WHERE result IN ('tried', 'gave_up')").fetchall()
    for row in rows:
        verdict = None
        if any(_matches(row["issue_text"], r) for r in resolved):
            verdict = "verified"
        elif row["result"] == "tried" and any(_matches(row["issue_text"], p) for p in persisting):
            verdict = "failed"
        if verdict:
            conn.execute("UPDATE resolution_attempts SET result = ? WHERE id = ?",
                         (verdict, row["id"]))
            log.append(f"✅ {row['issue_key']} 해결 확인" if verdict == "verified"
                       else f"❌ {row['issue_key']} 실패 — 다른 접근 검토")
    conn.commit()
    return log


def _hint_in_cooldown(history: list[dict], action_name: str) -> bool:
    """같은 정책 힌트 액션이 최근 7일 내 실행됐으면 냉각기 — 잦은 흔들기 방지."""
    if not action_name:
        return False
    cutoff = datetime.now() - timedelta(days=HINT_COOLDOWN_DAYS)
    for h in history:
        if h["approach"] not in ("policy", "prevent"):
            continue
        try:
            if (action_name in (h["attempt_json"] or "")
                    and datetime.fromisoformat(h["created_at"]) >= cutoff):
                return True
        except (ValueError, TypeError):
            continue
    return False


def pending_manuals(conn: sqlite3.Connection) -> list[dict]:
    """수동 전환(gave_up) 대기 목록 — 주간 리마인더(job_manual_queue)가 합산 발송.

    사람이 처리하면 다음 검수의 resolved 판정(update_outcomes)이 verified로 바꿔
    자동으로 이 목록에서 빠진다.
    """
    rows = conn.execute(
        "SELECT issue_key, issue_text, attempt_json, MAX(date) AS date "
        "FROM resolution_attempts WHERE result = 'gave_up' "
        "GROUP BY issue_key ORDER BY date DESC").fetchall()
    out = []
    for r in rows:
        try:
            brief = json.loads(r["attempt_json"] or "{}")
        except json.JSONDecodeError:
            brief = {}
        out.append({"issue_key": r["issue_key"], "issue_text": r["issue_text"],
                    "instruction": brief.get("instruction", ""), "date": r["date"]})
    return out
