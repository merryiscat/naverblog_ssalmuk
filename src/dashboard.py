"""운영 대시보드 — 관리 블로그별 통계·파이프라인 시각화 (2026-09-04 재설계, 로드맵 #1).

구조: 좌측 사이드바에서 관리 블로그를 고르고, 선택한 블로그의 ①통계 그래프 ②파이프라인
구성(발굴→작문→게이트→발행→측정→보정 흐름 + 오늘자 단계별 수치)을 본다. 멀티블로그(#2)를
대비해 블로그를 목록으로 다룬다 — 지금은 정책(네이버)만 라이브, 법률(티스토리)은 준비중 자리.

외부 라이브러리·CDN·이모지 없이 순수 파이썬 HTML+인라인 CSS+SVG. 요청마다 DB 라이브 조회.
"""

import json
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from src import config, db

# 블로그 레지스트리 — data/blogs.json에서 읽는다(추가·제거·이름 변경은 이 파일로, 코드 수정 X).
# 멀티블로그(#2)의 프로필도 이 레지스트리를 공유 근거로 쓴다. 파일 없으면 기본값.
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


def _sparkline(vals, w=280, h=52, color="#2a6") -> str:
    pts = [(i, v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
    if len(pts) < 2:
        return '<span class="muted">추세 데이터 부족</span>'
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    xr = (max(xs) - min(xs)) or 1
    yr = (max(ys) - min(ys)) or 1
    coords = " ".join(
        f"{4 + (x - min(xs)) / xr * (w - 8):.1f},{h - 6 - (y - min(ys)) / yr * (h - 12):.1f}"
        for x, y in pts)
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linejoin="round" points="{coords}"/></svg>'
            f'<span class="spark-last">{_n(ys[-1])}</span>')


def _card(label, value, sub=""):
    return (f'<div class="card"><div class="clabel">{label}</div>'
            f'<div class="cval">{value}</div><div class="csub">{sub}</div></div>')


# --- 통계 뷰 ---------------------------------------------------------------

def _stats_html(conn: sqlite3.Connection) -> str:
    rows = list(conn.execute(
        "SELECT date, citations, visitors, details_json FROM metrics "
        "ORDER BY date DESC LIMIT 14"))
    rows.reverse()
    cites = [r["citations"] for r in rows]
    visits = [r["visitors"] for r in rows]
    latest = rows[-1] if rows else None
    det = json.loads(latest["details_json"]) if latest and latest["details_json"] else {}
    cit = det.get("citations") or {}
    inflow = det.get("inflow") or {}
    ranks = det.get("ranks") or []

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

    n = len(ranks)
    n_idx = sum(1 for r in ranks if r.get("indexed"))
    n_br = sum(1 for r in ranks if r.get("briefing"))
    n_ci = sum(1 for r in ranks if r.get("cited") or r.get("ai_cited"))

    inflow_rows = "".join(f"<li>{q.get('query','')}</li>"
                          for q in (inflow.get("queries") or [])[:10]) or "<li class='muted'>없음</li>"
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
  <section><h2>누적 인용 추이 (14일)</h2><div class="chart">{_sparkline(cites, color="#2a6")}</div></section>
  <section><h2>방문자 추이 (14일)</h2><div class="chart">{_sparkline(visits, color="#37c")}</div></section>
</div>
<section><h2>AI 검색 검토 — 색인 {n_idx}/{n} · 브리핑 노출 {n_br} · 우리글 인용 {n_ci}</h2></section>
<div class="two">
  <section><h2>유입 검색어 Top</h2><ul>{inflow_rows}</ul></section>
  <section><h2>최근 발행 글</h2><table>{recent_rows}</table></section>
</div>"""


# --- 파이프라인 뷰 ---------------------------------------------------------

def _stage(name, sched, lines):
    body = "".join(f"<div>{ln}</div>" for ln in lines)
    return (f'<div class="stage"><div class="stage-h">{name}<span>{sched}</span></div>'
            f'<div class="stage-b">{body}</div></div>')


def _pipeline_html(conn: sqlite3.Connection) -> str:
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
    dec = conn.execute(
        "SELECT date, rationale FROM decisions ORDER BY id DESC LIMIT 1").fetchone()

    stages = (
        _stage("C1 주제 발굴", "04:00", [
            f'선정 <b>{tp.get("selected",0)}</b>',
            f'예비 {tp.get("reserve",0)} · 후보 {tp.get("candidate",0)}']) +
        _arrow() +
        _stage("C2 작문 · 게이트", "08:30", [
            f'게이트 통과 <b>{po.get("gated",0)}</b>',
            f'탈락/스킵 {po.get("skipped",0)}']) +
        _arrow() +
        _stage("C3 발행", "11–21", [
            f'오늘 발행 <b>{pub_today}</b>',
            f'대기(gated) {po.get("gated",0)}']) +
        _arrow() +
        _stage("C4 측정", "21:30", [
            f'인용 <b>{_n(cit.get("cumulative","-"))}</b>',
            f'방문 {_n(m["visitors"] if m else "-")}']) +
        _arrow() +
        _stage("C5 보정", "22:30", [
            f'{(dec["rationale"][:40] + "…") if dec and dec["rationale"] else "대기"}']))

    agents = (
        _stage("블로그 검수", "화·목·토 23:15", ["화면 검수 → 오케스트레이터"]) +
        _stage("오케스트레이터", "검수 직후", ["교정·예방·수동 지시"]) +
        _stage("메이트 관찰", "일 16:00", ["선정자 블로그 분석"]))

    return f"""
<section><h2>일일 파이프라인 (오늘 {today})</h2>
  <div class="flow">{stages}</div>
</section>
<section><h2>에이전트 (주기 실행)</h2>
  <div class="flow agents">{agents}</div>
</section>
<section><h2>구성 메모</h2>
  <ul class="muted">
    <li>발굴은 정책 시드 + 레퍼런스(정부 K-공감) + 다음달 예측 → 롱테일 확장 → 브리핑 게이트 → near-dup 하드컷(콘텐츠 메모리)</li>
    <li>발행은 5축 카테고리 자동 라우팅 · 출처 하이퍼링크 · 대표 이미지 비전 게이트</li>
    <li>측정은 프로필 인용수 위젯 · 방문자 API · 크리에이터 어드바이저 유입검색어</li>
  </ul>
</section>"""


def _arrow():
    return '<div class="arrow">→</div>'


# --- 페이지 조립 -----------------------------------------------------------

def render_page(blog_key: str = "policy", view: str = "stats") -> str:
    blogs = _load_blogs()
    blog = next((b for b in blogs if b["key"] == blog_key), blogs[0])

    side = "".join(
        f'<a class="blog {"active" if b["key"]==blog["key"] else ""} '
        f'{"" if b.get("active", True) else "off"}" href="?blog={b["key"]}">'
        f'<span class="name">{b["name"]}</span>'
        f'<span class="plat">{b["platform"]}</span></a>'
        for b in blogs)

    if not blog.get("active", True):
        main = ('<div class="placeholder"><h1>' + blog["name"] + '</h1>'
                '<p class="muted">' + blog["platform"] + ' — 로드맵 #2에서 구축 예정.<br>'
                '티스토리 법률 블로그(애드센스·구글 트래픽). 콘텐츠 코어는 정책 블로그와 공유, '
                '발행·측정은 티스토리·구글용으로 신규.</p></div>')
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
<meta http-equiv="refresh" content="300">
<title>운영 대시보드</title>
<style>
 :root {{ --bg:#f5f6f8; --panel:#fff; --fg:#1b2024; --muted:#7a838c; --line:#e5e8eb; --accent:#2a6; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; font-family:-apple-system,'Segoe UI','Malgun Gothic',sans-serif;
        background:var(--bg); color:var(--fg); font-size:13.5px; line-height:1.5; }}
 .app {{ display:flex; min-height:100vh; }}
 .sidebar {{ width:216px; background:#1e242b; color:#cfd6dd; padding:16px 12px; flex-shrink:0; }}
 .sidebar .brand {{ font-weight:700; color:#fff; font-size:14px; padding:4px 8px 14px; }}
 a.blog {{ display:block; padding:10px 12px; border-radius:8px; color:#cfd6dd;
          text-decoration:none; margin-bottom:4px; }}
 a.blog:hover {{ background:#2a323b; }}
 a.blog.active {{ background:var(--accent); color:#fff; }}
 a.blog.off {{ opacity:.55; }}
 a.blog .name {{ display:block; font-weight:600; }}
 a.blog .plat {{ display:block; font-size:11px; opacity:.8; }}
 main {{ flex:1; padding:22px 26px 60px; max-width:1000px; }}
 .head {{ display:flex; align-items:center; justify-content:space-between;
         flex-wrap:wrap; gap:10px; margin-bottom:14px; }}
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
 .chart {{ display:flex; align-items:center; }}
 .spark-last {{ font-size:22px; font-weight:700; margin-left:12px; font-variant-numeric:tabular-nums; }}
 .muted {{ color:var(--muted); }}
 ul {{ margin:0; padding-left:18px; }} li {{ margin:2px 0; }}
 table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
 td {{ padding:4px 8px 4px 0; border-bottom:1px solid var(--line); }}
 td.cited {{ color:var(--accent); font-weight:700; }}
 .flow {{ display:flex; align-items:stretch; gap:0; flex-wrap:wrap; }}
 .flow.agents {{ gap:10px; }}
 .stage {{ background:#f8fafb; border:1px solid var(--line); border-radius:9px;
          padding:8px 12px; min-width:130px; }}
 .stage-h {{ font-weight:700; font-size:12px; display:flex; justify-content:space-between; gap:8px; }}
 .stage-h span {{ color:var(--muted); font-weight:500; font-size:10.5px; }}
 .stage-b {{ margin-top:4px; font-size:12px; color:#333; }}
 .stage-b b {{ font-size:15px; color:var(--accent); }}
 .arrow {{ display:flex; align-items:center; color:var(--muted); padding:0 6px; font-size:16px; }}
 .placeholder {{ padding:40px 10px; }}
 .placeholder h1 {{ margin-bottom:8px; }}
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
        blog = q.get("blog", ["policy"])[0]
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
