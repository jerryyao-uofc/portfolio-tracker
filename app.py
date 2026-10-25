#!/usr/bin/env python3
"""Local portfolio tracker.

Reads every CSV in ./data, fetches prices from Yahoo Finance, and serves
the visualizer at http://127.0.0.1:8765/.
"""

from __future__ import annotations

import csv
import email.utils
import os
import xml.etree.ElementTree as ET
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
HOST = "127.0.0.1"
PORT = 8765
CACHE_SECONDS = 60
HISTORY_RANGE = "5y"
LOOKBACK_DAYS = 92
MIN_CAGR_DAYS = 365
RATE_FLOOR = -0.10
RATE_CAP = 0.12
ASSUMED_RATE = 0.06
# Keep most of the historical rate, then nudge it toward a 6% planning rate.
HISTORY_WEIGHT = 0.85

TYPE_ALIASES = ("asset_type", "type", "asset", "kind", "class")
SYMBOL_ALIASES = ("symbol", "ticker", "symbol_ticker", "tickers")
SHARE_ALIASES = ("shares", "share", "quantity", "qty", "units", "amount", "position")
COST_SHARE_ALIASES = ("cost_per_share", "cost_basis_per_share", "average_cost", "avg_cost")
COST_TOTAL_ALIASES = ("cost", "cost_basis", "total_cost")
EQUITY_SHOCK = -0.20
CRYPTO_SHOCK = -0.50

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "payload": None}


def ssl_context():
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class _AcceptHTTPError(urllib.request.HTTPErrorProcessor):
    """Yahoo's cookie endpoint answers 404 and still sets a cookie."""

    def http_response(self, request, response):
        return response

    https_response = http_response


class YahooClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context()),
            urllib.request.HTTPCookieProcessor(),
            _AcceptHTTPError(),
        )
        self._crumb = None
        self._cookie = ""

    def _request(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with self._opener.open(req, timeout=25) as response:
            return response.read()

    def _ensure_session(self) -> str:
        with self._lock:
            if self._crumb:
                return self._crumb
            self._request("https://fc.yahoo.com")
            crumb = self._request("https://query1.finance.yahoo.com/v1/test/getcrumb")
            self._crumb = crumb.decode().strip()
            return self._crumb

    def get_json(self, url: str) -> dict:
        crumb = self._ensure_session()
        joiner = "&" if "?" in url else "?"
        body = self._request(f"{url}{joiner}crumb={urllib.parse.quote(crumb)}")
        return json.loads(body.decode())


YAHOO = YahooClient()


def _pick(row: dict, names: tuple[str, ...]) -> str:
    folded = {key.strip().lower(): value for key, value in row.items() if key}
    for name in names:
        if name in folded and folded[name] is not None:
            return str(folded[name]).strip()
    return ""


def _parse_shares(raw: str) -> float:
    cleaned = raw.replace(",", "").replace("$", "").strip()
    return float(cleaned)


def yahoo_symbol(asset_type: str, symbol: str) -> str:
    ticker = symbol.strip().upper().replace(" ", "")
    if asset_type == "crypto":
        ticker = ticker.replace("/", "-").replace("_", "-")
        for suffix in ("-USDT", "USDT", "-USD"):
            if ticker.endswith(suffix) and suffix != "-USD":
                ticker = ticker[: -len(suffix)]
                break
        if not ticker.endswith("-USD"):
            ticker = f"{ticker}-USD"
        return ticker
    return ticker.replace(".", "-")


def load_holdings() -> tuple[list[dict], list[str], list[str]]:
    warnings = []
    files = []
    combined: dict[tuple[str, str], dict] = {}
    if not DATA_DIR.exists():
        return [], [], [f"No data folder at {DATA_DIR}"]

    paths = sorted(DATA_DIR.glob("*.csv"))
    if not paths:
        return [], [], ["Put one or more CSV files in the data folder."]

    for path in paths:
        files.append(path.name)
        try:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    warnings.append(f"{path.name} has no header row.")
                    continue
                for index, row in enumerate(reader, start=2):
                    asset_type = _pick(row, TYPE_ALIASES).lower()
                    symbol = _pick(row, SYMBOL_ALIASES).upper()
                    shares_raw = _pick(row, SHARE_ALIASES)
                    if asset_type in {"equity", "share", "shares"}:
                        asset_type = "stock"
                    if asset_type not in {"stock", "crypto", "etf"}:
                        warnings.append(
                            f"{path.name} row {index}: asset type must be stock or crypto (got {asset_type or 'blank'})."
                        )
                        continue
                    if not symbol or not shares_raw:
                        warnings.append(f"{path.name} row {index}: missing symbol or shares.")
                        continue
                    try:
                        shares = _parse_shares(shares_raw)
                    except ValueError:
                        warnings.append(f"{path.name} row {index}: shares '{shares_raw}' is not a number.")
                        continue
                    if shares <= 0:
                        warnings.append(f"{path.name} row {index}: shares must be greater than zero.")
                        continue
                    cost_total = _cost_total(row, shares, path.name, index, warnings)
                    key = (asset_type if asset_type != "etf" else "stock", yahoo_symbol(asset_type, symbol))
                    entry = combined.get(key)
                    if entry is None:
                        combined[key] = {
                            "type": "crypto" if asset_type == "crypto" else "stock",
                            "symbol": symbol.replace(" ", ""),
                            "yahoo": key[1],
                            "shares": shares,
                            "cost_total": cost_total,
                            "hint_etf": asset_type == "etf",
                            "sources": [path.name],
                        }
                    else:
                        entry["shares"] += shares
                        if entry["cost_total"] is None or cost_total is None:
                            entry["cost_total"] = None
                        else:
                            entry["cost_total"] += cost_total
                        entry["hint_etf"] = entry["hint_etf"] or asset_type == "etf"
                        if path.name not in entry["sources"]:
                            entry["sources"].append(path.name)
        except OSError as error:
            warnings.append(f"Could not read {path.name}: {error}")

    return list(combined.values()), files, warnings


def _cost_total(row: dict, shares: float, filename: str, index: int, warnings: list[str]) -> float | None:
    per_share = _pick(row, COST_SHARE_ALIASES)
    total = _pick(row, COST_TOTAL_ALIASES)
    try:
        if per_share:
            return _parse_shares(per_share) * shares
        if total:
            return _parse_shares(total)
    except ValueError:
        warnings.append(f"{filename} row {index}: cost is not a number.")
    return None


def _raw(node):
    if isinstance(node, dict):
        return node.get("raw")
    return node


def _canon(symbol: str) -> str:
    cleaned = symbol.upper().replace(".", "-")
    if cleaned == "GOOGL":
        return "GOOG"
    return cleaned


def _date_from_ts(timestamp: int, offset_seconds: int):
    tz = timezone(timedelta(seconds=offset_seconds))
    return datetime.fromtimestamp(timestamp, tz).date().isoformat()


def _etf_region(name: str, category: str | None) -> str:
    text = f"{name} {category or ''}".lower()
    if any(word in text for word in ("emerging", "frontier")):
        return "Developing"
    if any(word in text for word in ("international", "foreign", "developed", "eafe")):
        return "Developed"
    if any(word in text for word in ("gold", "silver", "commodity", "precious", "bond", "treasury")):
        return "Other"
    return "United States"


def fetch_symbol(holding: dict) -> dict:
    symbol = holding["yahoo"]
    chart = YAHOO.get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol)}?interval=1d&range={HISTORY_RANGE}"
    )
    result = (chart.get("chart") or {}).get("result") or []
    if not result:
        error = (chart.get("chart") or {}).get("error") or {}
        raise RuntimeError(error.get("description") or f"No price data for {symbol}")

    block = result[0]
    meta = block.get("meta") or {}
    timestamps = block.get("timestamp") or []
    indicators = block.get("indicators") or {}
    quote = (indicators.get("quote") or [{}])[0]
    closes = quote.get("close") or []
    adjusted = ((indicators.get("adjclose") or [{}])[0]).get("adjclose") or []
    offset = int(meta.get("gmtoffset") or 0)
    history = []
    for index, timestamp in enumerate(timestamps):
        value = None
        if index < len(adjusted) and adjusted[index] is not None:
            value = adjusted[index]
        elif index < len(closes) and closes[index] is not None:
            value = closes[index]
        if value is None or value <= 0:
            continue
        history.append({"date": _date_from_ts(timestamp, offset), "close": float(value)})

    price = meta.get("regularMarketPrice")
    if price is None and history:
        price = history[-1]["close"]
    previous = meta.get("chartPreviousClose")
    change = meta.get("fulldayChange")
    change_pct = meta.get("fulldayChangePercent")
    if price is not None and previous:
        if change is None:
            change = float(price) - float(previous)
        if change_pct is None and previous:
            change_pct = (float(price) - float(previous)) / float(previous) * 100

    profile = {"sector": None, "industry": None, "category": None, "dividend": None, "holdings": []}
    try:
        summary = YAHOO.get_json(
            "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
            f"{urllib.parse.quote(symbol)}?modules=assetProfile,fundProfile,topHoldings,summaryDetail,defaultKeyStatistics"
        )
        rows = (summary.get("quoteSummary") or {}).get("result") or []
        if rows:
            row = rows[0]
            asset_profile = row.get("assetProfile") or {}
            fund_profile = row.get("fundProfile") or {}
            detail = row.get("summaryDetail") or {}
            stats = row.get("defaultKeyStatistics") or {}
            profile["sector"] = asset_profile.get("sectorDisp") or asset_profile.get("sector")
            profile["industry"] = asset_profile.get("industryDisp") or asset_profile.get("industry")
            profile["category"] = fund_profile.get("categoryName")
            profile["dividend"] = _raw(detail.get("trailingAnnualDividendRate"))
            if profile["dividend"] is None and price:
                dividend_yield = (
                    _raw(detail.get("yield"))
                    or _raw(stats.get("yield"))
                    or _raw(detail.get("trailingAnnualDividendYield"))
                )
                if dividend_yield:
                    profile["dividend"] = float(dividend_yield) * float(price)
            for fund_holding in ((row.get("topHoldings") or {}).get("holdings") or []):
                percent = _raw(fund_holding.get("holdingPercent"))
                ticker = fund_holding.get("symbol")
                if percent is None or not ticker:
                    continue
                weight = float(percent)
                if weight > 1:
                    weight /= 100
                profile["holdings"].append(
                    {
                        "symbol": _canon(ticker),
                        "name": fund_holding.get("holdingName") or ticker,
                        "weight": weight,
                    }
                )
    except Exception:
        pass

    instrument = (meta.get("instrumentType") or "").upper()
    is_etf = bool(holding["hint_etf"] or instrument == "ETF")
    if holding["type"] == "crypto" or instrument == "CRYPTOCURRENCY":
        industry = "Cryptocurrency"
        sector = "Cryptocurrency"
        is_etf = False
        kind = "crypto"
    else:
        kind = "stock"
        industry = profile["industry"] or profile["sector"] or "Unclassified"
        sector = profile["sector"] or industry

    name = meta.get("longName") or meta.get("shortName") or holding["symbol"]
    region = _etf_region(name, profile["category"]) if is_etf else None

    return {
        "symbol": holding["symbol"],
        "yahoo": symbol,
        "name": name,
        "type": kind,
        "shares": holding["shares"],
        "price": None if price is None else float(price),
        "previous": None if previous is None else float(previous),
        "change": None if change is None else float(change),
        "changePct": None if change_pct is None else float(change_pct),
        "currency": meta.get("currency") or "USD",
        "isEtf": is_etf,
        "sector": sector,
        "industry": industry,
        "region": region,
        "costTotal": holding.get("cost_total"),
        "annualDividendPerShare": float(profile["dividend"] or 0),
        "constituents": profile["holdings"],
        "history": history,
        "sources": holding["sources"],
    }


def _annualized(history: list[dict]) -> float | None:
    if len(history) < 2:
        return None
    start, end = history[0], history[-1]
    start_day = datetime.fromisoformat(start["date"])
    end_day = datetime.fromisoformat(end["date"])
    days = (end_day - start_day).days
    if days < MIN_CAGR_DAYS or start["close"] <= 0:
        return None
    return (end["close"] / start["close"]) ** (365.25 / days) - 1


def _lookthrough(quotes: list[dict], total: float) -> dict:
    buckets: dict[str, dict] = {}

    def slot(symbol: str, name: str) -> dict:
        row = buckets.get(symbol)
        if row is None:
            row = {"symbol": symbol, "name": name, "direct": 0.0, "insideEtf": 0.0}
            buckets[symbol] = row
        elif name and row["name"] == row["symbol"]:
            row["name"] = name
        return row

    unattributed = 0.0
    for item in quotes:
        if item["type"] == "crypto" or not item["isEtf"]:
            slot(item["symbol"], item["name"])["direct"] += item["value"]
            continue
        covered = 0.0
        for constituent in item.get("constituents") or []:
            covered += constituent["weight"]
            slot(constituent["symbol"], constituent["name"])["insideEtf"] += item["value"] * constituent["weight"]
        leftover = max(0.0, 1 - covered)
        unattributed += item["value"] * leftover

    rows = []
    for row in buckets.values():
        combined = row["direct"] + row["insideEtf"]
        if combined <= 0:
            continue
        rows.append(
            {
                **row,
                "total": combined,
                "weight": combined / total if total else 0.0,
                "directWeight": row["direct"] / total if total else 0.0,
                "etfWeight": row["insideEtf"] / total if total else 0.0,
            }
        )
    rows.sort(key=lambda row: row["total"], reverse=True)
    return {
        "rows": rows[:18],
        "unattributed": unattributed,
        "unattributedWeight": unattributed / total if total else 0.0,
        "note": (
            "Some of these shares are held directly and some are inside your ETFs. "
            "Each fund is opened using its published top holdings. "
            "The rest of the fund is kept as other holdings inside ETFs."
        ),
    }


def _series(holdings: list[dict], days: int) -> list[dict]:
    dated = [item for item in holdings if item["history"] and item["price"] is not None]
    if not dated:
        return []
    by_symbol = []
    all_dates = set()
    for item in dated:
        closes = {point["date"]: point["close"] for point in item["history"]}
        all_dates.update(closes)
        by_symbol.append((item["shares"], closes))
    ordered = sorted(all_dates)
    if not ordered:
        return []
    cutoff = datetime.fromisoformat(ordered[-1]) - timedelta(days=days)
    last_close = [None] * len(by_symbol)
    points = []
    for day in ordered:
        total = 0.0
        seen = 0
        for index, (shares, closes) in enumerate(by_symbol):
            if day in closes:
                last_close[index] = closes[day]
            if last_close[index] is None:
                continue
            seen += 1
            total += shares * last_close[index]
        if seen == len(by_symbol) and datetime.fromisoformat(day) >= cutoff:
            points.append({"date": day, "value": total})
    return points


def _group(rows: list[dict], key: str) -> list[dict]:
    buckets: dict[str, float] = {}
    for row in rows:
        buckets[row[key]] = buckets.get(row[key], 0.0) + row["value"]
    grouped = [{"label": label, "value": value} for label, value in buckets.items()]
    grouped.sort(key=lambda item: item["value"], reverse=True)
    return grouped


def _inflation() -> dict:
    """Average consumer-price inflation over the latest available years, as a decimal."""
    url = (
        "https://api.worldbank.org/v2/country/USA;SGP;CAN;CHN/indicator/FP.CPI.TOTL.ZG"
        "?format=json&per_page=80&mrnev=10"
    )
    codes = {"USA": "USD", "SGP": "SGD", "CAN": "CAD", "CHN": "CNY"}
    buckets = {code: [] for code in codes.values()}
    fallback = {"USD": 0.025, "SGD": 0.02, "CAD": 0.021, "CNY": 0.02}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20, context=ssl_context()) as response:
            payload = json.loads(response.read().decode())
        rows = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        for row in rows or []:
            code = codes.get(row.get("countryiso3code") or "")
            value = row.get("value")
            if code and value is not None:
                buckets[code].append(float(value) / 100)
    except Exception:
        return fallback
    return {code: (sum(values) / len(values) if values else fallback[code]) for code, values in buckets.items()}


def _fx_rate(yahoo_symbol: str) -> float | None:
    chart = YAHOO.get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(yahoo_symbol)}?interval=1d&range=1d"
    )
    result = (chart.get("chart") or {}).get("result") or []
    if not result:
        return None
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    return float(price) if price else None


def _fx_rates() -> dict:
    """Units of foreign currency for one US dollar."""
    pairs = {"SGD": "SGD=X", "CAD": "CAD=X", "CNY": "CNY=X"}
    rates = {"USD": 1.0}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fx_rate, symbol): code for code, symbol in pairs.items()}
        for future in as_completed(futures):
            code = futures[future]
            try:
                rates[code] = future.result()
            except Exception:
                rates[code] = None
    return rates


def _headlines(symbols: list[str]) -> list[dict]:
    """Yahoo Finance headlines for the individual stocks in the portfolio."""

    def one(symbol: str) -> list[dict]:
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s="
            f"{urllib.parse.quote(symbol)}&region=US&lang=en-US"
        )
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15, context=ssl_context()) as response:
            root = ET.fromstring(response.read())
        rows = []
        for item in root.findall("./channel/item")[:2]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            published = item.findtext("pubDate") or ""
            try:
                when = email.utils.parsedate_to_datetime(published).isoformat()
            except (TypeError, ValueError, OverflowError):
                when = ""
            rows.append({"symbol": symbol, "title": title, "published": when})
        return rows

    found = []
    seen = set()
    if not symbols:
        return found
    workers = min(8, len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed(pool.submit(one, symbol) for symbol in symbols):
            try:
                found.extend(future.result())
            except Exception:
                continue
    unique = []
    for row in found:
        key = row["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda row: row["published"], reverse=True)
    return unique[:24]


def build_payload(force: bool = False) -> dict:
    now = time.time()
    with _cache_lock:
        if not force and _cache["payload"] and now - _cache["at"] < CACHE_SECONDS:
            cached = dict(_cache["payload"])
            cached["cached"] = True
            return cached

    holdings, files, warnings = load_holdings()
    quotes = []
    if holdings:
        workers = min(8, len(holdings))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_symbol, holding): holding for holding in holdings}
            for future in as_completed(futures):
                holding = futures[future]
                try:
                    quotes.append(future.result())
                except Exception as error:
                    warnings.append(f"{holding['symbol']}: {error}")

    quotes = [item for item in quotes if item.get("price")]
    quotes.sort(key=lambda item: item["shares"] * item["price"], reverse=True)
    total = sum(item["shares"] * item["price"] for item in quotes)
    day_change = 0.0
    for item in quotes:
        value = item["shares"] * item["price"]
        item["value"] = value
        item["weight"] = (value / total) if total else 0.0
        if item["change"] is not None:
            day_change += item["shares"] * item["change"]
        cost_total = item.get("costTotal")
        if cost_total:
            item["cost"] = cost_total
            item["gain"] = value - cost_total
            item["gainPct"] = item["gain"] / cost_total * 100
        else:
            item["cost"] = None
            item["gain"] = None
            item["gainPct"] = None
        raw_rate = _annualized(item["history"])
        item["historicalCagr"] = raw_rate
        if raw_rate is None:
            item["projectionRate"] = ASSUMED_RATE
            item["rateBasis"] = "assumed"
        else:
            tempered = HISTORY_WEIGHT * raw_rate + (1 - HISTORY_WEIGHT) * ASSUMED_RATE
            item["projectionRate"] = min(RATE_CAP, max(RATE_FLOOR, tempered))
            item["rateBasis"] = "historical" if item["projectionRate"] == tempered else "capped"

    lookthrough = _lookthrough(quotes, total)
    series = _series(quotes, LOOKBACK_DAYS)
    for item in quotes:
        item.pop("history", None)
        item.pop("constituents", None)
        item.pop("costTotal", None)

    if series:
        series[-1]["value"] = total
    start_value = series[0]["value"] if series else None
    end_value = series[-1]["value"] if series else None
    period_change = None if start_value is None else end_value - start_value
    period_pct = None if not start_value else period_change / start_value * 100

    annual_dividend = sum(
        item["annualDividendPerShare"] * item["shares"] for item in quotes if item["type"] != "crypto"
    )
    income_value = sum(item["value"] for item in quotes if item["type"] != "crypto")
    crypto_value = sum(item["value"] for item in quotes if item["type"] == "crypto")
    equity_loss = 0.0
    crypto_loss = 0.0
    for item in quotes:
        if item["type"] == "crypto":
            shock = CRYPTO_SHOCK
        elif item.get("region") == "Other":
            shock = 0.0
        else:
            shock = EQUITY_SHOCK
        loss = item["value"] * shock
        if shock == CRYPTO_SHOCK:
            crypto_loss += loss
        elif shock == EQUITY_SHOCK:
            equity_loss += loss
    stress_loss = equity_loss + crypto_loss

    non_etf = [item for item in quotes if not item["isEtf"]]
    etfs = [item for item in quotes if item["isEtf"]]
    projections = []
    for year in range(0, 31):
        value = 0.0
        for item in quotes:
            value += item["value"] * ((1 + item["projectionRate"]) ** year)
        projections.append({"year": year, "value": value})

    payload = {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "cached": False,
        "files": files,
        "warnings": warnings,
        "holdings": quotes,
        "series": series,
        "income": {
            "monthly": annual_dividend / 12,
            "annual": annual_dividend,
            "yield": (annual_dividend / income_value) if income_value else 0.0,
        },
        "stress": {
            "loss": stress_loss,
            "lossPct": (stress_loss / total * 100) if total else 0.0,
            "equityLoss": equity_loss,
            "cryptoLoss": crypto_loss,
            "cryptoWeight": (crypto_value / total) if total else 0.0,
            "cryptoLossShare": (crypto_loss / stress_loss) if stress_loss else 0.0,
        },
        "lookthrough": lookthrough,
        "news": _headlines([item["yahoo"] for item in quotes if item["type"] != "crypto" and not item["isEtf"]]),
        "fx": _fx_rates(),
        "totals": {
            "value": total,
            "dayChange": day_change,
            "dayChangePct": (day_change / (total - day_change) * 100) if (total - day_change) else 0.0,
            "periodChange": period_change,
            "periodChangePct": period_pct,
            "start": start_value,
            "end": end_value,
        },
        "etfSplit": _group(
            [{"value": item["value"], "label": "ETF" if item["isEtf"] else "Non-ETF"} for item in quotes],
            "label",
        ),
        "industries": _group(
            [{"value": item["value"], "label": item["industry"]} for item in non_etf],
            "label",
        ),
        "regions": _group(
            [{"value": item["value"], "label": item["region"] or "Other"} for item in etfs],
            "label",
        ),
        "projections": projections,
        "projectionNote": (
            "Each holding is compounded from its own annualized price change over the last five years, "
            "with that rate moved 15% of the way toward 6% a year. "
            "The drawn path then limits the rate to between -10% and +12% a year. "
            "Holdings with less than a year of history use a flat 6% assumption. "
            "The amounts then remove average consumer-price inflation, so they are in today's purchasing power. "
            "This is an illustration, not a forecast."
        ),
        "inflation": _inflation(),
    }
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["payload"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path)
        if path.path == "/api/portfolio":
            force = "refresh=1" in (path.query or "")
            try:
                payload = build_payload(force=force)
                body = json.dumps(payload).encode()
                self._send(200, body, "application/json")
            except Exception as error:
                body = json.dumps({"error": str(error)}).encode()
                self._send(500, body, "application/json")
            return

        if path.path in {"/", "/index.html"}:
            file_path = STATIC_DIR / "index.html"
            if not file_path.exists():
                self._send(404, b"Missing interface", "text/plain; charset=utf-8")
                return
            self._send(200, file_path.read_bytes(), "text/html; charset=utf-8")
            return

        self._send(404, b"Not found", "text/plain; charset=utf-8")


def main():
    DATA_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Portfolio tracker reading CSVs from {DATA_DIR}", flush=True)
    print(f"Open {url}", flush=True)
    if not os.environ.get("SKIP_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
