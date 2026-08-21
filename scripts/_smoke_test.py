# 일회성 스모크 테스트 — 마이그레이션·resolver 판정·요약 함수 (LLM·네트워크 호출 없음)
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("NAVER_BLOG_ID", "testblog")

from src import config

tmp = tempfile.mktemp(suffix=".db")
config.DB_PATH = tmp

from src import db

conn = db.connect()
cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
assert {"skip_reason", "first_indexed_at"} <= cols, cols
rcols = {r["name"] for r in conn.execute("PRAGMA table_info(rankings)")}
assert "indexed" in rcols, rcols
tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert "resolution_attempts" in tables, tables
print("마이그레이션 OK")

from src import resolver

conn.execute("INSERT INTO resolution_attempts (date, issue_key, issue_text, approach, attempt_json, result) "
             "VALUES ('2026-08-20', 'thumb-uniform', '썸네일이 획일적으로 반복됨', 'policy', '{}', 'tried')")
conn.execute("INSERT INTO resolution_attempts (date, issue_key, issue_text, approach, attempt_json, result) "
             "VALUES ('2026-08-20', 'footnote-raw', '각주 번호가 원시 텍스트로 노출', 'manual', '{}', 'gave_up')")
conn.commit()
report = {"resolved": ["각주 번호가 원시 텍스트로 노출"],
          "persisting": ["썸네일이 획일적으로 반복됨 (여전)"]}
log = resolver.update_outcomes(report, conn)
rows = {r["issue_key"]: r["result"] for r in conn.execute("SELECT issue_key, result FROM resolution_attempts")}
assert rows["thumb-uniform"] == "failed", rows
assert rows["footnote-raw"] == "verified", rows
print("update_outcomes OK:", log)
assert resolver.pending_manuals(conn) == []  # verified로 바뀌어 리마인더에서 소거
print("pending_manuals 소거 OK")

from src import writer

s = writer.gate_fail_summary({
    "scores": {"factual": 48, "freshness": 58, "structure": 78, "deai": 54,
               "stuffing": 70, "frontload": 82, "specificity": 62},
    "total": 64.6,
    "feedback": "사실 정확도와 최신성에서 감점이 큽니다. 지원금 액수가 흔들려 신뢰도가 떨어집니다."})
print("gate_fail_summary:", s)
assert "사실성 48" in s and "총점 64.6" in s

import src.blog_actions
import src.metrics
import src.orchestrator
import src.scheduler
import src.topics

print("전 모듈 임포트 OK")
conn.close()
os.remove(tmp)
