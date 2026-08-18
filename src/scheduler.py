"""상주 스케줄러 — 전 모듈을 하루 리듬으로 연결하는 완전 방치 운영의 심장.

하루 일과 (모든 시각에 지터 — 사람 같은 불규칙성):
  04:00±40분  주제 발굴 (C1)
  08:30±60분  글 생성·게이트 (C2) → 통과 글마다 발행 시각을 11~21시 사이 랜덤 예약
  (예약 시각)  발행 (C3) — 일 상한은 가드레일이 강제
  21:30±30분  측정 (C4)
  22:30±20분  일일 보정 + 리포트 (C5)
  09:00       세션 수명 점검 — 만료 7일 전부터 텔레그램 경고
  화·금 15:00  경쟁·공백 관찰 (C6)
  일 16:00    메이트 관찰 에이전트 (C7)

모든 작업은 실패해도 스케줄러가 죽지 않는다 — 예외는 텔레그램으로 알리고 다음 주기를 기다린다.
실행: uv run python -m src.scheduler  (--dry: 스케줄만 출력하고 종료)
"""

import json
import random
import sys
import traceback
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from src import competitors, config, db, mate_observer, metrics, notify, publisher, steering, topics, writer
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


def job_generate(scheduler: BlockingScheduler):
    """C2 — selected 주제로 글 생성, 통과 글은 발행 시각을 랜덤 예약."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM topics WHERE status = 'selected' "
            "AND date = date('now', 'localtime')").fetchall()
        publish_times = _random_publish_times(len(rows))
        for topic, when in zip(rows, publish_times):
            result = writer.generate(dict(topic), conn)
            if result["status"] != "gated":
                print(f"생성 스킵: {topic['keyword']} — {result.get('reason')}")
                continue
            post_id = result["post_id"]
            scheduler.add_job(
                _safe(f"발행({result['title'][:20]})", lambda pid=post_id: _publish(pid)),
                DateTrigger(run_date=when), id=f"publish-{post_id}",
                misfire_grace_time=3600)
            print(f"발행 예약: [{post_id}] {result['title'][:30]} → {when:%H:%M}")
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
    notify.send(f"📝 발행 완료 ({mark})\n{result['url']}")


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
    scheduler.add_job(_safe("글생성", lambda: job_generate(scheduler)),
                      CronTrigger(hour=8, minute=30, jitter=3600), id="generate")
    scheduler.add_job(_safe("측정", job_metrics),
                      CronTrigger(hour=21, minute=30, jitter=1800), id="metrics")
    scheduler.add_job(_safe("보정", job_steering),
                      CronTrigger(hour=22, minute=30, jitter=1200), id="steering")
    scheduler.add_job(_safe("세션점검", job_session_check),
                      CronTrigger(hour=9, minute=0), id="session-check")
    # 주간 루프 (loop-design): 경쟁·공백 관찰 주 2회 — 발행 창과 겹치지 않는 오후 3시대
    scheduler.add_job(_safe("경쟁관찰", job_competitors),
                      CronTrigger(day_of_week="tue,fri", hour=15, minute=0, jitter=1800),
                      id="competitors")
    # C7 메이트 관찰 에이전트 주 1회 — 일요일 오후 (선정은 월 단위라 주 1회면 충분)
    scheduler.add_job(_safe("메이트관찰", job_mate_observer),
                      CronTrigger(day_of_week="sun", hour=16, minute=0, jitter=1800),
                      id="mate-observer")
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
