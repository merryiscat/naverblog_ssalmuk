"""텔레그램 알림 — 일일 리포트, 이상 알림, 소환(사람 개입 요청) 발송.

완전 방치 운영에서 사용자와의 유일한 접점이다. 발송 전용이며,
명령 수신(정지/재개)은 C5 구현 단계에서 추가한다.
"""

import httpx

from src import config

# 텔레그램 sendMessage의 본문 한도 — 초과하면 API가 통째로 거부(400)한다.
# 자르지 않고 줄 단위로 나눠 여러 건 발송한다 (2026-08-22 잘림 보고의 항체).
# 한도는 텔레그램 기준인 UTF-16 code units다 — 이모지는 2로 세므로 이모지가 많으면
# 파이썬 len()(코드포인트)보다 길다. 여유를 두고 4000으로 잡는다.
TELEGRAM_MAX = 4000


def _u16len(s: str) -> int:
    """텔레그램이 세는 단위(UTF-16 code units) 길이 — 이모지는 2로 친다."""
    return len(s.encode("utf-16-le")) // 2


def _split(text: str) -> list[str]:
    """한도 초과 메시지를 줄 경계에서 여러 조각으로 나눈다 (내용 소실 없음).

    길이는 텔레그램 기준(UTF-16 code units)으로 잰다 — 이모지가 많아도 조각이
    4096을 넘어 거부되지 않게 (2026-08-23 이모지 초과 거부 방어).
    """
    if _u16len(text) <= TELEGRAM_MAX:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while _u16len(line) > TELEGRAM_MAX:  # 한 줄이 한도를 넘는 극단 케이스
            cut = TELEGRAM_MAX // 2  # 코드포인트 절반 — UTF-16으로도 한도 안
            parts.append(line[:cut])
            line = line[cut:]
        if cur and _u16len(cur) + 1 + _u16len(line) > TELEGRAM_MAX:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send(text: str, *, silent: bool = False) -> bool:
    """텔레그램 메시지를 보낸다. 성공하면 True. 한도 초과분은 나눠서 연속 발송.

    실패해도 예외를 던지지 않는다 — 알림이 죽어도 파이프라인은 계속 돌아야
    하므로(usecases.md C5 배드 케이스), 호출자는 반환값만 확인하면 된다.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    ok = True
    for chunk in _split(text):
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_notification": silent,
                },
                timeout=15,
            )
            ok = ok and resp.status_code == 200
        except httpx.HTTPError:
            ok = False
    return ok


def summon(reason: str) -> bool:
    """소환 알림 — 시스템이 못 푸는 상황(캡차·기기확인 등)에 사람을 부른다.

    완전 방치의 유일한 예외 지점. 무음 없이 강하게 알린다.
    """
    return send(f"🚨 [소환] 사람 개입이 필요합니다\n\n{reason}")
