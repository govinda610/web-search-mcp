"""Live numbers from keyless APIs: stock quotes (Yahoo Finance, NSE/BSE included), currency
rates (Frankfurter, ECB data), crypto prices (CoinGecko) and weather (Open-Meteo)."""
import re
import time
from urllib.parse import quote

from media import http

WEATHER_CODES = {0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
                 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
                 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
                 81: "rain showers", 82: "violent rain showers", 95: "thunderstorm", 96: "thunderstorm, hail",
                 99: "thunderstorm, heavy hail"}


async def stock(query: str) -> str:
    """A ticker ("RELIANCE.NS", "AAPL") or a company name, which is looked up first."""
    symbol = query.strip()
    if not re.fullmatch(r"[A-Z0-9^.=-]{1,15}", symbol):
        found = (await http(f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(query)}"
                            "&quotesCount=5&newsCount=0")).json().get("quotes", [])
        if not found:
            return f"No stock found for {query!r}."
        symbol = found[0]["symbol"]
    data = (await http(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"))
    meta = data.json()["chart"]["result"][0]["meta"]
    price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose")
    change = f" ({(price - prev) / prev * 100:+.2f}% vs {prev})" if price and prev else ""
    return (f"{meta.get('longName') or meta.get('shortName') or symbol} [{symbol}, {meta.get('fullExchangeName')}]\n"
            f"price: {price} {meta.get('currency')}{change}\n"
            f"day range: {meta.get('regularMarketDayLow')} - {meta.get('regularMarketDayHigh')} | "
            f"52-week: {meta.get('fiftyTwoWeekLow')} - {meta.get('fiftyTwoWeekHigh')}\n"
            f"as of {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(meta.get('regularMarketTime', 0)))} (Yahoo Finance)")


async def currency(query: str) -> str:
    """"USD INR", "100 EUR to USD" or "USD" (against INR, EUR, GBP, JPY)."""
    amount = re.search(r"\d+(?:\.\d+)?", query)
    codes = re.findall(r"\b[A-Za-z]{3}\b", query.replace(" to ", " "))
    codes = [c.upper() for c in codes]
    if not codes:
        return "Give currency codes, e.g. \"USD INR\" or \"100 EUR to USD\"."
    base, targets = codes[0], ",".join(codes[1:]) or "INR,EUR,GBP,JPY,USD"
    data = (await http(f"https://api.frankfurter.dev/v1/latest?base={base}&symbols={targets}")).json()
    n = float(amount.group(0)) if amount else 1.0
    lines = [f"{n:g} {base} = {rate * n:,.4f} {code}" for code, rate in data.get("rates", {}).items()]
    return "\n".join(lines) + f"\nrates of {data.get('date')} (Frankfurter, European Central Bank data)"


async def crypto(query: str) -> str:
    coins = (await http(f"https://api.coingecko.com/api/v3/search?query={quote(query)}")).json().get("coins", [])
    if not coins:
        return f"No coin found for {query!r}."
    coin = coins[0]
    price = (await http(f"https://api.coingecko.com/api/v3/simple/price?ids={coin['id']}&vs_currencies=usd,inr"
                        "&include_24hr_change=true&include_market_cap=true")).json()[coin["id"]]
    return (f"{coin['name']} ({coin['symbol']}), market-cap rank {coin.get('market_cap_rank')}\n"
            f"price: ${price['usd']:,} / ₹{price['inr']:,} ({price.get('usd_24h_change', 0):+.2f}% in 24h)\n"
            f"market cap: ${price.get('usd_market_cap', 0):,.0f} (CoinGecko)")


async def weather(query: str) -> str:
    places = (await http(f"https://geocoding-api.open-meteo.com/v1/search?name={quote(query)}&count=1")).json()
    if not places.get("results"):
        return f"No place found for {query!r}."
    p = places["results"][0]
    data = (await http(f"https://api.open-meteo.com/v1/forecast?latitude={p['latitude']}&longitude={p['longitude']}"
                       "&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code"
                       "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
                       "&timezone=auto&forecast_days=4")).json()
    now, day = data["current"], data["daily"]
    lines = [f"{p['name']}, {p.get('admin1', '')}, {p.get('country', '')} (local time {now['time']})",
             f"now: {now['temperature_2m']}°C (feels {now['apparent_temperature']}°C), "
             f"{WEATHER_CODES.get(now['weather_code'], 'code ' + str(now['weather_code']))}, "
             f"humidity {now['relative_humidity_2m']}%, wind {now['wind_speed_10m']} km/h"]
    for i, date in enumerate(day["time"]):
        lines.append(f"{date}: {day['temperature_2m_min'][i]}-{day['temperature_2m_max'][i]}°C, "
                     f"{WEATHER_CODES.get(day['weather_code'][i], '?')}, rain chance {day['precipitation_probability_max'][i]}%")
    return "\n".join(lines) + "\n(Open-Meteo)"


KINDS = {"stock": stock, "currency": currency, "crypto": crypto, "weather": weather}
