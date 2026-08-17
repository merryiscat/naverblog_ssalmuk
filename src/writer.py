"""C2 글 생성 + 품질 게이트 — 주제를 받아 인용되기 좋은 정보형 글을 만든다.

템플릿·채점 기준의 근거는 docs/mate-analysis.md "C2 글 템플릿·품질 게이트 사양":
  템플릿 7요소 — ①첫 문단 즉답 ②H2/H3 위계 ③고정 스키마 표 ④통계·수치+출처
                 ⑤FAQ 절 ⑥기간·집계 기준 명시 ⑦발행일 명시
  게이트 6차원 — 사실성 / 구조 / 去AI味 / 키워드 스터핑 감점 / 두괄식 / 구체성

흐름: 리서치(검색 API 스니펫) → 초안(gpt-4.1) → 게이트(gpt-5.4-mini 채점)
      → 불합격이면 피드백을 넣어 재생성 (최대 GATE_MAX_RETRIES회) → 실패 시 스킵.
"""

import json
import re
import sqlite3
from datetime import date

from src import config, db, llm
from src.naver_api import openapi_search

# 게이트 합격선 (100점 만점). 낮추는 것은 사람만 할 수 있다 (가드레일 ④의 연장)
GATE_PASS_SCORE = 70

# 去AI味 — 네이버 블로그에서 'AI 티'로 읽히는 상투 표현들 (게이트가 감점)
AI_CLICHES = ["결론적으로", "종합해보면", "~하는 것이 중요합니다", "알아보도록 하겠습니다",
              "도움이 되셨기를", "지금까지 ~에 대해 알아보았습니다", "혁신적인", "게임 체인저"]


def _strip_tags(s: str) -> str:
    """검색 API 응답의 <b> 태그 등 HTML 제거."""
    return re.sub(r"<[^>]+>", "", s).replace("&quot;", '"').replace("&amp;", "&")


def gather_research(keyword: str) -> list[dict]:
    """검색 API(뉴스·웹문서·블로그)에서 사실 스니펫을 모은다.

    v1은 스니펫 수준 — 본문 전문 수집(정밀 리서치)은 측정 결과를 보고 보강한다.
    """
    sources = []
    for kind, n in (("news", 6), ("webkr", 6), ("blog", 4)):
        try:
            for item in openapi_search(kind, keyword, display=n).get("items", []):
                sources.append({
                    "kind": kind,
                    "title": _strip_tags(item.get("title", "")),
                    "snippet": _strip_tags(item.get("description", "")),
                    "url": item.get("link", ""),
                    "date": item.get("pubDate", item.get("postdate", "")),
                })
        except Exception:
            continue  # 한 소스가 죽어도 나머지로 진행
    return sources


def write_draft(topic: dict, research: list[dict], feedback: str | None = None) -> str:
    """초안 작성 — 작문 모델(gpt-4.1)에 템플릿 7요소를 강제한다."""
    src_block = "\n".join(
        f"[{i+1}] ({s['kind']}) {s['title']} — {s['snippet']} ({s['url']})"
        for i, s in enumerate(research)
    )
    # 분야별 조정 (mate-analysis: 테크·인사이트=통계·출처↑, 라이프·푸드·여행=가독성↑)
    tone = ("통계·수치와 출처 인용의 밀도를 높여라"
            if topic.get("category") in ("테크", "인사이트", "미디어")
            else "쉬운 설명과 읽기 편한 흐름을 우선하되 수치는 정확히 써라")

    prompt = f"""네이버 블로그에 올릴 정보 정리·분석형 글을 작성하라. 주제: "{topic['keyword']}" (분야: {topic.get('category', '일반')})

목표: 네이버 AI 브리핑이 인용하기 좋은 글. 아래 7요소를 반드시 지켜라:
1. 첫 문단에서 검색 의도에 바로 답한다 (두괄식 즉답 — 서론·인사말 금지)
2. H2/H3 헤딩 위계 (마크다운 ## / ###)
3. 핵심 정보를 정리한 표 최소 1개 (동일 스키마, 마크다운 표)
4. 통계·수치를 쓸 때마다 아래 리서치 소스 번호로 출처 표기 — 예: (출처: [3])
5. 마지막에 FAQ 절 (## 자주 묻는 질문, Q 3개 이상)
6. 정보의 기준 시점을 명시한다 ("2026년 8월 기준")
7. {tone}

금지사항:
- 리서치 소스에 없는 사실을 지어내지 않는다 (모르면 "확인 필요"로 남긴다)
- 경험담·후기 톤 금지 ("제가 써보니" 등) — 정보 정리자의 톤
- AI 상투어 금지: {", ".join(AI_CLICHES)}
- 같은 키워드를 기계적으로 반복하지 않는다 (키워드 스터핑은 역효과)

리서치 소스:
{src_block}

{f'이전 초안의 게이트 불합격 피드백 — 반드시 고쳐라: {feedback}' if feedback else ''}

출력: 첫 줄에 "제목: <제목>", 둘째 줄부터 마크다운 본문."""
    return llm.chat(config.MODEL_WRITER, prompt, purpose="write-draft")


def gate_draft(draft: str, topic: dict, research: list[dict]) -> dict:
    """품질 게이트 — 판단 모델이 6차원 채점. 반환: {scores, total, passed, feedback}."""
    src_block = "\n".join(f"[{i+1}] {s['title']} — {s['snippet']}" for i, s in enumerate(research))
    prompt = f"""다음 블로그 초안을 채점하라. 주제: "{topic['keyword']}"

채점 기준 (각 0~100):
- factual: 본문의 사실·수치가 리서치 소스와 부합하나? 소스에 없는 주장에는 감점
- structure: 두괄식 즉답 / H2·H3 / 표 / FAQ / 기준 시점 명시가 갖춰졌나
- deai: AI 상투어·기계적 문체가 없는가 (자연스러운 한국어 블로그 문체)
- stuffing: 키워드 기계 반복이 없는가 (반복 심할수록 낮게)
- frontload: 첫 문단이 검색 의도에 즉답하나
- specificity: 모호한 서술 대신 구체 수치·근거가 있나

리서치 소스:
{src_block}

초안:
{draft[:6000]}

JSON만 출력:
{{"factual": 0, "structure": 0, "deai": 0, "stuffing": 0, "frontload": 0, "specificity": 0,
 "feedback": "불합격 원인과 고칠 점을 구체적으로 (합격이어도 개선점 한 줄)"}}"""
    raw = llm.chat(config.MODEL_JUDGE, prompt, purpose="quality-gate")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    scores = json.loads(raw)
    dims = ["factual", "structure", "deai", "stuffing", "frontload", "specificity"]
    total = sum(float(scores.get(d, 0)) for d in dims) / len(dims)
    # 사실성은 단독 커트라인 — 평균이 높아도 사실성이 낮으면 불합격 (허위 정보 방지)
    passed = total >= GATE_PASS_SCORE and float(scores.get("factual", 0)) >= 60
    return {"scores": scores, "total": round(total, 1), "passed": passed,
            "feedback": scores.get("feedback", "")}


def generate(topic: dict, conn: sqlite3.Connection | None = None) -> dict:
    """주제 하나 → 리서치 → 초안 → 게이트 (재시도 포함) → posts 기록.

    반환: {status: 'gated'|'skipped', post_id, body_path, gate, attempts}
    """
    own = conn is None
    conn = conn or db.connect()
    today = date.today().isoformat()
    try:
        research = gather_research(topic["keyword"])
        if len(research) < 3:
            return {"status": "skipped", "reason": f"리서치 소스 부족 ({len(research)}건 < 3)"}

        draft, gate, feedback = "", None, None
        attempts = 0
        for attempts in range(1, config.GATE_MAX_RETRIES + 2):  # 최초 1회 + 재시도
            draft = write_draft(topic, research, feedback)
            gate = gate_draft(draft, topic, research)
            if gate["passed"]:
                break
            feedback = gate["feedback"]

        title = draft.splitlines()[0].removeprefix("제목:").strip() if draft else topic["keyword"]
        body = "\n".join(draft.splitlines()[1:]).strip()

        if not gate["passed"]:
            conn.execute(
                "INSERT INTO posts (topic_id, title, gate_json, status) VALUES (?, ?, ?, 'skipped')",
                (topic.get("id"), title, json.dumps(gate, ensure_ascii=False)),
            )
            conn.commit()
            return {"status": "skipped", "reason": f"게이트 {attempts}회 불합격 (총점 {gate['total']})",
                    "gate": gate}

        # 합격 — 본문 파일 저장 + posts 기록 (발행은 C3의 일)
        config.ensure_dirs()
        safe_kw = re.sub(r"[^\w가-힣]", "_", topic["keyword"])
        body_path = config.POSTS_DIR / f"{today}-{safe_kw}.md"
        body_path.write_text(f"# {title}\n\n{body}", encoding="utf-8")

        cur = conn.execute(
            "INSERT INTO posts (topic_id, title, body_path, gate_json, status) "
            "VALUES (?, ?, ?, ?, 'gated')",
            (topic.get("id"), title, str(body_path), json.dumps(gate, ensure_ascii=False)),
        )
        conn.commit()
        return {"status": "gated", "post_id": cur.lastrowid, "title": title,
                "body_path": str(body_path), "gate": gate, "attempts": attempts}
    finally:
        if own:
            conn.close()
