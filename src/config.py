"""설정 — .env 로드, 경로, 모델 단가, 하드 가드레일 상수.

하드 가드레일 4종은 여기 상수로 고정한다. 일일 자가 보정(LLM)은 이 값을
읽을 수만 있고 바꿀 수 없다 — 바꾸는 방법은 사람이 이 파일을 수정하는 것뿐.
(usecases.md "운영 원칙" 참조)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 (이 파일의 두 단계 위 폴더)
ROOT = Path(__file__).resolve().parent.parent

# .env 파일에서 API 키 등을 읽어온다
load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# 하드 가드레일 — 시스템(자가 보정 LLM 포함)이 절대 넘을 수 없는 선
# ---------------------------------------------------------------------------

# ① 하루 최대 발행 건수. 네이버 어뷰징 필터("작성자의 활동 패턴을 본다") 대응 —
#    'AI 따발총' 계정 제재 사례가 있어 이 값이 곧 생존 조건이다.
#    2026-08-20 사용자 결정으로 2→4 상향: 기본 3건 선정 + 여유 1(예비 투입·스나이핑).
#    발행 시각이 11~21시 창에 3시간+ 간격으로 분산되므로 패턴상 여전히 사람 범위.
DAILY_PUBLISH_LIMIT = 4

# ①-b 하루 기본 선정(생성) 건수 — 오케스트레이터가 이 수만큼 selected를 고른다.
#    게이트 탈락으로 구멍이 나면 예비(reserve)를 자동 투입해 이 수를 지향한다 ("3+α"의 3).
DAILY_SELECT_COUNT = 3

# ①-c 예비(reserve) 주제 수 — 게이트 탈락 시 자동 투입되는 보충분.
#    2026-08-22 사용자 결정("하루 2건은 실제로 올라가야 한다")으로 1→2 확대.
#    8/21 생성 4건 전멸(발행 0건)이 계기 — 병목은 물량이 아니라 게이트 통과율이므로
#    발행 상한(①)은 그대로 두고 보충 여력만 늘린다.
RESERVE_COUNT = 2

# ①-d 하루 실발행 최소 목표 — 생성 사이클이 이 수의 합격에 못 미치면
#    예비를 계속 투입하고, 그래도 미달이면 텔레그램 소환(사람 개입)을 부른다.
DAILY_PUBLISH_TARGET = 2

# ①-e 생성 사이클 총 시도 상한 — selected 3 + reserve 2 = 5. 무한 보충 방지.
GENERATE_MAX_ATTEMPTS = 5

# ② 월 운영비 상한 (원). 도달하면 파이프라인 자동 정지 + 텔레그램 알림.
MONTHLY_BUDGET_KRW = 30_000
# 비용은 달러로 적산하므로 환산 환율을 둔다 (보수적으로 넉넉히 잡음)
USD_KRW = 1_450
MONTHLY_BUDGET_USD = MONTHLY_BUDGET_KRW / USD_KRW

# ③ 발행 외 상호작용(공감·댓글·서로이웃) 자동화 금지 —
#    2025-07부터 네이버가 봇 행위를 강력 차단, 계정 제재 사례 있음.
#    이 항목은 상수가 아니라 "그런 코드를 만들지 않는다"는 규칙이다.

# 대표 이미지 생성 스위치 — 2026-08-26 재활성화 (텍스트 없는 이미지 게이트 채택).
# gpt-image-1이 깨진 한글을 넣던 문제는 writer._image_has_text 비전 게이트로 차단:
# 생성 후 글자 검사 → 있으면 1회 재생성 → 그래도 있으면 이미지 없이 발행.
HERO_IMAGE_ENABLED = True

# ④ 품질 게이트 미달 글 발행 금지 — 재생성 최대 횟수만 여기 둔다.
#    2026-08-22 2→3 상향: 이틀 연속 전멸(8/21 4건·8/22 5건)로 보완 여력 확대 —
#    "낮게 나와서 못 올림 끝이면 문제"(사용자). 글당 최대 4회 작성 (~$0.1) 수용.
GATE_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# 경로 — 런타임 데이터는 전부 data/ 아래 (git 제외 대상)
# ---------------------------------------------------------------------------

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "naverblog.db"
SESSION_PATH = DATA_DIR / "session" / "storage_state.json"  # 네이버 로그인 세션
POSTS_DIR = DATA_DIR / "posts"    # 생성된 글 본문(마크다운/HTML)
IMAGES_DIR = DATA_DIR / "images"  # 생성된 썸네일·본문 이미지


def ensure_dirs() -> None:
    """data/ 아래 필요한 폴더를 만들어 둔다 (없으면 생성, 있으면 그대로)."""
    for d in (DATA_DIR, SESSION_PATH.parent, POSTS_DIR, IMAGES_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# OpenAI 모델 역할 분담과 단가 (상위 CLAUDE.md 모델 선택 원칙 준수)
# ---------------------------------------------------------------------------

MODEL_WRITER = "gpt-4.1"        # 글 작문 (코딩/작문 특화)
MODEL_JUDGE = "gpt-5.4-mini"    # 판단 — 주제 스코어링, 품질 게이트 채점
MODEL_STEER = "gpt-5.4"         # 일일 자가 보정 (최상위 판단)
MODEL_AGENT = "gpt-5.4-mini"    # 메이트 관찰 에이전트 (도구 사용·탐색 판단)
MODEL_INSPECTOR = "gpt-5.4-mini"  # 블로그 검수 에이전트 (스크린샷 비전 검수)
MODEL_IMAGE = "gpt-image-1"       # 대표 이미지 생성

# 1M 토큰당 달러 단가: (입력, 출력). 비용 적산(llm.py)에 쓴다.
PRICES_PER_MTOK = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}

# 이미지 1장당 추정 비용(달러) — 실제 단가는 크기·품질에 따라 다르므로
# 보수적(높게) 잡고, 정확한 값은 이미지 기능 구현 시 보정한다.
IMAGE_COST_USD = 0.05

# ---------------------------------------------------------------------------
# 환경 변수 (비어 있으면 셋업 위저드가 채우도록 안내)
# ---------------------------------------------------------------------------

# Playwright headless의 기본 UA는 'HeadlessChrome'이라 네이버가 세션을 거부한다
# — 발행·측정 등 세션 브라우저는 모두 이 UA를 쓴다
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
SEARCHAD_API_KEY = os.getenv("SEARCHAD_API_KEY", "")
SEARCHAD_SECRET_KEY = os.getenv("SEARCHAD_SECRET_KEY", "")
SEARCHAD_CUSTOMER_ID = os.getenv("SEARCHAD_CUSTOMER_ID", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NAVER_BLOG_ID = os.getenv("NAVER_BLOG_ID", "")  # blog.naver.com/<이 값>
