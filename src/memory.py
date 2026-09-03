"""콘텐츠 메모리 — 발행한 글을 '의미'로 조회하는 검색층 (2026-09-04, 로드맵 #5).

왜 필요한가: 글이 수백 건 쌓이면 DB에 넣기만 해서는 죽은 데이터가 된다. 사람은 raw DB를
안 뒤지고, 지금 시스템은 '정확히 같은 키워드'만 중복 처리(topics.recent_keywords)라
"퇴직연금 중도인출 관련해서 우리가 뭐라 썼지?" 같은 의미 조회를 못 한다.

이 모듈은 각 발행 글을 임베딩(text-embedding-3-small)해 저장하고, 질문을 받으면 의미가
가까운 과거 글 top-k를 돌려준다. 소비자는 사람이 아니라 LLM 에이전트다 — 발굴이 중복을
피하고, 잘된(인용 받은) 유형을 이어가게 하는 '되먹임 메모리'.

순수 파이썬 코사인 유사도를 쓴다(글 수백 개 규모라 numpy 불필요). 임베딩은 JSON으로 저장.
"""

import json
import math
from pathlib import Path

from src import config, db, llm


def _post_text(row) -> str:
    """임베딩할 텍스트 — 제목(핵심 신호) + 본문 앞부분(맥락 보강)."""
    title = row["title"] or ""
    body = ""
    try:
        p = row["body_path"]
        if p and Path(p).exists():
            body = Path(p).read_text(encoding="utf-8")[:1500]
    except Exception:
        pass
    return f"{title}\n{body}".strip()


def index_post(conn, post_id: int) -> bool:
    """글 하나를 임베딩해 post_embeddings에 저장(있으면 갱신). 성공 True."""
    row = conn.execute(
        "SELECT id, title, body_path FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        return False
    text = _post_text(row)
    if not text:
        return False
    vec = llm.embed(text, purpose="content-memory-index")
    conn.execute(
        "INSERT OR REPLACE INTO post_embeddings (post_id, model, vec, indexed_at) "
        "VALUES (?, ?, ?, datetime('now', 'localtime'))",
        (post_id, config.MODEL_EMBED, json.dumps(vec)))
    conn.commit()
    return True


def backfill(conn) -> int:
    """발행된(verified/published) 글 중 아직 색인 안 된 것들을 모두 색인. 색인 수 반환."""
    rows = conn.execute(
        "SELECT p.id FROM posts p LEFT JOIN post_embeddings e ON p.id = e.post_id "
        "WHERE p.status IN ('verified', 'published') AND e.post_id IS NULL "
        "ORDER BY p.id").fetchall()
    n = 0
    for r in rows:
        try:
            if index_post(conn, r["id"]):
                n += 1
        except Exception as e:
            print(f"색인 실패 post {r['id']}: {type(e).__name__}: {e}")
    return n


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def retrieve(conn, query: str, k: int = 5, min_sim: float = 0.0) -> list[dict]:
    """질문/키워드와 의미적으로 가까운 과거 발행 글 top-k.

    반환: [{post_id, title, url, category, sim, ai_cited}] — sim 내림차순.
    ai_cited는 그 글이 AI 브리핑 인용을 받은 적 있는지(rankings) — '잘된 유형' 신호.
    """
    qv = llm.embed(query, purpose="content-memory-query")
    rows = conn.execute(
        "SELECT e.post_id, e.vec, p.title, p.publish_url, t.category "
        "FROM post_embeddings e JOIN posts p ON e.post_id = p.id "
        "LEFT JOIN topics t ON p.topic_id = t.id").fetchall()
    scored = []
    for r in rows:
        try:
            sim = _cosine(qv, json.loads(r["vec"]))
        except Exception:
            continue
        if sim >= min_sim:
            scored.append({"post_id": r["post_id"], "title": r["title"],
                           "url": r["publish_url"], "category": r["category"],
                           "sim": round(sim, 3)})
    scored.sort(key=lambda x: -x["sim"])
    top = scored[:k]
    for s in top:
        c = conn.execute(
            "SELECT MAX(ai_cited) AS c FROM rankings WHERE post_id = ?",
            (s["post_id"],)).fetchone()
        s["ai_cited"] = bool(c and c["c"])
    return top


def related_summary(conn, query: str, k: int = 3, min_sim: float = 0.35) -> str:
    """발굴·작문 프롬프트에 끼울 한 덩이 텍스트 — '관련 과거 글' 요약(없으면 빈 문자열).

    min_sim 이상만(무관한 글로 프롬프트를 더럽히지 않게). 인용 받은 글은 표식.
    """
    hits = retrieve(conn, query, k=k, min_sim=min_sim)
    if not hits:
        return ""
    lines = []
    for h in hits:
        mark = " [인용받음]" if h["ai_cited"] else ""
        lines.append(f"- {h['title']} (유사도 {h['sim']}){mark}")
    return "관련 과거 발행 글:\n" + "\n".join(lines)
