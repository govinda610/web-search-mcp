"""Live numbers from keyless APIs: stock quotes (Yahoo Finance, NSE/BSE included), currency
rates (Frankfurter, ECB data), crypto prices (CoinGecko), weather (Open-Meteo) and economic
indicators (World Bank, FRED)."""
import os
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


_currency_codes: set[str] | None = None


async def _valid_currency_codes() -> set[str]:
    """Frankfurter's currency list, fetched once and cached, so words like "how" aren't
    mistaken for ISO codes."""
    global _currency_codes
    if _currency_codes is None:
        data = (await http("https://api.frankfurter.dev/v1/currencies")).json()
        _currency_codes = set(data.keys())
    return _currency_codes


async def currency(query: str) -> str:
    """"USD INR", "100 EUR to USD" or "USD" (against INR, EUR, GBP, JPY)."""
    amount = re.search(r"\d+(?:\.\d+)?", query)
    valid = await _valid_currency_codes()
    codes = [c.upper() for c in re.findall(r"\b[A-Za-z]{3}\b", query.replace(" to ", " "))
             if c.upper() in valid]
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
    change = price.get("usd_24h_change")
    change_str = f"{change:+.2f}% in 24h" if change is not None else "24h change unknown"
    return (f"{coin['name']} ({coin['symbol']}), market-cap rank {coin.get('market_cap_rank')}\n"
            f"price: ${price['usd']:,} / ₹{price['inr']:,} ({change_str})\n"
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


# indicator word -> World Bank code; longest phrase checked first so "gdp growth" beats "gdp"
WB_INDICATORS = {"gdp growth": "NY.GDP.MKTP.KD.ZG", "gdp": "NY.GDP.MKTP.CD", "inflation": "FP.CPI.TOTL.ZG",
                 "cpi": "FP.CPI.TOTL.ZG", "unemployment": "SL.UEM.TOTL.ZS", "population": "SP.POP.TOTL",
                 "debt": "GC.DOD.TOTL.GD.ZS"}
WB_ALIASES = {"us": "united states", "usa": "united states", "america": "united states", "uk": "united kingdom"}

_wb_countries: dict[str, str] | None = None


async def _wb_country_codes() -> dict[str, str]:
    """World Bank's own country/aggregate list, name -> id, fetched once and cached."""
    global _wb_countries
    if _wb_countries is None:
        data = (await http("https://api.worldbank.org/v2/country?format=json&per_page=400")).json()
        _wb_countries = {c["name"].lower(): c["id"] for c in data[1]}
    return _wb_countries


async def economy(query: str) -> str:
    """"India GDP growth", "US CPI", "world population". Indicators: gdp, gdp growth,
    inflation/cpi, unemployment, population, debt, via the World Bank. For other US series
    (with FRED_API_KEY set), falls back to searching FRED by name."""
    words = query.lower()
    indicator = next((code for name, code in sorted(WB_INDICATORS.items(), key=lambda x: -len(x[0]))
                      if re.search(rf"\b{re.escape(name)}\b", words)), None)
    countries = await _wb_country_codes()
    country = next((name for name in sorted(countries, key=len, reverse=True)
                    if re.search(rf"\b{re.escape(name)}\b", words)), None)
    if not country:
        alias = next((a for a in WB_ALIASES if re.search(rf"\b{a}\b", words)), None)
        country = WB_ALIASES.get(alias)
    if indicator and country:
        code_id = countries[country]
        data = (await http(f"https://api.worldbank.org/v2/country/{code_id}/indicator/{indicator}"
                           "?format=json&per_page=20")).json()
        points = [p for p in data[1] if p.get("value") is not None][:8] if len(data) > 1 else []
        if not points:
            return f"No World Bank data for {country.title()} / {indicator}."
        lines = [f"{points[0]['country']['value']}: {points[0]['indicator']['value']}"]
        for p in reversed(points):
            lines.append(f"{p['date']}: {p['value']:,.2f}")
        return "\n".join(lines) + "\n(World Bank)"
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return ('No World Bank match. Ask e.g. "India GDP growth" or "US population" '
                '(indicators: gdp, gdp growth, inflation/cpi, unemployment, population, debt), '
                "or set FRED_API_KEY to search other US series by name.")
    found = (await http(f"https://api.stlouisfed.org/fred/series/search?search_text={quote(query)}"
                        f"&api_key={key}&file_type=json&limit=1")).json().get("seriess") or []
    if not found:
        return f"No FRED series found for {query!r}."
    series = found[0]
    obs = (await http(f"https://api.stlouisfed.org/fred/series/observations?series_id={series['id']}"
                      f"&api_key={key}&file_type=json&sort_order=desc&limit=8")).json().get("observations", [])
    lines = [f"{series['title']} ({series['id']}, {series.get('units', '')}, {series.get('frequency', '')})"]
    for o in reversed(obs):
        lines.append(f"{o['date']}: {o['value']}")
    return "\n".join(lines) + "\n(FRED, Federal Reserve Bank of St. Louis)"


KINDS = {"stock": stock, "currency": currency, "crypto": crypto, "weather": weather, "economy": economy}
