"""일회성 정리 (2026-08-17) — C5가 지적한 중복 초안 폐기 + 랭킹 중복 제거.

유니크 인덱스(idx_rankings_daily) 생성 전에 중복 행부터 지워야 db.connect()가 성공한다.
"""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config

conn = sqlite3.connect(config.DB_PATH)  # db.connect() 대신 직접 — 인덱스 생성 전 정리용
conn.execute("UPDATE posts SET status='skipped' WHERE id=2 AND status='gated'")
n = conn.execute(
    "DELETE FROM rankings WHERE rowid NOT IN "
    "(SELECT MIN(rowid) FROM rankings GROUP BY date, post_id)").rowcount
conn.commit()
conn.close()
print(f"정리 완료 — 중복 랭킹 {n}행 제거, 구버전 초안 폐기")
