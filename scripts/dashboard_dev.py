"""로컬 개발용 대시보드 러너 (2026-09-05).

운영(실매매 미니PC)에 얹지 않고, 이 PC에서 서버 DB만 당겨 localhost로 본다.
여기서 대시보드를 완성한 뒤 운영에 배포한다 (사용자 방침: 개발에서 완벽히 → 운영 배포).

실행:  uv run python scripts/dashboard_dev.py
보기:  브라우저에서 http://localhost:8765/   (터널·방화벽 불필요 — 이 PC에서 도는 것)
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows 콘솔(cp949)에서 한글·특수문자 print가 깨지지 않게 utf-8로
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# scripts/에서 실행돼도 프로젝트 루트의 src를 임포트할 수 있게
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config  # noqa: E402

# 서버에서 당길 파일 (읽기 전용 스냅샷)
SERVER = "merryiscat@192.168.50.205"
REMOTE = "naverblog_ssalmuk/data"  # 서버 홈 기준
CACHE = Path(tempfile.gettempdir()) / "naverblog_dash"
CACHE.mkdir(exist_ok=True)

# config가 로컬 캐시를 보도록 재바인딩 (db.connect·_load_blogs가 호출 시점에 읽음)
config.DATA_DIR = CACHE
config.DB_PATH = CACHE / "naverblog.db"

_last = [0.0]


def pull(force: bool = False) -> None:
    """서버 DB·레지스트리를 로컬 캐시로 scp (20초 캐시 — 매 요청 scp 방지)."""
    if not force and time.time() - _last[0] < 20:
        return
    for f in ("naverblog.db", "blogs.json"):
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes",
                        f"{SERVER}:{REMOTE}/{f}", str(CACHE / f)], check=False)
    _last[0] = time.time()


pull(force=True)  # 최초 1회 (없으면 대시보드가 빈 화면)

# config 재바인딩 후에 import — 요청마다 최신 스냅샷을 당기도록 핸들러를 감싼다
from src import dashboard  # noqa: E402

_orig_get = dashboard._Handler.do_GET


def _do_GET(self):
    pull()
    _orig_get(self)


dashboard._Handler.do_GET = _do_GET

if __name__ == "__main__":
    print(f"로컬 개발 대시보드 — 서버 DB 캐시: {CACHE}")
    print("브라우저에서 http://localhost:8765/ (이 PC에서 돎, 운영 무관)")
    dashboard.serve(8765)
