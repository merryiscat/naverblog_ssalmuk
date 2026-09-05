"""운영 대시보드 — 전체 현황 + 블로그별 통계·파이프라인 (2026-09-05 재설계, 로드맵 #1).

좌측 사이드바: [전체 현황] + 관리 블로그 목록(레지스트리 data/blogs.json). 전체는 모든 블로그를
한눈에, 각 블로그는 ①통계(카드+추이 차트+유입·최근글) ②파이프라인(주제발굴→작문→게이트→발행→
측정→보정 흐름 + 오늘자 수치 + 발굴 상세: 소스별·일자별).

외부 라이브러리·CDN·이모지 없이 순수 파이썬 HTML+인라인 CSS+SVG. 개발은 로컬(dashboard_dev.py),
완성 후 운영 배포.
"""

import json
import re
import sqlite3
from collections import Counter, OrderedDict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from src import config, db

_DEFAULT_BLOGS = [
    {"key": "policy", "name": "정책브리핑 가이드", "platform": "네이버", "active": True},
    {"key": "legal", "name": "법률 유권해석", "platform": "티스토리 · 준비중", "active": False},
]


def _load_blogs() -> list:
    try:
        path = config.DATA_DIR / "blogs.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass
    return _DEFAULT_BLOGS


def _n(v) -> str:
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    return str(v) if v is not None else "-"


def _chart(vals, labels=None, w=440, h=140, color="#2a6") -> str:
    """추이 차트 — 영역 + 선 + y축 눈금 + 그리드 + 끝점 마커 + x축 날짜(labels)."""
    pts = [(i, v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
    if len(pts) < 2:
        return '<div class="muted nodata">데이터 부족</div>'
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    xr, yr = (xmax - xmin) or 1, (ymax - ymin) or 1
    pl, pr, pt, pb = 42, 10, 12, 22  # 아래 여백 = x축 날짜 자리

    def X(x):
        return pl + (x - xmin) / xr * (w - pl - pr)

    def Y(y):
        return h - pb - (y - ymin) / yr * (h - pt - pb)

    line = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in pts)
    area = f"{X(xs[0]):.1f},{h - pb:.1f} {line} {X(xs[-1]):.1f},{h - pb:.1f}"
    grid = ""
    for frac in (0, 0.5, 1):
        val = ymin + frac * yr
        y = Y(val)
        grid += (f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="#eef1f3"/>'
                 f'<text x="{pl - 6}" y="{y + 3:.1f}" text-anchor="end" '
                 f'font-size="9.5" fill="#a3acb4">{_n(int(round(val)))}</text>')
    # x축 날짜 — 첫·중간·끝 점 (labels는 vals와 같은 인덱스)
    xax = ""
    if labels:
        mid = pts[len(pts) // 2][0]
        for i, anc in ((xs[0], "start"), (mid, "middle"), (xs[-1], "end")):
            if 0 <= i < len(labels) and labels[i]:
                xax += (f'<text x="{X(i):.1f}" y="{h - 6:.1f}" text-anchor="{anc}" '
                        f'font-size="9.5" fill="#a3acb4">{labels[i]}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="chartsvg">{grid}'
            f'<polygon points="{area}" fill="{color}" opacity="0.10"/>'
            f'<polyline points="{line}" fill="none" stroke="{color}" '
            f'stroke-width="2.5" stroke-linejoin="round"/>'
            f'<circle cx="{X(xs[-1]):.1f}" cy="{Y(ys[-1]):.1f}" r="3.5" fill="{color}"/>{xax}</svg>')


def _deltas(vals) -> list:
    """누적 시계열 → 일별 증가량. 앞뒤 값이 다 숫자일 때만 차이, 아니면 None."""
    out, prev = [], None
    for v in vals:
        if isinstance(v, (int, float)) and isinstance(prev, (int, float)):
            out.append(v - prev)
        else:
            out.append(None)
        if isinstance(v, (int, float)):
            prev = v
    return out


def _bars(vals, labels=None, w=440, h=140, color="#2a6") -> str:
    """일별 값(성장 속도) 막대 차트 — 0 기준선 + y눈금 + x날짜. 값 없는 날은 건너뜀."""
    idx = [(i, v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
    if not idx:
        return '<div class="muted nodata">데이터 부족</div>'
    ys = [v for _, v in idx]
    ymax = max(ys + [0])
    ymin = min(ys + [0])
    yr = (ymax - ymin) or 1
    pl, pr, pt, pb = 42, 10, 12, 22
    nslots = len(vals)
    slotw = (w - pl - pr) / max(nslots, 1)
    bw = slotw * 0.6

    def X(i):
        return pl + (i + 0.5) * slotw

    def Y(v):
        return h - pb - (v - ymin) / yr * (h - pt - pb)

    y0 = Y(0)
    bars = "".join(
        f'<rect x="{X(i) - bw / 2:.1f}" y="{min(Y(v), y0):.1f}" width="{bw:.1f}" '
        f'height="{max(abs(Y(v) - y0), 0.5):.1f}" fill="{color}" rx="1.5"/>'
        for i, v in idx)
    grid = ""
    for val in (ymin, (ymin + ymax) / 2 if ymin < 0 else ymax / 2, ymax):
        y = Y(val)
        grid += (f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" stroke="#eef1f3"/>'
                 f'<text x="{pl - 6}" y="{y + 3:.1f}" text-anchor="end" '
                 f'font-size="9.5" fill="#a3acb4">{_n(int(round(val)))}</text>')
    # x축 날짜 — 막대 중앙(X(i))에 middle 정렬로 균등 배치(약 5개). 어느 막대=어느 날 명확히.
    xax = ""
    if labels:
        step = max(1, -(-len(vals) // 5))
        for i in range(0, len(vals), step):
            if i < len(labels) and labels[i]:
                xax += (f'<text x="{X(i):.1f}" y="{h - 6:.1f}" text-anchor="middle" '
                        f'font-size="9" fill="#a3acb4">{labels[i]}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="chartsvg">{grid}'
            f'{bars}{xax}</svg>')


def _card(label, value, sub=""):
    return (f'<div class="card"><div class="clabel">{label}</div>'
            f'<div class="cval">{value}</div><div class="csub">{sub}</div></div>')


# --- 데이터 헬퍼 -----------------------------------------------------------

def _metrics(conn, n=14):
    rows = list(conn.execute(
        "SELECT date, citations, visitors, details_json FROM metrics "
        "ORDER BY date DESC LIMIT ?", (n,)))
    rows.reverse()
    return rows


def _src_of(rationale: str) -> str:
    r = rationale or ""
    if "[레퍼런스]" in r:
        return "레퍼런스"
    if "[다음달예측]" in r:
        return "다음달예측"
    return "시드 롱테일"


# 주제 식별에 도움 안 되는 흔한 토큰 (제목·검색어에 공통으로 껴서 오매칭 유발)
_STOP = {"2026년", "2026", "기준", "총정리", "방법", "정리", "조건", "안내", "및", "상황별",
         "최신", "현황", "가이드", "신청", "핵심", "때", "후기", "무엇", "얼마"}


def _topic_tokens(s):
    return {t for t in re.findall(r"[가-힣a-z0-9]{2,}", (s or "").lower()) if t not in _STOP}


_inflow_cache = {}


def _inflow_token_groups(conn, queries):
    """폴백 — 주제 토큰 겹침으로 유입 검색어를 글에 매핑(API 없이)."""
    posts = [(p["title"], p["category"], _topic_tokens(p["title"]))
             for p in conn.execute(
                 "SELECT p.title, t.category FROM posts p LEFT JOIN topics t ON p.topic_id=t.id "
                 "WHERE p.status IN ('published','verified')")]
    groups = {}
    for q in queries:
        kw = q.get("query", "")
        qt = _topic_tokens(kw)
        best, score = None, 0
        for title, cat, tk in posts:
            s = len(qt & tk)
            if s > score:
                best, score = (title, cat), s
        key = best if (best and score >= 1) else ("(매칭 글 없음)", None)
        groups.setdefault(key, []).append(kw)
    return groups


def _inflow_by_post(conn, queries) -> list:
    """유입 검색어를 '의미가 가장 가까운 발행 글'로 매핑해 글별로 묶는다.

    콘텐츠 메모리(임베딩)로 의미 매칭 — 토큰 정확일치가 놓치는 변형(노란우산↔노란우산공제,
    dc퇴직금↔퇴직연금 중도인출)까지 잡는다. 검색어 집합 단위로 캐시(매 로드 재임베딩 방지).
    임베딩 실패(키 없음 등) 시 토큰 폴백. 반환: [(제목, 분야, [검색어...])] 유입 많은 순 = 인기 주제.
    """
    key = tuple(sorted(q.get("query", "") for q in queries))
    if key in _inflow_cache:
        return _inflow_cache[key]
    groups = None
    try:
        from src import memory
        groups = {}
        for q in queries:
            kw = q.get("query", "")
            hits = memory.retrieve(conn, kw, k=1, min_sim=0.32)
            gk = (hits[0]["title"], hits[0].get("category")) if hits else ("(매칭 글 없음)", None)
            groups.setdefault(gk, []).append(kw)
    except Exception as e:
        print(f"유입 의미매핑 실패 — 토큰 폴백: {type(e).__name__}: {e}")
        groups = _inflow_token_groups(conn, queries)
    result = [(k2[0], k2[1], v) for k2, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]
    _inflow_cache[key] = result
    return result


# --- 통계 뷰 ---------------------------------------------------------------

def _stats_html(conn) -> str:
    rows = _metrics(conn)
    cites = [r["citations"] for r in rows]
    visits = [r["visitors"] for r in rows]
    dates = [(r["date"] or "")[5:] for r in rows]  # MM-DD (x축 라벨)
    latest = rows[-1] if rows else None
    det = json.loads(latest["details_json"]) if latest and latest["details_json"] else {}
    cit = det.get("citations") or {}
    inflow = det.get("inflow") or {}

    pub_total = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified')").fetchone()["c"]
    pub_today = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified') "
        "AND date(published_at)=date('now','localtime')").fetchone()["c"]
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM costs "
        "WHERE strftime('%Y-%m', ts)=strftime('%Y-%m','now','localtime')").fetchone()["s"]

    cards = "".join([
        _card("누적 인용", _n(cit.get("cumulative", cites[-1] if cites else "-")),
              f"당월 {cit.get('this_month','-')}"),
        _card("방문자 오늘", _n(latest["visitors"] if latest else "-"), "14일 추세 아래"),
        _card("오늘 발행", str(pub_today), f"누적 {pub_total}"),
        _card("이달 비용", f"${cost:.2f}", f"예산 ${config.MONTHLY_BUDGET_USD:.2f}"),
    ])
    igroups = _inflow_by_post(conn, (inflow.get("queries") or [])[:15])
    inflow_html = "".join(
        f'<div class="ig"><div class="ig-h">{(title or "")[:46]}'
        f'<span class="ig-n">{len(qs)}건</span></div>'
        f'<div class="ig-q">{" · ".join(qs)}</div></div>'
        for title, cat, qs in igroups) or '<div class="muted">유입 데이터 없음</div>'
    recent = list(conn.execute(
        "SELECT p.title, t.category, date(p.published_at) d, "
        "(SELECT MAX(ai_cited) FROM rankings r WHERE r.post_id=p.id) cited "
        "FROM posts p LEFT JOIN topics t ON p.topic_id=t.id "
        "WHERE p.status IN ('published','verified') ORDER BY p.published_at DESC LIMIT 10"))
    recent_rows = "".join(
        f'<tr><td class="muted">{r["d"] or ""}</td><td>{r["category"] or "-"}</td>'
        f'<td>{(r["title"] or "")[:46]}</td>'
        f'<td class="{"cited" if r["cited"] else ""}">{"인용" if r["cited"] else ""}</td></tr>'
        for r in recent)
    return f"""
<div class="cards">{cards}</div>
<div class="two">
  <section><h2>일일 인용 증가 (14일) — 성장 속도</h2>{_bars(_deltas(cites), dates, color="#2a6")}</section>
  <section><h2>일일 방문자 (14일)</h2>{_bars(visits, dates, color="#3a7bd5")}</section>
</div>
<section><h2>유입 키워드 → 인기 주제 (어느 글이 어떤 검색어로 유입되나 · 많은 순)</h2>
  <div class="igs">{inflow_html}</div></section>
<section><h2>최근 발행 글</h2><table>{recent_rows}</table></section>"""


# --- 파이프라인 뷰 ---------------------------------------------------------

def _stage(name, sched, lines):
    body = "".join(f"<div>{ln}</div>" for ln in lines)
    return (f'<div class="stage"><div class="stage-h">{name}<span>{sched}</span></div>'
            f'<div class="stage-b">{body}</div></div>')


def _pipeline_html(conn) -> str:
    today = date.today().isoformat()
    tp = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM topics WHERE date=? GROUP BY status", (today,))}
    po = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM posts WHERE date(created_at)=? GROUP BY status", (today,))}
    pub_today = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified') "
        "AND date(published_at)=?", (today,)).fetchone()["c"]
    m = conn.execute(
        "SELECT citations, visitors, details_json FROM metrics ORDER BY date DESC LIMIT 1").fetchone()
    cit = (json.loads(m["details_json"]).get("citations") if m and m["details_json"] else {}) or {}
    dec = conn.execute("SELECT rationale FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    tried = po.get("gated", 0) + po.get("skipped", 0)

    flow = (
        _stage("주제 발굴", "04:00", [f'선정 <b>{tp.get("selected",0)}</b>',
                                    f'예비 {tp.get("reserve",0)} · 후보 {tp.get("candidate",0)}']) + _arrow() +
        _stage("작문", "08:30", [f'시도 <b>{tried}</b>건']) + _arrow() +
        _stage("게이트", "08:30", [f'통과 <b>{po.get("gated",0)}</b>', f'탈락 {po.get("skipped",0)}']) + _arrow() +
        _stage("발행", "11–21", [f'완료 <b>{pub_today}</b>', f'대기 {po.get("gated",0)}']) + _arrow() +
        _stage("측정", "21:30", [f'인용 <b>{_n(cit.get("cumulative","-"))}</b>',
                                f'방문 {_n(m["visitors"] if m else "-")}']) + _arrow() +
        _stage("보정", "22:30", [f'{(dec["rationale"][:34] + "…") if dec and dec["rationale"] else "대기"}']))

    agents = (
        _stage("블로그 검수", "화·목·토 23:15", ["화면 검수 → 오케스트레이터"]) +
        _stage("오케스트레이터", "검수 직후", ["교정·예방·수동 지시"]) +
        _stage("메이트 관찰", "일 16:00", ["선정자 블로그 분석"]))

    # 발굴 상세 — 오늘 소스별 + 일자별 선정 내용
    src_today = Counter(_src_of(r["rationale"])
                        for r in conn.execute("SELECT rationale FROM topics WHERE date=?", (today,)))
    src_line = " · ".join(f'<b>{k}</b> {v}' for k, v in src_today.most_common()) or "없음"

    byday = OrderedDict()
    for r in conn.execute(
            "SELECT date, keyword, rationale FROM topics WHERE status='selected' "
            "AND date >= date('now','localtime','-6 days') ORDER BY date DESC, id"):
        byday.setdefault(r["date"], []).append((r["keyword"], _src_of(r["rationale"])))
    day_html = ""
    for d, items in byday.items():
        lis = "".join(f'<li><span class="tag">{s}</span> {k}</li>' for k, s in items)
        day_html += f'<div class="day"><div class="day-d">{d}</div><ul>{lis}</ul></div>'
    if not day_html:
        day_html = '<div class="muted">최근 선정 없음</div>'

    return f"""
<section><h2>일일 파이프라인 (오늘 {today})</h2><div class="flow">{flow}</div></section>
<section><h2>에이전트 (주기 실행)</h2><div class="flow agents">{agents}</div></section>
<section><h2>발굴 상세 — 오늘 소스별: {src_line}</h2>
  <div class="days">{day_html}</div>
</section>"""


def _arrow():
    return '<div class="arrow">→</div>'


# --- 전체 현황 -------------------------------------------------------------

def _overview_html(blogs) -> str:
    cards = ""
    for b in blogs:
        if not b.get("active", True):
            cards += (f'<a class="ocard off" href="?blog={b["key"]}"><div class="oname">{b["name"]}</div>'
                      f'<div class="muted">{b["platform"]}</div>'
                      f'<div class="muted" style="margin-top:8px">준비 중 (로드맵 #2)</div></a>')
            continue
        conn = db.connect()
        try:
            rows = _metrics(conn)
            cites = [r["citations"] for r in rows]
            odates = [(r["date"] or "")[5:] for r in rows]
            latest = rows[-1] if rows else None
            det = json.loads(latest["details_json"]) if latest and latest["details_json"] else {}
            cit = det.get("citations") or {}
            pub_today = conn.execute(
                "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified') "
                "AND date(published_at)=date('now','localtime')").fetchone()["c"]
        finally:
            conn.close()
        cards += (
            f'<a class="ocard" href="?blog={b["key"]}">'
            f'<div class="oname">{b["name"]} <span class="muted">{b["platform"]}</span></div>'
            f'<div class="ostats">'
            f'<span><b>{_n(cit.get("cumulative","-"))}</b> 누적 인용</span>'
            f'<span><b>{_n(latest["visitors"] if latest else "-")}</b> 방문</span>'
            f'<span><b>{pub_today}</b> 오늘 발행</span></div>'
            f'<div class="ochart">{_bars(_deltas(cites), odates, w=380, h=104, color="#2a6")}</div></a>')
    return f'<div class="head"><h1>전체 현황</h1></div><div class="overview">{cards}</div>'


# --- 페이지 조립 -----------------------------------------------------------

def render_page(blog_key: str = "policy", view: str = "stats") -> str:
    blogs = _load_blogs()
    is_all = blog_key == "all"
    blog = next((b for b in blogs if b["key"] == blog_key), blogs[0])

    side = (f'<a class="blog nav {"active" if is_all else ""}" href="?blog=all">'
            f'<span class="name">전체 현황</span>'
            f'<span class="plat">모든 블로그</span></a><div class="sep"></div>')
    side += "".join(
        f'<a class="blog {"active" if (not is_all and b["key"]==blog["key"]) else ""} '
        f'{"" if b.get("active", True) else "off"}" href="?blog={b["key"]}">'
        f'<span class="name">{b["name"]}</span>'
        f'<span class="plat">{b["platform"]}</span></a>'
        for b in blogs)

    if is_all:
        main = _overview_html(blogs)
    elif not blog.get("active", True):
        main = (f'<div class="head"><h1>{blog["name"]} '
                f'<span class="plat">{blog["platform"]}</span></h1></div>'
                f'<section><p class="muted">{blog["platform"]} — 로드맵 #2에서 구축 예정.<br>'
                f'티스토리 법률 블로그(애드센스·구글 트래픽). 콘텐츠 코어는 정책 블로그와 공유, '
                f'발행·측정은 티스토리·구글용으로 신규.</p></section>')
    else:
        conn = db.connect()
        try:
            content = _pipeline_html(conn) if view == "pipeline" else _stats_html(conn)
        finally:
            conn.close()
        tabs = "".join(
            f'<a class="tab {"active" if view==v else ""}" '
            f'href="?blog={blog["key"]}&view={v}">{label}</a>'
            for v, label in (("stats", "통계"), ("pipeline", "파이프라인")))
        main = (f'<div class="head"><h1>{blog["name"]} '
                f'<span class="plat">{blog["platform"]}</span></h1>'
                f'<div class="tabs">{tabs}</div></div>{content}')

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300"><title>운영 대시보드</title>
<style>
 :root {{ --bg:#f5f6f8; --panel:#fff; --fg:#1b2024; --muted:#7a838c; --line:#e5e8eb; --accent:#2a6; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; font-family:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;
        background:var(--bg); color:var(--fg); font-size:13.5px; line-height:1.5; }}
 .app {{ display:flex; min-height:100vh; }}
 .sidebar {{ width:216px; background:#1e242b; color:#cfd6dd; padding:16px 12px; flex-shrink:0; }}
 .sidebar .brand {{ font-weight:700; color:#fff; font-size:14px; padding:4px 8px 14px; }}
 .sidebar .sep {{ height:1px; background:#333c45; margin:8px 4px; }}
 a.blog {{ display:block; padding:9px 12px; border-radius:8px; color:#cfd6dd;
          text-decoration:none; margin-bottom:4px; }}
 a.blog:hover {{ background:#2a323b; }}
 a.blog.active {{ background:var(--accent); color:#fff; }}
 a.blog.off {{ opacity:.55; }}
 a.blog.nav .name {{ color:#fff; }}
 a.blog .name {{ display:block; font-weight:600; }}
 a.blog .plat {{ display:block; font-size:11px; opacity:.8; }}
 main {{ flex:1; padding:22px 26px 60px; max-width:1040px; }}
 .head {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap;
         gap:10px; margin-bottom:14px; }}
 h1 {{ font-size:19px; margin:0; }}
 h1 .plat, .head .plat {{ font-size:12px; color:var(--muted); font-weight:500; margin-left:6px; }}
 .tabs {{ display:flex; gap:6px; }}
 .tab {{ padding:6px 14px; border-radius:20px; text-decoration:none; color:var(--muted);
        background:var(--panel); border:1px solid var(--line); font-weight:600; }}
 .tab.active {{ background:var(--fg); color:#fff; border-color:var(--fg); }}
 .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:16px; }}
 .card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
 .clabel {{ color:var(--muted); font-size:11px; }}
 .cval {{ font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }}
 .csub {{ color:var(--muted); font-size:11px; }}
 section {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:12px 16px; margin-bottom:12px; }}
 h2 {{ font-size:12.5px; color:var(--muted); margin:0 0 8px; font-weight:600; }}
 .two {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
 .chartsvg {{ width:100%; height:auto; display:block; }}
 .nodata {{ padding:24px 0; text-align:center; }}
 .muted {{ color:var(--muted); }}
 ul {{ margin:0; padding-left:18px; }} li {{ margin:2px 0; }}
 table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
 td {{ padding:4px 8px 4px 0; border-bottom:1px solid var(--line); }}
 td.cited {{ color:var(--accent); font-weight:700; }}
 .flow {{ display:flex; align-items:stretch; flex-wrap:wrap; }}
 .flow.agents {{ gap:10px; }}
 .stage {{ background:#f8fafb; border:1px solid var(--line); border-radius:9px; padding:8px 12px; min-width:128px; }}
 .stage-h {{ font-weight:700; font-size:12.5px; display:flex; justify-content:space-between; gap:8px; }}
 .stage-h span {{ color:var(--muted); font-weight:500; font-size:10.5px; }}
 .stage-b {{ margin-top:4px; font-size:12px; color:#333; }}
 .stage-b b {{ font-size:15px; color:var(--accent); }}
 .arrow {{ display:flex; align-items:center; color:var(--muted); padding:0 6px; font-size:16px; }}
 .days {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }}
 .day {{ border:1px solid var(--line); border-radius:8px; padding:8px 10px; }}
 .day-d {{ font-weight:700; font-size:12px; color:#444; margin-bottom:4px; }}
 .day ul {{ padding-left:0; list-style:none; }}
 .day li {{ font-size:12px; margin:3px 0; }}
 .tag {{ font-size:10px; background:#eaf3ee; color:#2a6; border-radius:4px; padding:1px 5px; margin-right:4px; }}
 .igs {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:10px; }}
 .ig {{ border:1px solid var(--line); border-radius:8px; padding:8px 11px; }}
 .ig-h {{ font-weight:700; font-size:12.5px; display:flex; justify-content:space-between;
         gap:8px; align-items:baseline; }}
 .ig-n {{ color:var(--accent); font-weight:700; font-size:12px; white-space:nowrap; }}
 .ig-q {{ color:var(--muted); font-size:11.5px; margin-top:3px; line-height:1.55; }}
 .overview {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:12px; }}
 a.ocard {{ display:block; background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:14px 16px; text-decoration:none; color:var(--fg); }}
 a.ocard:hover {{ border-color:var(--accent); }}
 a.ocard.off {{ opacity:.6; }}
 .oname {{ font-weight:700; font-size:15px; margin-bottom:8px; }}
 .ostats {{ display:flex; gap:18px; margin-bottom:6px; }}
 .ostats b {{ font-size:18px; color:var(--accent); font-variant-numeric:tabular-nums; }}
 .ostats span {{ font-size:11px; color:var(--muted); }}
 @media(max-width:720px){{ .cards{{grid-template-columns:repeat(2,1fr);}} .two{{grid-template-columns:1fr;}}
   .sidebar{{width:150px;}} }}
</style></head><body><div class="app">
<aside class="sidebar"><div class="brand">운영 대시보드</div>{side}</aside>
<main>{main}</main>
</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        q = parse_qs(u.query)
        blog = q.get("blog", ["all"])[0]
        view = q.get("view", ["stats"])[0]
        try:
            body = render_page(blog, view).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, *a):
        pass


def serve(port: int | None = None) -> None:
    port = port or config.DASHBOARD_PORT
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"운영 대시보드 서빙: http://0.0.0.0:{port}/")
    srv.serve_forever()
