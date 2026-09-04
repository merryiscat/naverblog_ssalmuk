"""운영 대시보드 — 2일마다 하는 검토를 '스캔 가능한 한 화면'으로 (2026-09-04, 로드맵 #1).

왜: 텔레그램 일일 리포트가 20~25줄 텍스트 덩어리라 안 읽힌다(사용자 지적). 사용자는 실제로
2일마다 데이터를 검토하는 능동 소비자 → 형태만 시각화하면 본다. 미니PC에서 상시 서빙,
브라우저로 http://<서버IP>:<포트>/ 접속. 요청마다 DB를 읽어 살아있는 현황을 그린다.

외부 라이브러리·CDN 없이 순수 파이썬으로 HTML+인라인 CSS+인라인 SVG를 만든다(오프라인 LAN에서
동작, 새 의존성 0). 이모지는 쓰지 않는다(사용자 원칙) — 강조는 CSS·타이포·색으로만.
"""

import json
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src import config, db


def _n(v) -> str:
    """숫자를 천단위 콤마로. None/문자는 그대로."""
    if isinstance(v, (int, float)):
        return f"{int(v):,}"
    return str(v) if v is not None else "-"


def _sparkline(vals: list, w: int = 260, h: int = 48, color: str = "#2a6") -> str:
    """값 리스트 → 인라인 SVG 꺾은선(추세만 보이면 됨). None은 건너뛴다."""
    pts = [(i, v) for i, v in enumerate(vals) if isinstance(v, (int, float))]
    if len(pts) < 2:
        return '<div class="spark-empty">추세 데이터 부족</div>'
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    yr = (ymax - ymin) or 1
    xr = (xmax - xmin) or 1
    coords = " ".join(
        f"{4 + (x - xmin) / xr * (w - 8):.1f},{h - 4 - (y - ymin) / yr * (h - 8):.1f}"
        for x, y in pts)
    last = ys[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{coords}"/>'
            f'</svg><span class="spark-last">{_n(last)}</span>')


def _card(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="card"><div class="card-label">{label}</div>'
            f'<div class="card-value">{value}</div>'
            f'<div class="card-sub">{sub}</div></div>')


def render_html(conn: sqlite3.Connection) -> str:
    today = date.today().isoformat()

    # 최근 14일 지표 (추세용)
    metric_rows = list(conn.execute(
        "SELECT date, citations, visitors, details_json FROM metrics "
        "ORDER BY date DESC LIMIT 14"))
    metric_rows.reverse()
    cites = [r["citations"] for r in metric_rows]
    visits = [r["visitors"] for r in metric_rows]

    # 최신 metric의 상세 (인용 원문·유입검색어·AI검토)
    latest = metric_rows[-1] if metric_rows else None
    det = json.loads(latest["details_json"]) if latest and latest["details_json"] else {}
    cit = det.get("citations") or {}
    inflow = det.get("inflow") or {}
    ranks = det.get("ranks") or []

    # 발행 현황
    pub_today = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified') "
        "AND date(published_at)=?", (today,)).fetchone()["c"]
    pub_total = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE status IN ('published','verified')").fetchone()["c"]
    gated_today = list(conn.execute(
        "SELECT scheduled_at, title FROM posts WHERE status='gated' "
        "AND date(created_at)=? ORDER BY scheduled_at", (today,)))

    # 이달 비용
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) s FROM costs "
        "WHERE strftime('%Y-%m', ts)=strftime('%Y-%m','now','localtime')").fetchone()["s"]

    # 최근 발행 글 (인용 여부 포함)
    recent = list(conn.execute(
        "SELECT p.id, p.title, t.category, date(p.published_at) d, "
        "(SELECT MAX(ai_cited) FROM rankings r WHERE r.post_id=p.id) cited "
        "FROM posts p LEFT JOIN topics t ON p.topic_id=t.id "
        "WHERE p.status IN ('published','verified') ORDER BY p.published_at DESC LIMIT 10"))

    # 경보 — 수동 큐(gave_up) + 최근 검수 이슈
    manual = list(conn.execute(
        "SELECT issue_text, date FROM resolution_attempts WHERE result='gave_up' "
        "ORDER BY date DESC LIMIT 5"))

    # --- HTML 조립 ---
    cards = "".join([
        _card("누적 인용", _n(cit.get("cumulative", cites[-1] if cites else "-")),
              f"당월 {cit.get('this_month','-')}"),
        _card("방문자 오늘", _n(latest["visitors"] if latest else "-"),
              f"추세 14일"),
        _card("오늘 발행", str(pub_today), f"누적 {pub_total}"),
        _card("이달 비용", f"${cost:.2f}", f"예산 ${config.MONTHLY_BUDGET_USD:.2f}"),
    ])

    n = len(ranks)
    n_idx = sum(1 for r in ranks if r.get("indexed"))
    n_br = sum(1 for r in ranks if r.get("briefing"))
    n_ci = sum(1 for r in ranks if r.get("cited") or r.get("ai_cited"))

    inflow_rows = "".join(
        f'<li>{q.get("query","")}</li>' for q in (inflow.get("queries") or [])[:10]
    ) or "<li>데이터 없음</li>"

    gated_rows = "".join(
        f'<li><span class="t">{(r["scheduled_at"] or "")[11:16]}</span> {r["title"][:44]}</li>'
        for r in gated_today) or "<li>없음</li>"

    recent_rows = "".join(
        f'<tr><td>{r["d"] or ""}</td><td>{(r["category"] or "-")}</td>'
        f'<td>{(r["title"] or "")[:48]}</td>'
        f'<td class="{"cited" if r["cited"] else ""}">{"인용" if r["cited"] else ""}</td></tr>'
        for r in recent)

    manual_rows = "".join(
        f'<li><span class="d">{m["date"]}</span> {(m["issue_text"] or "")[:60]}</li>'
        for m in manual) or "<li>대기 중 없음</li>"

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>운영 대시보드</title>
<style>
  :root {{ --bg:#f6f7f8; --fg:#1c2124; --muted:#6b7680; --line:#e3e6e9; --accent:#2a6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,'Segoe UI',Roboto,'Malgun Gothic',sans-serif;
         background:var(--bg); color:var(--fg); font-size:14px; line-height:1.5; }}
  .wrap {{ max-width:960px; margin:0 auto; padding:20px 16px 60px; }}
  h1 {{ font-size:18px; margin:0 0 2px; }}
  .meta {{ color:var(--muted); font-size:12px; margin-bottom:16px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:20px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
  .card-label {{ color:var(--muted); font-size:11px; letter-spacing:.02em; }}
  .card-value {{ font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .card-sub {{ color:var(--muted); font-size:11px; }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:10px;
            padding:12px 16px; margin-bottom:14px; }}
  h2 {{ font-size:13px; color:var(--muted); margin:0 0 8px; font-weight:600;
       text-transform:none; }}
  .row {{ display:flex; gap:24px; flex-wrap:wrap; align-items:center; }}
  .spark-last {{ font-size:22px; font-weight:700; margin-left:10px;
                 font-variant-numeric:tabular-nums; vertical-align:middle; }}
  .spark-empty {{ color:var(--muted); font-size:12px; }}
  ul {{ margin:0; padding-left:18px; }} li {{ margin:2px 0; }}
  ul.plain {{ list-style:none; padding:0; }}
  .t {{ color:var(--accent); font-variant-numeric:tabular-nums; font-weight:600; }}
  .d {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  td {{ padding:4px 8px 4px 0; border-bottom:1px solid var(--line); }}
  td.cited {{ color:var(--accent); font-weight:700; }}
  .two {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  @media(max-width:640px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} .two {{ grid-template-columns:1fr; }} }}
</style></head><body><div class="wrap">
<h1>정책브리핑 가이드 — 운영 대시보드</h1>
<div class="meta">갱신 {date.today():%Y-%m-%d} · 5분마다 자동 새로고침</div>
<div class="cards">{cards}</div>

<div class="two">
  <section><h2>누적 인용 추이 (14일)</h2><div class="row">{_sparkline(cites, color="#2a6")}</div></section>
  <section><h2>방문자 추이 (14일)</h2><div class="row">{_sparkline(visits, color="#37c")}</div></section>
</div>

<section><h2>AI 검색 검토 — 색인 {n_idx}/{n} · 브리핑 노출 {n_br} · 우리글 인용 {n_ci}</h2></section>

<div class="two">
  <section><h2>유입 검색어 Top</h2><ul>{inflow_rows}</ul></section>
  <section><h2>오늘 발행 예정 (gated)</h2><ul class="plain">{gated_rows}</ul></section>
</div>

<section><h2>최근 발행 글</h2><table>{recent_rows}</table></section>

<section><h2>경보 · 수동 대기 (사람 몫)</h2><ul>{manual_rows}</ul></section>
</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            conn = db.connect()
            try:
                html = render_html(conn)
            finally:
                conn.close()
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, *a):
        pass  # 접속 로그로 journalctl 더럽히지 않는다


def serve(port: int | None = None) -> None:
    """대시보드 HTTP 서버 — 블로킹. 스케줄러는 이걸 데몬 스레드로 띄운다."""
    port = port or config.DASHBOARD_PORT
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"운영 대시보드 서빙: http://0.0.0.0:{port}/")
    srv.serve_forever()
