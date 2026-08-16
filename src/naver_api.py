"""네이버 API 클라이언트 — 오픈API(검색)와 검색광고 API 호출.

- 오픈API 검색: 헤더에 Client ID/Secret만 넣으면 되는 단순 호출 (일 25,000회)
- 검색광고 API: 요청마다 HMAC-SHA256 서명이 필요 (키워드 도구 = 월간 검색량·연관키워드)

두 API 모두 C1 주제 발굴의 핵심 데이터 소스다 (usecases.md P3·P4).
"""

import base64
import hashlib
import hmac
import time

import httpx

from src import config


# ---------------------------------------------------------------------------
# 오픈API — 검색 (블로그·웹문서 등)
# ---------------------------------------------------------------------------

def openapi_search(kind: str, query: str, display: int = 10, start: int = 1) -> dict:
    """네이버 검색 오픈API 호출. kind: 'blog' | 'webkr' | 'news' | 'cafearticle' 등.

    반환 JSON의 total 필드가 곧 '문서수' — 골든키워드 점수의 분모다.
    """
    # 연속 대량 호출 시 429가 나므로 기본 간격 + 지수 백오프 (일 한도와 별개의 초당 제한)
    for attempt in range(4):
        time.sleep(0.25 if attempt == 0 else 2 * (2 ** (attempt - 1)))
        r = httpx.get(
            f"https://openapi.naver.com/v1/search/{kind}.json",
            params={"query": query, "display": display, "start": start},
            headers={
                "X-Naver-Client-Id": config.NAVER_CLIENT_ID,
                "X-Naver-Client-Secret": config.NAVER_CLIENT_SECRET,
            },
            timeout=15,
        )
        if r.status_code != 429:
            break
    r.raise_for_status()
    return r.json()


def doc_count(query: str) -> int:
    """키워드의 블로그 문서수 (골든키워드 = 검색량 ÷ 이 값)."""
    return openapi_search("blog", query, display=1)["total"]


# ---------------------------------------------------------------------------
# 검색광고 API — 키워드 도구 (월간 검색량·연관키워드)
# ---------------------------------------------------------------------------

SEARCHAD_BASE = "https://api.searchad.naver.com"


def _searchad_headers(method: str, uri: str) -> dict:
    """검색광고 API 서명 헤더 생성 — 타임스탬프.메서드.URI를 비밀키로 HMAC 서명."""
    ts = str(round(time.time() * 1000))
    msg = f"{ts}.{method}.{uri}"
    sig = base64.b64encode(
        hmac.new(config.SEARCHAD_SECRET_KEY.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "X-Timestamp": ts,
        "X-API-KEY": config.SEARCHAD_API_KEY,
        "X-Customer": config.SEARCHAD_CUSTOMER_ID,
        "X-Signature": sig,
    }


def keyword_stats(hint_keywords: list[str]) -> list[dict]:
    """키워드 도구 조회 — 각 키워드의 월간 검색량(PC/모바일)과 연관키워드.

    hint_keywords는 최대 5개. 반환: keywordList 배열
    (monthlyPcQcCnt, monthlyMobileQcCnt 등. '< 10'은 문자열로 옴에 주의).
    ※ 이 API는 공백 포함 키워드를 400으로 거부한다 — 공백을 제거해 보낸다
      (네이버 키워드도구 UI도 같은 규칙).
    """
    uri = "/keywordstool"
    hints = [k.replace(" ", "") for k in hint_keywords]
    # 이 API는 호출 빈도 제한이 빡빡하다 (연속 호출 시 429).
    # 호출 전 기본 간격 + 429면 지수 백오프로 최대 3회 재시도.
    for attempt in range(4):
        time.sleep(1.5 if attempt == 0 else 5 * (2 ** (attempt - 1)))
        r = httpx.get(
            SEARCHAD_BASE + uri,
            params={"hintKeywords": ",".join(hints), "showDetail": "1"},
            headers=_searchad_headers("GET", uri),
            timeout=20,
        )
        if r.status_code != 429:
            break
    r.raise_for_status()
    return r.json().get("keywordList", [])
