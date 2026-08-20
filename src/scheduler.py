"""상주 스케줄러 — 전 모듈을 하루 리듬으로 연결하는 완전 방치 운영의 심장.

하루 일과 (모든 시각에 지터 — 사람 같은 불규칙성):
  04:00±40분  주제 발굴 (C1)
  08:30±60분  글 생성·게이트 (C2) → 통과 글마다 발행 시각을 11~21시 사이 랜덤 예약(DB)
  11~21시 20분 간격  발행 폴러 — 예약 시각 지난 글 발행 (C3) — 일 상한은 가드레일이 강제
  21:30±30분  측정 (C4)
  22:30±20분  일일 보정 + 리포트 (C5)
  09:00       세션 수명 점검 — 만료 7일 전부터 텔레그램 경고
  토 09:00    수동 작업 대기열 리마인더 (docs/manual-queue.md → 텔레그램)
  화·금 15:00  경쟁·공백 관찰 (C6)
  일 16:00    메이트 관찰 에이전트 (C7)
  화·목·토 23:15  블로그 검수 에이전트 (실화면 비전 검수 + 복기)
                  → 직후 검수 오케스트레이터가 결정, 수행 에이전트가 블로그 설정 반영

모든 작업은 실패해도 스케줄러가 죽지 않는다 — 예외는 텔레그램으로 알리고 다음 주기를 기다린다.
실행: uv run python -m src.scheduler  (--dry: 스케줄만 출력하고 종료)
"""

import json
import random
import re
import sys
import traceback
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from src import competitors, config, db, inspector, mate_observer, metrics, notify, orchestrator, publisher, steering, topics, writer
from src.guardrails import GuardrailViolation

PUBLISH_WINDOW = (11, 21)  # 발행 예약 창 (시)


def _safe(name: str, fn):
    """작업 래퍼 — 실패해도 상주 프로세스는 계속, 원인은 텔레그램으로."""
    def run():
        try:
            fn()
        except GuardrailViolation as e:
            notify.send(f"🛑 가드레일: {e}")
        except publisher.PublishError as e:
            notify.summon(f"{name} 실패\n{e}")
        except Exception as e:
            notify.send(f"⚠️ {name} 오류: {type(e).__name__}: {e}")
            traceback.print_exc()
    return run


def job_discover():
    """C1 — 오늘의 주제 발굴."""
    result = topics.discover()
    print(f"[{datetime.now():%H:%M}] 주제 발굴: {len(result)}건")


def job_generate():
    """C2 — selected 주제로 글 생성, 통과 글은 발행 시각을 DB에 예약.

    예약을 메모리(DateTrigger)가 아니라 posts.scheduled_at에 남긴다 —
    상주 프로세스가 재시작돼도 예약이 증발하지 않는다 (2026-08-19 버그 수정).
    실제 발행은 job_publish_due 폴러가 집어간다.

    게이트 탈락으로 구멍이 나면 오늘의 예비(reserve) 주제를 selected로 승격해
    한 번 더 시도한다 — 기본 3건("3+α"의 3)을 지향 (2026-08-20 사용자 결정.
    8/20 유튜브 프리미엄 게이트 탈락으로 일 1건만 발행된 것이 계기).
    """
    conn = db.connect()
    try:
        queue = [dict(r) for r in conn.execute(
            "SELECT * FROM topics WHERE status = 'selected' "
            "AND date = date('now', 'localtime')").fetchall()]
        publish_times = _random_publish_times(len(queue))
        gated = 0
        while queue:
            topic = queue.pop(0)
            result = writer.generate(topic, conn)
            if result["status"] != "gated":
                print(f"생성 스킵: {topic['keyword']} — {result.get('reason')}")
                # 예비 투입 — 오늘 reserve 하나를 승격해 대기열 뒤에 붙인다
                # (승격분도 탈락하면 예비가 더 없어 그대로 끝 — 재시도 무한루프 없음)
                res = conn.execute(
                    "SELECT * FROM topics WHERE status = 'reserve' "
                    "AND date = date('now', 'localtime') LIMIT 1").fetchone()
                if res:
                    conn.execute("UPDATE topics SET status = 'selected' WHERE id = ?",
                                 (res["id"],))
                    conn.commit()
                    queue.append(dict(res))
                    print(f"예비 투입: {res['keyword']} — {topic['keyword']} 탈락 대체")
                continue
            when = (publish_times[gated] if gated < len(publish_times)
                    else datetime.now() + timedelta(minutes=random.randint(30, 90)))
            conn.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?",
                         (when.strftime("%Y-%m-%d %H:%M:%S"), result["post_id"]))
            conn.commit()
            gated += 1
            print(f"발행 예약: [{result['post_id']}] {result['title'][:30]} → {when:%H:%M}")
    finally:
        conn.close()


def job_publish_due():
    """발행 폴러 — 예약 시각이 지난 gated 글을 발행한다 (발행 창에서 20분 간격).

    24시간 넘게 밀린 예약(장애로 놓친 것)은 신선도가 의심되므로 발행하지 않고
    스킵 처리 + 텔레그램 알림 — 낡은 글 발행 사고(2026-08-18)의 연장 방어선.
    """
    conn = db.connect()
    try:
        due = conn.execute(
            "SELECT id, title, scheduled_at FROM posts WHERE status = 'gated' "
            "AND scheduled_at IS NOT NULL "
            "AND scheduled_at <= datetime('now', 'localtime') ORDER BY scheduled_at").fetchall()
        for row in due:
            overdue_h = (datetime.now()
                         - datetime.fromisoformat(row["scheduled_at"])).total_seconds() / 3600
            if overdue_h > 24:
                conn.execute("UPDATE posts SET status = 'skipped' WHERE id = ?", (row["id"],))
                conn.commit()
                notify.send(f"⏭️ 예약 24시간 초과로 발행 취소 (신선도 보호): {row['title'][:40]}")
                continue
            _publish(row["id"])  # 가드레일(일 상한) 위반 시 예외 → _safe가 처리
    finally:
        conn.close()


def _random_publish_times(n: int) -> list[datetime]:
    """오늘 발행 창 안에서 n개의 시각을 3시간 이상 간격으로 뽑는다."""
    now = datetime.now()
    start_h, end_h = PUBLISH_WINDOW
    times = []
    for i in range(min(n, config.DAILY_PUBLISH_LIMIT)):
        # 창을 균등 분할한 구간 안에서 랜덤 — 간격 보장
        span = (end_h - start_h) / max(n, 1)
        h = start_h + span * i + random.uniform(0.2, max(span - 0.5, 0.3))
        t = now.replace(hour=int(h), minute=random.randint(0, 59), second=0)
        times.append(t if t > now else now + timedelta(minutes=random.randint(10, 40)))
    return times


def _publish(post_id: int):
    """C3 — 예약된 발행 실행."""
    result = publisher.publish(post_id)
    mark = "✅ 실게시 확인" if result["status"] == "verified" else "⚠️ 게시 확인 실패 — 수동 확인"
    warn = f"\n⚠️ {result['category_warn']}" if result.get("category_warn") else ""
    notify.send(f"📝 발행 완료 ({mark}){warn}\n{result['url']}")


def job_metrics():
    """C4 — 일일 측정."""
    metrics.collect()
    print(f"[{datetime.now():%H:%M}] 측정 완료")


def job_steering():
    """C5 — 일일 보정 + 리포트."""
    steering.run_daily()
    print(f"[{datetime.now():%H:%M}] 보정·리포트 완료")


def job_competitors():
    """C6 — 경쟁·공백 관찰 (주 2회). 공백 발견 시 텔레그램에도 요약."""
    result = competitors.observe()
    if result["opportunities"]:
        lines = [f"🎯 인용 공백 발견 — 스나이핑 후보 {len(result['opportunities'])}건"]
        for o in result["opportunities"]:
            lines.append(f"· {o['keyword']} (최신 출처 {o['freshest_days']}일 전)")
        notify.send("\n".join(lines))
    print(f"[{datetime.now():%H:%M}] 경쟁 관찰: {result['observed']}개 키워드, "
          f"공백 {len(result['opportunities'])}건")


def job_mate_observer():
    """C7 — 메이트 관찰·분석 에이전트 (주 1회). 정책 힌트가 나오면 텔레그램 요약."""
    result = mate_observer.observe()
    hints = (result["report"] or {}).get("policy_hints", [])
    if hints:
        notify.send("🕵️ 메이트 관찰 보고\n" + "\n".join(f"· {h}" for h in hints[:5]))
    print(f"[{datetime.now():%H:%M}] 메이트 관찰: 도구 {result['tool_calls']}회, "
          f"힌트 {len(hints)}건")


def job_inspector():
    """블로그 검수 에이전트 (주 3회) → 검수 오케스트레이터가 결정·수행까지.

    2026-08-20 사용자 위임: 검수 의견을 "사용자 결정 대기"로 쌓지 않는다 —
    오케스트레이터(gpt-5.4)가 결정하고 수행 에이전트(blog_actions)가 실행,
    사람은 결과 보고만 받는다. 오케스트레이션 실패가 검수 기록을 해치지 않게 분리.
    """
    result = inspector.inspect()
    report = result["report"] or {}
    urgent = [i for i in report.get("issues", []) if i.get("severity") in ("high", "mid")]
    persisting = report.get("persisting", [])
    fails = [c for c in report.get("checklist", []) if c.get("status") == "fail"]
    if urgent or persisting or fails:
        lines = ["🔍 블로그 검수 보고"]
        for c in fails[:5]:  # 정기 점검 실패 = 알려진 유형의 회귀 — 최우선 표시
            lines.append(f"📋 정기 실패 — {c.get('item')}: {c.get('note', '')[:60]}")
        for i in urgent[:5]:
            lines.append(f"[{i.get('severity')}] {i.get('where')}: {i.get('what')}")
        if persisting:
            lines.append("♻️ 반복 지적: " + "; ".join(persisting[:3]))
        notify.send("\n".join(lines))
    print(f"[{datetime.now():%H:%M}] 블로그 검수: 스크린샷 {result['shots']}장, "
          f"정기 실패 {len(fails)}건 / 발굴 {len(report.get('issues', []))}건 (반복 {len(persisting)}건)")

    # 검수 직후 오케스트레이션 — 정기 실패든 발굴이든 지적이 있을 때만
    if report.get("issues") or fails:
        try:
            cur = orchestrator.curate(report)
            lines = ["🎛️ 검수 오케스트레이터 결정"]
            for r in cur.get("results", []):
                mark = "✅" if r.get("ok") else "❌"
                lines.append(f"{mark} {r['action']}: {str(r.get('value', ''))[:40]} — {r['detail'][:60]}")
            for b in cur.get("blocked", []):
                lines.append(f"🚧 {b}")
            for m in cur.get("manual", [])[:3]:
                lines.append(f"🙋 수동 필요: {m.get('what', '')[:60]}")
            lines.append(f"근거: {cur.get('rationale', '')[:200]}")
            notify.send("\n".join(lines))
        except Exception as e:
            notify.send(f"⚠️ 검수 오케스트레이터 오류: {type(e).__name__}: {e}")
            traceback.print_exc()


def job_manual_queue():
    """수동 작업 대기열 주간 리마인더 (토 09:00, 사용자 요청 2026-08-21).

    docs/manual-queue.md의 '대기 중' 표를 파싱해 텔레그램으로 보낸다.
    비어 있어도 보낸다 — 침묵과 고장을 구분할 수 있게.
    """
    path = config.ROOT / "docs" / "manual-queue.md"
    if not path.exists():
        notify.send("🗒️ 수동 작업 대기열 파일 없음 (docs/manual-queue.md) — 배포 확인 필요")
        return
    text = path.read_text(encoding="utf-8")
    pending = text.split("## 대기 중")[1].split("## 완료")[0] if "## 대기 중" in text else ""
    items = []
    for row in pending.splitlines():
        if not row.startswith("|") or set(row.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if not cells or cells[0] in ("작업", ""):
            continue
        # 마크다운 장식 제거 — [텍스트](링크) → 텍스트, ** 제거
        name = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cells[0]).replace("**", "")
        items.append(name)
    if items:
        notify.send(f"🗒️ 주간 수동 작업 대기열 ({len(items)}건)\n"
                    + "\n".join(f"· {i}" for i in items)
                    + "\n\n방법·배경: docs/manual-queue.md")
    else:
        notify.send("🗒️ 주간 수동 작업 대기열 — 비어 있음 ✅")
    print(f"[{datetime.now():%H:%M}] 수동 대기열 리마인더: {len(items)}건")


def job_session_check():
    """세션 수명 점검 — 만료 7일 전부터 매일 경고."""
    if not config.SESSION_PATH.exists():
        notify.summon("세션 파일 없음 — 위저드 3단계(로그인)를 실행하세요")
        return
    state = json.loads(config.SESSION_PATH.read_text(encoding="utf-8"))
    auth = next((c for c in state.get("cookies", []) if c["name"] == "NID_AUT"), None)
    if not auth:
        notify.summon("세션에 인증 쿠키 없음 — 재로그인 필요")
        return
    exp = auth.get("expires", 0)
    if exp > 0:
        days = (datetime.fromtimestamp(exp) - datetime.now()).days
        if days <= 7:
            notify.summon(f"네이버 세션이 {days}일 후 만료 — 위저드 3단계로 재로그인 필요\n"
                          f"(data/session 삭제 후 uv run python scripts/setup.py)")


def build(scheduler: BlockingScheduler) -> BlockingScheduler:
    """하루 일과 등록. jitter 단위는 초."""
    scheduler.add_job(_safe("주제발굴", job_discover),
                      CronTrigger(hour=4, minute=0, jitter=2400), id="discover")
    scheduler.add_job(_safe("글생성", job_generate),
                      CronTrigger(hour=8, minute=30, jitter=3600), id="generate")
    # 발행 폴러 — 예약(DB) 시각이 지난 글을 집어 발행. 폴링이라 지터 불필요
    # (발행 시각의 사람 같은 불규칙성은 scheduled_at 자체가 랜덤이라 이미 확보)
    scheduler.add_job(_safe("발행폴러", job_publish_due),
                      CronTrigger(hour="11-21", minute="*/20"), id="publish-due")
    scheduler.add_job(_safe("측정", job_metrics),
                      CronTrigger(hour=21, minute=30, jitter=1800), id="metrics")
    scheduler.add_job(_safe("보정", job_steering),
                      CronTrigger(hour=22, minute=30, jitter=1200), id="steering")
    scheduler.add_job(_safe("세션점검", job_session_check),
                      CronTrigger(hour=9, minute=0), id="session-check")
    # 수동 작업 대기열 리마인더 주 1회 — 토요일 아침, 주말에 처리하기 좋은 시점
    scheduler.add_job(_safe("수동대기열", job_manual_queue),
                      CronTrigger(day_of_week="sat", hour=9, minute=0), id="manual-queue")
    # 주간 루프 (loop-design): 경쟁·공백 관찰 주 2회 — 발행 창과 겹치지 않는 오후 3시대
    scheduler.add_job(_safe("경쟁관찰", job_competitors),
                      CronTrigger(day_of_week="tue,fri", hour=15, minute=0, jitter=1800),
                      id="competitors")
    # C7 메이트 관찰 에이전트 주 1회 — 일요일 오후 (선정은 월 단위라 주 1회면 충분)
    scheduler.add_job(_safe("메이트관찰", job_mate_observer),
                      CronTrigger(day_of_week="sun", hour=16, minute=0, jitter=1800),
                      id="mate-observer")
    # 블로그 검수 에이전트 주 3회 (사용자 확정 화·목·토) — 당일 발행분이 다 올라온 심야,
    # C4(21:30)·C5(22:30)와 무충돌. 결과는 다음날 보정 입력으로
    scheduler.add_job(_safe("블로그검수", job_inspector),
                      CronTrigger(day_of_week="tue,thu,sat", hour=23, minute=15, jitter=900),
                      id="inspector")
    return scheduler


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지
    scheduler = build(BlockingScheduler(timezone="Asia/Seoul"))
    if "--dry" in sys.argv:
        print("등록된 하루 일과:")
        for job in scheduler.get_jobs():
            print(f"  {job.id:<14} {job.trigger}")
        return
    notify.send("🟢 파이프라인 상주 시작 — 하루 일과가 자동으로 돕니다")
    print("스케줄러 상주 시작 (Ctrl+C로 중단)")
    scheduler.start()


if __name__ == "__main__":
    main()
