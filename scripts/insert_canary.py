"""유사-캐너리 실험 주제 삽입 — 발행→색인→AI 브리핑 반영 래그 실측용 (1회성 도구).

배경 (2026-08-22 사용자 결정): 우리 글이 언제 AI 인용에 걸리는지 알려면
색인·브리핑 반영에 걸리는 시간부터 실측해야 한다. 진짜 쌩뚱맞은 글은 블로그
정체성·품질 신호(C-rank)를 해치므로, 우리 분야(정책 계열 권장) 안에서 경쟁 문서가
거의 없는 초롱테일 고유 키워드로 '정상 품질' 글을 내보내고 추적한다 (유사-캐너리).

사용법 (⚠️ 운영 DB는 서버가 단일 출처 — 서버에서 실행한다, docs/deploy.md 원칙):
  cd ~/naverblog_ssalmuk
  .venv/bin/python3 scripts/insert_canary.py "키워드" "분야"
  예: .venv/bin/python3 scripts/insert_canary.py "1인 자영업자 폐업지원금 서류 준비" "라이프"

동작:
  ① 키워드의 블로그 문서수(경쟁)를 조회해 보여준다 — 많으면(>500) 경고 (캐너리 순도 하락)
  ② topics에 오늘 날짜 status='selected'로 삽입 (rationale에 [canary] 표기)
  ③ 다음 08:30 생성 사이클이 일반 파이프라인 그대로 작문·게이트·발행한다
  ④ 추적은 기존 인프라가 자동 수행: 매일 21:30 측정이 색인(posts.first_indexed_at,
     확인 시 텔레그램 🔎)과 브리핑·인용(rankings)을 기록한다
  ⑤ D+7쯤 rankings·first_indexed_at을 보고 래그를 docs/mate-analysis.md
     "인용 타임라인" 절에 실측 기록한다 — 이후 "발행 후 인용 확인 대기 일수"의 기준값

주의: 이날 selected가 1건 늘어난다 — 발행 상한(가드레일 ①, 일 4건)은 코드가
지키므로 안전하지만, 게이트 전원 합격 시 일반 글 1건이 다음 날로 밀릴 수 있다.
"""

import sys
from datetime import date
from pathlib import Path

# 프로젝트 루트를 임포트 경로에 추가 — scripts/ 밖의 src를 찾기 위함
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.naver_api import doc_count


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    keyword, category = sys.argv[1].strip(), sys.argv[2].strip()

    docs = doc_count(keyword)
    print(f"키워드: {keyword} / 분야: {category} / 경쟁 블로그 문서수: {docs:,}")
    if docs > 500:
        print("⚠️ 경쟁 문서가 많다 — 색인·인용 래그 측정의 순도가 떨어진다. "
              "더 긴 롱테일 조합을 권장.")
        if input("그래도 계속? [y/N] ").strip().lower() != "y":
            print("중단.")
            sys.exit(0)

    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO topics (date, keyword, category, doc_count, rationale, status) "
            "VALUES (?, ?, ?, ?, ?, 'selected')",
            (date.today().isoformat(), keyword, category, docs,
             "[canary] 색인·브리핑 반영 래그 실측용 유사-캐너리 (대화 2026-08-22)"))
        conn.commit()
        print(f"✅ 삽입 완료 (topic id {cur.lastrowid}) — 다음 08:30 생성 사이클에 포함된다.")
        print("추적은 자동: 21:30 측정이 색인·인용 기록. D+7쯤 결과를 mate-analysis.md에 기록할 것.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
