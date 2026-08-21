"""해결 에이전트(resolver) — 반복 지적(persisting)을 지적으로 끝내지 않고 스스로 고쳐본다.

배경 (2026-08-22 사용자 결정): 검수 에이전트가 같은 문제를 계속 지적만 반복하는
구조를 없앤다 — 원인을 진단하고, 할 수 있는 해결책을 시도하고, 다음 검수가
효과를 판정한다. 사고 리스크가 있는 파괴적 조작(발행 글 수정·이동)은 하지 않는다 —
비파괴 수단(액션 카탈로그 + 글 생성 정책 힌트)만 쓰고, 나머지는 수동 지시서로 넘긴다.

흐름 (검수 사이클 화·목·토 23:15 안에서, 오케스트레이터 직후):
  ① update_outcomes — 지난 시도(tried)를 이번 검수의 resolved/persisting과 대조해
     verified(해결 확인)/failed(여전히 반복)로 판정. 심판은 검수 화면이다 —
     별도 확인 로직을 만들지 않고 검수의 복기 메커니즘을 재활용한다
  ② try_resolve — 이번 persisting(최대 3건)에 대해 LLM(gpt-5.4)이
     원인 진단 + 해결책 분류(catalog 액션 / policy 힌트 / manual 지시서) → 즉시 실행

코드 강제 제동 (LLM 판단 위의 안전장치 — 진동·무한 재시도 방지):
  - 같은 issue_key는 검수 1회당 1시도
  - 같은 issue_key에 failed 2회면 강제 gave_up (수동 전환, 재시도 중단)
  - 정책 힌트는 같은 액션 7일 냉각기 (steering 냉각기와 같은 철학)
  - 실행은 전부 blog_actions.validate→apply 경로 — 화이트리스트·파괴적 자동화 규칙 승계

수동 전환(gave_up)은 DB(resolution_attempts)가 원본이다 — 서버가 docs 파일에 쓰면
배포 시 덮여 유실되므로 파일에는 쓰지 않고, 주간 리마인더(job_manual_queue)가
DB를 합산해 보낸다. 사람이 처리하면 다음 검수의 resolved 판정이 자동으로 지운다.
"""

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from src import blog_actions, config, llm

MAX_ISSUES_PER_CYCLE = 3       # 한 검수당 진단·시도하는 반복 지적 수 (비용·진동 제한)
MAX_FAILED_BEFORE_GIVEUP = 2   # 서로 다른 접근으로 이 횟수 실패하면 수동 전환
HINT_COOLDOWN_DAYS = 7         # 같은 정책 힌트 액션의 재변경 냉각기

RESOLVE_PROMPT = """너는 네이버 블로그 자동 운영 시스템의 해결 담당이다. 블로그 검수에서
반복 지적(persisting)된 문제의 원인을 진단하고, 아래 수단 안에서 해결을 시도하라.
사람 확인 없이 바로 실행된다 — 확신 없는 변경은 하지 말고 manual로 분류하라.

## 반복 지적 (이번 검수, 오늘 {today})
{persisting_block}

## 과거 시도 이력 (같은 지적에 실패한 접근·값은 반복 금지)
{history_block}

## 방금 오케스트레이터가 이미 실행한 액션 (중복 조치 금지)
{done_block}

## 사용 가능한 수단
1. catalog — 액션 카탈로그 실행 (블로그 설정 변경):
{catalog}
2. policy — 글 생성 정책 힌트 주입 (set_writer_hint / set_image_style_hint 액션):
   '생성물'에 대한 지적(문체·썸네일 획일화·표 가독성·각주 표기 등)은 이 경로가 정답이다 —
   다음 글부터 작문·이미지 프롬프트에 힌트가 반영된다
3. manual — 코드 수정이 필요하거나(렌더링·발행 로직 등) 위 수단으로 불가능하면:
   원인 분석 + 사람이 바로 실행할 수 있는 구체 수정 지시서를 써라

## 규칙
- 지적마다 issue_key(짧은 영문 슬러그, 예: thumb-uniform)를 부여하라 —
  과거 이력에 같은 지적이 있으면 반드시 그 키를 재사용한다
- 같은 issue_key에서 이미 failed한 접근·값은 반복 금지 — 다른 접근을 시도하라
- 과거 failed가 {max_failed}회인 지적은 반드시 manual로 분류하라
- 발행된 글 자체를 수정·이동·삭제하는 해결책은 어떤 수단에도 없다 — 그런 건 manual

JSON만 출력:
{{"resolutions": [{{"issue_key": "thumb-uniform", "issue_text": "지적 원문 그대로",
  "diagnosis": "원인 진단 1~2문장", "approach": "catalog|policy|manual",
  "action": {{"action": "액션명", "value": "값"}},
  "manual_brief": {{"cause": "원인 분석", "instruction": "구체 수정 지시서 (대상 파일·방향 포함)"}}}}]}}
(approach가 catalog/policy면 action 필수, manual이면 manual_brief 필수)"""


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
    cutoff = datetime.now() - timedelta(days=HINT_COOLDOWN_DAYS)
    for h in history:
        if h["approach"] != "policy":
            continue
        try:
            if (action_name in (h["attempt_json"] or "")
                    and datetime.fromisoformat(h["created_at"]) >= cutoff):
                return True
        except (ValueError, TypeError):
            continue
    return False


def try_resolve(report: dict, curated_actions: list[dict],
                conn: sqlite3.Connection) -> dict:
    """이번 persisting 진단·해결 시도. 반환: {"lines": 텔레그램 보고용 줄 목록}."""
    persisting = [str(p) for p in report.get("persisting", [])][:MAX_ISSUES_PER_CYCLE]
    if not persisting:
        return {"lines": []}
    today = date.today().isoformat()

    # 과거 시도 이력 — 실패 반복 방지·issue_key 재사용·냉각기 판정의 재료
    history = [dict(r) for r in conn.execute(
        "SELECT issue_key, issue_text, diagnosis, approach, attempt_json, result, created_at "
        "FROM resolution_attempts ORDER BY id DESC LIMIT 30")]
    gave_up_keys = {h["issue_key"] for h in history if h["result"] == "gave_up"}
    failed_counts = Counter(h["issue_key"] for h in history if h["result"] == "failed")

    catalog = "\n".join(f"   - {name}: {desc}"
                        for name, (desc, _) in blog_actions.ACTION_CATALOG.items())
    history_block = "\n".join(
        f"- [{h['issue_key']}] {h['result']} / {h['approach']}: {(h['attempt_json'] or '')[:80]}"
        for h in history[:15]) or "(없음)"
    prompt = RESOLVE_PROMPT.format(
        today=today,
        persisting_block="\n".join(f"- {p}" for p in persisting),
        history_block=history_block,
        done_block=json.dumps(curated_actions or [], ensure_ascii=False)[:500],
        catalog=catalog, max_failed=MAX_FAILED_BEFORE_GIVEUP)
    raw = llm.chat(config.MODEL_STEER, prompt, purpose="persisting-resolve")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        resolutions = json.loads(raw).get("resolutions", [])
    except json.JSONDecodeError:
        return {"lines": [f"⚠️ 해결 진단 응답 파싱 실패 — 이번 회차 건너뜀: {raw[:100]}"]}

    lines, seen_keys = [], set()
    for r in resolutions[:MAX_ISSUES_PER_CYCLE]:
        key = (str(r.get("issue_key") or "").strip() or "unknown")
        issue_text = str(r.get("issue_text") or "")[:300]
        if key in seen_keys:
            continue  # 제동 ①: 같은 지적은 회차당 1시도
        seen_keys.add(key)
        if key in gave_up_keys:
            continue  # 이미 수동 전환된 지적 — 재시도 금지 (리마인더가 계속 알린다)

        approach = r.get("approach")
        # 제동 ②: 실패 2회면 LLM 출력과 무관하게 강제 수동 전환
        if failed_counts.get(key, 0) >= MAX_FAILED_BEFORE_GIVEUP:
            approach = "manual"

        if approach in ("catalog", "policy"):
            action = r.get("action") or {}
            # 제동 ③: 정책 힌트는 같은 액션 7일 냉각기
            if (action.get("action") in blog_actions.POLICY_HINT_ACTIONS
                    and _hint_in_cooldown(history, action["action"])):
                lines.append(f"🧊 [{key}] {action['action']} 냉각기(7일) — 이번 회차 보류")
                continue
            try:
                results = blog_actions.apply(
                    [{"action": action.get("action"), "value": action.get("value")}])
                ok = bool(results and results[0].get("ok"))
                detail = (results[0].get("detail", "") if results else "결과 없음")
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            conn.execute(
                "INSERT INTO resolution_attempts (date, issue_key, issue_text, diagnosis, "
                "approach, attempt_json, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (today, key, issue_text, str(r.get("diagnosis") or ""), approach,
                 json.dumps({"action": action, "exec_ok": ok, "detail": detail},
                            ensure_ascii=False),
                 # 실행 자체가 실패하면 곧장 failed — 효과 판정을 기다릴 것이 없다
                 "tried" if ok else "failed"))
            lines.append(
                f"🔧 [{key}] {str(r.get('diagnosis') or '')[:70]}\n"
                f"  시도: {action.get('action')} = {str(action.get('value'))[:50]}"
                + (" — 실행됨, 다음 검수에서 효과 확인" if ok
                   else f" — 실행 실패: {str(detail)[:60]}"))
        else:  # manual — 원인 분석 + 지시서를 남기고 수동 전환
            brief = r.get("manual_brief") or {}
            conn.execute(
                "INSERT INTO resolution_attempts (date, issue_key, issue_text, diagnosis, "
                "approach, attempt_json, result) VALUES (?, ?, ?, ?, 'manual', ?, 'gave_up')",
                (today, key, issue_text, str(r.get("diagnosis") or ""),
                 json.dumps(brief, ensure_ascii=False)))
            lines.append(
                f"🙋 [{key}] 자동 해결 불가 — 수동 전환\n"
                f"  원인: {str(brief.get('cause') or r.get('diagnosis') or '')[:80]}\n"
                f"  지시: {str(brief.get('instruction') or '')[:120]}")
    conn.commit()
    return {"lines": lines}


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
