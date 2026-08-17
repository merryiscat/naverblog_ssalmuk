"""C6 경쟁 관찰 단독 실행.

실행: uv run python scripts/run_competitors.py
※ 읽기 전용 관찰 — 발행 계열이 아니므로 개발 PC 실행 허용 (deploy.md 단일 운영 원칙 참조)
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import competitors, db


def main() -> None:
    conn = db.connect()
    result = competitors.observe(conn)

    print(f"관찰 키워드 {result['observed']}개")
    print(f"\n🎯 인용 공백 (스나이핑 후보): {len(result['opportunities'])}건")
    for o in result["opportunities"]:
        print(f"  · {o['keyword']} — 최신 출처가 {o['freshest_days']}일 전")

    print(f"\n브리핑 없는 키워드 (인용 기회 없음): {', '.join(result['no_briefing']) or '없음'}")

    print(f"\n인용 글 구조 표본 {len(result['patterns'])}건:")
    for p in result["patterns"][:8]:
        print(f"  [{p['keyword']}] {p['title'][:35]} — {p['chars']:,}자, "
              f"표 {p['tables']}, 이미지 {p['images']}, {p.get('days_old', '?')}일 전")
    conn.close()


if __name__ == "__main__":
    main()
