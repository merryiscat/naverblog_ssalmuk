"""OpenAI 호출 게이트웨이 — 모든 LLM·이미지 호출은 반드시 이 모듈을 지난다.

이유 (usecases.md 공통 전제 4): 비용을 한 곳에서 적산해야 월 상한(가드레일 ②)이
정확히 작동한다. 다른 모듈이 OpenAI SDK를 직접 부르는 것은 금지.

사용법:
    from src.llm import chat
    text = chat(config.MODEL_JUDGE, "이 주제가 쓸 가치가 있는지 0~1로...", purpose="topic-scoring")
"""

import sqlite3

from openai import OpenAI

from src import config, db, guardrails

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """OpenAI 클라이언트를 한 번만 만들어 재사용한다."""
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _record_cost(
    conn: sqlite3.Connection,
    kind: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    purpose: str,
) -> None:
    """비용 원장(costs 테이블)에 한 줄 기록한다."""
    conn.execute(
        "INSERT INTO costs (kind, model, tokens_in, tokens_out, cost_usd, purpose) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, model, tokens_in, tokens_out, cost_usd, purpose),
    )
    conn.commit()


def chat(model: str, prompt: str, *, purpose: str, system: str | None = None) -> str:
    """텍스트 생성 호출. 호출 전 예산을 검사하고, 호출 후 비용을 적산한다.

    예산 초과 시 GuardrailViolation이 발생한다 — 호출자는 이를 잡아
    파이프라인을 정지하고 텔레그램으로 알려야 한다.
    """
    conn = db.connect()
    try:
        # 호출 "전"에 검사 — 이미 상한을 넘었다면 새 비용을 만들지 않는다
        guardrails.check_monthly_budget(conn)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = _get_client().chat.completions.create(model=model, messages=messages)

        # 사용 토큰 수로 비용 계산 (단가표는 config.PRICES_PER_MTOK)
        tokens_in = resp.usage.prompt_tokens if resp.usage else 0
        tokens_out = resp.usage.completion_tokens if resp.usage else 0
        price_in, price_out = config.PRICES_PER_MTOK.get(model, (5.00, 30.00))
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000

        _record_cost(conn, "text", model, tokens_in, tokens_out, cost, purpose)
        return resp.choices[0].message.content or ""
    finally:
        conn.close()


def chat_tools(model: str, messages: list, tools: list, *, purpose: str,
               tool_choice: str = "auto"):
    """도구(function calling) 지원 호출 — 에이전트 루프용.

    chat()과 달리 대화 이력(messages)을 그대로 받고, assistant 메시지 객체를 돌려준다
    (message.tool_calls가 있으면 호출자가 도구를 실행할 차례).
    비용 적산·예산 검사는 chat()과 동일 — 에이전트도 가드레일 ② 아래에 있다.
    tool_choice="none"이면 모델이 도구를 못 쓰고 최종 답을 내야 한다 (예산 소진 시 사용).
    """
    conn = db.connect()
    try:
        guardrails.check_monthly_budget(conn)
        resp = _get_client().chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice=tool_choice)
        tokens_in = resp.usage.prompt_tokens if resp.usage else 0
        tokens_out = resp.usage.completion_tokens if resp.usage else 0
        price_in, price_out = config.PRICES_PER_MTOK.get(model, (5.00, 30.00))
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        _record_cost(conn, "text", model, tokens_in, tokens_out, cost, purpose)
        return resp.choices[0].message
    finally:
        conn.close()


def chat_vision(model: str, prompt: str, images: list[bytes], *, purpose: str) -> str:
    """이미지(스크린샷)를 보는 호출 — 블로그 검수 에이전트용.

    images는 PNG 바이트 목록. 비용 적산·예산 검사는 chat()과 동일
    (비전 입력 토큰은 usage.prompt_tokens에 합산되어 온다).
    """
    import base64

    conn = db.connect()
    try:
        guardrails.check_monthly_budget(conn)
        content = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(img).decode(),
                "detail": "auto"}})
        resp = _get_client().chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}])
        tokens_in = resp.usage.prompt_tokens if resp.usage else 0
        tokens_out = resp.usage.completion_tokens if resp.usage else 0
        price_in, price_out = config.PRICES_PER_MTOK.get(model, (5.00, 30.00))
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        _record_cost(conn, "text", model, tokens_in, tokens_out, cost, purpose)
        return resp.choices[0].message.content or ""
    finally:
        conn.close()


def generate_image(prompt: str, *, purpose: str, size: str = "1024x1024") -> bytes:
    """이미지 생성 호출. 텍스트와 같은 예산 원장을 쓴다."""
    conn = db.connect()
    try:
        guardrails.check_monthly_budget(conn)

        resp = _get_client().images.generate(
            model=config.MODEL_IMAGE, prompt=prompt, size=size)
        import base64

        image_bytes = base64.b64decode(resp.data[0].b64_json)

        # 이미지 단가는 추정치(config.IMAGE_COST_USD)로 적산 — 구현 시 보정 예정
        _record_cost(conn, "image", config.MODEL_IMAGE, 0, 0, config.IMAGE_COST_USD, purpose)
        return image_bytes
    finally:
        conn.close()
