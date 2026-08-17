"""C3 발행 단독 실행 — 가장 최근 gated 글을 실제 발행한다.

실행: uv run python scripts/run_publisher.py [--headed]
실패(캡차·셀렉터 깨짐 등) 시 텔레그램 소환을 보낸다.
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, notify, publisher
from src.guardrails import GuardrailViolation


def main() -> None:
    headless = "--headed" not in sys.argv
    conn = db.connect()
    row = conn.execute(
        "SELECT id, title FROM posts WHERE status = 'gated' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("발행 대기(gated) 글이 없습니다.")
        sys.exit(1)

    print(f"발행 시도: [{row['id']}] {row['title']}")
    try:
        result = publisher.publish(row["id"], conn, headless=headless,
                                   tags=["넷플릭스", "넷플릭스요금제", "OTT"])
        print(f"결과: {result['status']}")
        print(f"URL: {result['url']}")
        if result["status"] == "verified":
            notify.send(f"📝 첫 발행 성공 (실게시 확인됨)\n{row['title']}\n{result['url']}")
        else:
            notify.send(f"⚠️ 발행됐으나 비로그인 실게시 확인 실패 — 수동 확인 필요\n{result['url']}")
    except GuardrailViolation as e:
        print(f"가드레일 차단: {e}")
    except publisher.PublishError as e:
        print(f"발행 실패: {e}")
        notify.summon(f"발행 자동화 실패\n{e}")
        sys.exit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
