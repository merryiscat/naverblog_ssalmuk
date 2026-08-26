"""기발행 글의 대표 이미지 제거 — 깨진 한글 텍스트 이미지 일괄 삭제 (사용자 "그냥 삭제").

각 글의 images_json을 비우고 post_editor로 재게시(본문만, 이미지 없이)한다.
파괴적 자동화 규칙 준수: post_editor가 제목 재검증·재조회 검증·재시도 금지를 내장.
사람 같은 간격(90초)으로 순차 실행.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src import db, post_editor

conn = db.connect()
targets = conn.execute(
    "SELECT id, title FROM posts WHERE status IN ('published','verified') "
    "AND images_json IS NOT NULL AND images_json != '[]' AND images_json != '' "
    "ORDER BY id").fetchall()
print(f"대상 {len(targets)}건 (이미지 있는 발행 글)")

for row in targets:
    pid = row["id"]
    # images_json을 비운 뒤 재게시 → 이미지 없이 본문만 다시 올라간다
    conn.execute("UPDATE posts SET images_json = '[]' WHERE id = ?", (pid,))
    conn.commit()
    try:
        r = post_editor.update_post(pid, conn)
        print(f"  post {pid} {row['title'][:28]}: {r['ok']} — {r['detail']}")
    except Exception as e:
        print(f"  post {pid} 실패: {type(e).__name__}: {str(e)[:120]} — 재시도 안 함")
    time.sleep(90)

print("IMAGES_REMOVED_DONE")
