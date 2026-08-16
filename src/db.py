"""SQLite 저장소 — 파이프라인의 모든 기록이 이 파일 하나(data/naverblog.db)에 쌓인다.

테이블 구성 (usecases.md 공통 전제 2·6 반영):
  topics      — 매일 발굴한 주제 후보와 선정 결과 (C1)
  posts       — 글 초안부터 발행·검증까지의 생애 주기 (C2·C3)
  metrics     — 일별 측정 스냅샷: 인용수·방문자 (C4)
  rankings    — 발행 글의 키워드별 검색 노출 순위 (C4)
  decisions   — 일일 자가 보정의 결정과 근거 (C5) — "왜 바꿨는지"가 반드시 남는다
  policy      — 현재 방향(주제 풀·템플릿 등) 스냅샷, 최신 행이 유효 (C5)
  competitors — AI 브리핑에 인용된 경쟁 글 관찰 (C6)
  costs       — 모든 LLM·이미지 호출 비용 원장 (가드레일 ②의 근거 데이터)
"""

import sqlite3

from src import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,              -- 발굴 날짜 (YYYY-MM-DD)
    keyword     TEXT NOT NULL,              -- 대상 키워드/주제
    search_vol  INTEGER,                    -- 월간 검색량 (검색광고 API, 없으면 NULL)
    doc_count   INTEGER,                    -- 네이버 문서 수
    golden_score REAL,                      -- 검색량 ÷ 문서수
    llm_score   REAL,                       -- LLM 선제 스코어링 점수 (0~1)
    rationale   TEXT,                       -- 선정/탈락 근거
    status      TEXT NOT NULL DEFAULT 'candidate',  -- candidate/selected/reserve/rejected/used
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id    INTEGER REFERENCES topics(id),
    title       TEXT,
    body_path   TEXT,                       -- 본문 파일 경로 (data/posts/)
    images_json TEXT,                       -- 이미지 파일 경로 목록 (JSON 배열)
    gate_json   TEXT,                       -- 품질 게이트 채점 결과 (JSON)
    status      TEXT NOT NULL DEFAULT 'draft',
                -- draft(초안) / gated(게이트 통과) / published(발행)
                -- / verified(실게시 확인) / skipped(스킵) / failed(실패)
    publish_url TEXT,                       -- 발행된 공개 URL
    published_at TEXT,
    verified_at TEXT,                       -- 비로그인 실게시 확인 시각 (가짜 성공 방지)
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL UNIQUE,       -- 측정 날짜 (하루 한 행)
    citations   INTEGER,                    -- 프로필 누적 인용수 스냅샷
    visitors    INTEGER,                    -- 블로그 방문자 수
    details_json TEXT,                      -- 유입 키워드 등 부가 데이터 (JSON)
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS rankings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id),
    date        TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    rank        INTEGER,                    -- 검색 노출 순위 (미노출이면 NULL)
    ai_cited    INTEGER NOT NULL DEFAULT 0, -- AI 브리핑 인용 여부 (0/1)
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    input_summary TEXT,                     -- 결정에 쓰인 데이터 요약
    decision_json TEXT NOT NULL,            -- 무엇을 어떻게 바꿨나 (JSON)
    rationale   TEXT NOT NULL,              -- 왜 — 전권 위임의 최소 조건
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS policy (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_json TEXT NOT NULL,              -- 현재 방향 전체 (주제 풀·템플릿·실험 가설)
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS competitors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    url         TEXT NOT NULL,
    structure_json TEXT,                    -- 헤딩·표·글자수·이미지 수 등 구조 분석
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    kind        TEXT NOT NULL,              -- 'text' 또는 'image'
    model       TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL,              -- 이 호출의 비용 (달러)
    purpose     TEXT                        -- 무엇을 위한 호출이었나 (예: 'topic-scoring')
);

CREATE INDEX IF NOT EXISTS idx_costs_ts ON costs(ts);
CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at);
"""


def connect() -> sqlite3.Connection:
    """DB에 연결하고 스키마가 없으면 만든다. 모든 모듈이 이 함수로 연결한다."""
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row  # 컬럼 이름으로 값을 꺼낼 수 있게
    conn.executescript(SCHEMA)
    return conn
