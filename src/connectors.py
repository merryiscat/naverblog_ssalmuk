"""공식 데이터 커넥터 — 스니펫 리서치로 사실성이 안 나오는 주제에 공식 소스를 공급한다.

배경 (status.md 미해결): 순위류 주제는 검색 스니펫에 정확한 순위표가 없어
게이트(사실성)가 정상 차단해 왔다. 공식 데이터를 리서치 소스 [1]로 주입하면
작문이 지어낼 필요가 없어지고, 사실성·신선도 게이트를 정면으로 통과할 수 있다.

v1: 넷플릭스 주간 Top 10 (공식 Tudum 데이터 — 매주 화요일 갱신, 국가별 TSV 공개).
mate-analysis의 "주간 순위 시리즈 = 반복 인용 자산" 전략의 데이터 기반이다.
"""

import io
import urllib.request
from datetime import date, datetime

# 넷플릭스 공식 주간 Top 10 데이터 (모든 주·모든 국가 누적 TSV)
NETFLIX_TSV_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
NETFLIX_PAGE_URL = "https://www.netflix.com/tudum/top10/south-korea"


def netflix_top10_kr() -> dict | None:
    """한국 최신 주간 Top 10을 가져온다. 반환: {week, age_days, films, tv} 또는 None.

    TSV는 2021년부터의 누적(수만 행)이라 스트리밍으로 한국 행만 걸러낸다.
    """
    try:
        req = urllib.request.Request(NETFLIX_TSV_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = io.TextIOWrapper(resp, encoding="utf-8")
            header = next(text).rstrip("\n").split("\t")
            idx = {name: i for i, name in enumerate(header)}
            kr_rows = []
            for line in text:
                if "South Korea" not in line:
                    continue
                cols = line.rstrip("\n").split("\t")
                if cols[idx["country_name"]] == "South Korea":
                    kr_rows.append(cols)
        if not kr_rows:
            return None

        latest_week = max(r[idx["week"]] for r in kr_rows)
        films, tv = [], []
        for r in kr_rows:
            if r[idx["week"]] != latest_week:
                continue
            # 시즌명이 있으면 그것이 실제 화제작 이름 (예: "오징어 게임: 시즌 3")
            title = r[idx["show_title"]]
            season = r[idx["season_title"]] if "season_title" in idx else ""
            if season and season not in ("N/A", "", title):
                title = season
            entry = {"rank": int(r[idx["weekly_rank"]]), "title": title,
                     "weeks_in_top10": int(r[idx["cumulative_weeks_in_top_10"]] or 0)}
            (films if r[idx["category"]].startswith("Films") else tv).append(entry)

        films.sort(key=lambda e: e["rank"])
        tv.sort(key=lambda e: e["rank"])
        age = (date.today() - datetime.strptime(latest_week, "%Y-%m-%d").date()).days
        return {"week": latest_week, "age_days": age, "films": films, "tv": tv}
    except Exception as e:
        print(f"넷플릭스 Top10 커넥터 실패: {type(e).__name__}: {e}")
        return None


def official_sources(keyword: str) -> list[dict]:
    """키워드가 공식 데이터로 커버되는 주제면 리서치 소스 형식으로 돌려준다.

    writer.gather_research가 이 결과를 소스 목록 맨 앞에 붙인다 — [1]이 공식 데이터.
    """
    kw = keyword.lower()
    if "넷플릭스" in keyword and any(w in kw for w in ("순위", "top", "인기", "화제")):
        data = netflix_top10_kr()
        if not data:
            return []
        lines = [f"주간 집계 기준 주: {data['week']} (매주 화요일 갱신)"]
        lines.append("[시리즈 TOP10] " + " / ".join(
            f"{e['rank']}위 {e['title']}({e['weeks_in_top10']}주째)" for e in data["tv"]))
        lines.append("[영화 TOP10] " + " / ".join(
            f"{e['rank']}위 {e['title']}({e['weeks_in_top10']}주째)" for e in data["films"]))
        lines.append("※ 제목은 넷플릭스 공식 영문 표기 — 글에는 다른 소스에서 확인되는 "
                     "국내 통용 한국어 제목을 우선 쓰고, 불확실하면 영문을 병기하라")
        return [{
            "kind": "official",
            "title": f"넷플릭스 공식 주간 Top 10 — 한국 ({data['week']} 주)",
            "snippet": " | ".join(lines),
            "url": NETFLIX_PAGE_URL,
            "date": data["week"],
            "age_days": data["age_days"],
        }]
    return []
