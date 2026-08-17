# 배포 — 미니PC (kei-ubuntu-server, Ubuntu 24.04)

> 대상: 192.168.50.205 (Intel N100 2vCPU / 4GB RAM). Odin·tokbiseo와 **동거**하므로
> 자원 예의가 설계에 포함돼 있다 (systemd Nice=10, MemoryMax=1.2G, 짧은 버스트 작업만).

## 자원 동거 분석

- 이 파이프라인의 무거운 순간은 headless chromium이 뜨는 몇 분뿐이다
  (발행 11~21시 창에서 1~2회, 측정 21시반 1회 — 회당 ~400MB, 수 분).
- 현재 서버 RAM 1.9GB 사용 / 4GB — 버스트 여유 있음. MemoryMax=1.2G로 폭주 시
  우리 쪽이 먼저 죽게 해 Odin(자동매매)을 보호한다.
- 발행 창(11~21시)이 주식 장중(09~15:30)과 겹치지만 위 한도로 충분히 안전.

## 배포 절차 (서버에서 실행)

```bash
# 1. 프로젝트 전송 — 개발 PC에서 (repo에 원격이 없으므로 scp. .venv 제외)
#    (Windows 개발 PC에서) scp로 통째 전송:
#    scp -r C:\Users\minhy\project\naverblog_ssalmuk <user>@192.168.50.205:~/
#    ※ .env 와 data/session/storage_state.json 포함돼야 함 (git에는 없음)

# 2. uv 설치 (서버에 없으면)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 의존성 + 브라우저 (시스템 라이브러리 포함)
cd ~/naverblog_ssalmuk
uv sync
uv run playwright install chromium --with-deps   # sudo 비밀번호 필요할 수 있음

# 4. 동작 확인 (실호출 검증 5종 + 스케줄 드라이런)
uv run python scripts/verify_keys.py
uv run python -m src.scheduler --dry

# 5. systemd 등록 (deploy/naverblog.service의 CHANGEME를 계정명으로 바꾼 뒤)
sed -i 's/\r$//' deploy/naverblog.service   # Windows에서 scp된 CRLF 제거 (안 하면 유닛 파싱 실패)
sudo cp deploy/naverblog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now naverblog

# 6. 확인
systemctl status naverblog          # active (running)
journalctl -u naverblog -f          # 로그 팔로우
# 텔레그램에 "🟢 파이프라인 상주 시작" 도착하면 성공
```

## 운영 메모

- 세션 갱신(30일 주기): 위저드 재로그인은 **개발 PC(화면 있는 곳)** 에서 하고
  `data/session/storage_state.json`만 서버로 다시 scp 한다. 만료 7일 전 텔레그램 소환이 온다.
- 코드 업데이트: 개발 PC에서 수정·커밋 후 scp(또는 서버에 git 원격 추가) → `systemctl restart naverblog`
- 중지: `sudo systemctl stop naverblog` (가드레일과 별개로 사람의 최종 스위치)
- Windows용 install_task.ps1은 대상이 Ubuntu로 확정되며 폐기 예정 (scripts/에 잔존)
