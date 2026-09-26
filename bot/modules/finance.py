"""Finance module for Telegram bot — exchange rates, currency conversion, and crypto prices."""

import httpx

# ── Symbol mappings for common cryptocurrencies ──────────────────────────────

CRYPTO_ID_MAP: dict[str, str] = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "xrp": "ripple",
    "ripple": "ripple",
    "doge": "dogecoin",
    "dogecoin": "dogecoin",
}


# ── Exchange rate lookup ─────────────────────────────────────────────────────

async def get_exchange_rate(base: str, target: str) -> str:
    """Return a formatted string showing the current exchange rate from *base* to *target*.

    Parameters
    ----------
    base : str
        ISO 4217 currency code for the base currency (e.g. ``"USD"``).
    target : str
        ISO 4217 currency code for the target currency (e.g. ``"KRW"``).

    Returns
    -------
    str
        A Telegram-friendly message with emoji formatting.
    """
    base = base.upper()
    target = target.upper()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://open.er-api.com/v6/latest/{base}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError:
        return f"⚠️ 환율 정보를 가져오는 중 오류가 발생했습니다. (HTTP {resp.status_code})"
    except httpx.RequestError:
        return "⚠️ 환율 API에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요."
    except Exception:
        return "⚠️ 환율 조회 중 알 수 없는 오류가 발생했습니다."

    if data.get("result") == "error":
        reason = data.get("error-type", "알 수 없는 오류")
        return f"⚠️ 환율 조회 실패: {reason}"

    rates = data.get("rates", {})
    if target not in rates:
        return f"⚠️ 통화 코드 **{target}** 를 찾을 수 없습니다. 올바른 ISO 4217 코드를 입력해 주세요."

    rate = rates[target]
    update_time = data.get("time_last_update_utc", "알 수 없음")

    return (
        f"💱 환율 정보\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏳️ 기준 통화: {base}\n"
        f"🎯 대상 통화: {target}\n"
        f"📊 환율: 1 {base} = {rate:,.4f} {target}\n"
        f"🕐 업데이트: {update_time}"
    )


# ── Currency conversion ─────────────────────────────────────────────────────

async def convert_currency(amount: float, base: str, target: str) -> str:
    """Convert *amount* from *base* currency to *target* currency and return a formatted string.

    Parameters
    ----------
    amount : float
        The amount to convert.
    base : str
        ISO 4217 currency code of the source currency.
    target : str
        ISO 4217 currency code of the target currency.

    Returns
    -------
    str
        A Telegram-friendly message with the conversion result.
    """
    base = base.upper()
    target = target.upper()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://open.er-api.com/v6/latest/{base}")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError:
        return f"⚠️ 환율 정보를 가져오는 중 오류가 발생했습니다. (HTTP {resp.status_code})"
    except httpx.RequestError:
        return "⚠️ 환율 API에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요."
    except Exception:
        return "⚠️ 환전 계산 중 알 수 없는 오류가 발생했습니다."

    if data.get("result") == "error":
        reason = data.get("error-type", "알 수 없는 오류")
        return f"⚠️ 환율 조회 실패: {reason}"

    rates = data.get("rates", {})
    if target not in rates:
        return f"⚠️ 통화 코드 **{target}** 를 찾을 수 없습니다. 올바른 ISO 4217 코드를 입력해 주세요."

    rate = rates[target]
    converted = amount * rate

    return (
        f"💰 환전 계산 결과\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📥 금액: {amount:,.2f} {base}\n"
        f"📊 환율: 1 {base} = {rate:,.4f} {target}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📤 결과: {converted:,.2f} {target}"
    )


# ── Cryptocurrency price lookup ─────────────────────────────────────────────

async def get_crypto_price(symbol: str, currency: str = "usd") -> str:
    """Return a formatted string with the current price of a cryptocurrency.

    Parameters
    ----------
    symbol : str
        Ticker symbol or full name (e.g. ``"BTC"``, ``"bitcoin"``).
    currency : str
        Fiat currency to quote the price in (default ``"usd"``).

    Returns
    -------
    str
        A Telegram-friendly message with the crypto price.
    """
    coin_id = CRYPTO_ID_MAP.get(symbol.lower())
    if coin_id is None:
        available = ", ".join(sorted({v for v in CRYPTO_ID_MAP.values()}))
        return (
            f"⚠️ 지원하지 않는 코인 심볼입니다: **{symbol}**\n"
            f"지원 코인: {available}"
        )

    currency = currency.lower()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": currency},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError:
        return f"⚠️ 코인 가격을 가져오는 중 오류가 발생했습니다. (HTTP {resp.status_code})"
    except httpx.RequestError:
        return "⚠️ CoinGecko API에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요."
    except Exception:
        return "⚠️ 코인 가격 조회 중 알 수 없는 오류가 발생했습니다."

    if coin_id not in data:
        return f"⚠️ **{coin_id}** 의 가격 정보를 찾을 수 없습니다."

    price_info = data[coin_id]
    if currency not in price_info:
        return f"⚠️ 통화 **{currency.upper()}** 에 대한 가격 정보가 없습니다."

    price = price_info[currency]

    return (
        f"🪙 암호화폐 시세\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📛 코인: {coin_id.upper()}\n"
        f"💵 가격: {price:,.2f} {currency.upper()}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📡 출처: CoinGecko"
    )