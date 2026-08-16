"""텔레그램 알림 — 일일 리포트, 이상 알림, 소환(사람 개입 요청) 발송.

완전 방치 운영에서 사용자와의 유일한 접점이다. 발송 전용이며,
명령 수신(정지/재개)은 C5 구현 단계에서 추가한다.
"""

import httpx

from src import config


def send(text: str, *, silent: bool = False) -> bool:
    """텔레그램 메시지를 보낸다. 성공하면 True.

    실패해도 예외를 던지지 않는다 — 알림이 죽어도 파이프라인은 계속 돌아야
    하므로(usecases.md C5 배드 케이스), 호출자는 반환값만 확인하면 된다.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "disable_notification": silent,
            },
            timeout=15,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def summon(reason: str) -> bool:
    """소환 알림 — 시스템이 못 푸는 상황(캡차·기기확인 등)에 사람을 부른다.

    완전 방치의 유일한 예외 지점. 무음 없이 강하게 알린다.
    """
    return send(f"🚨 [소환] 사람 개입이 필요합니다\n\n{reason}")
