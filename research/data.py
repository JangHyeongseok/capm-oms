"""Fetch once with TLS validation; replay a hashed, locally frozen snapshot."""

import argparse
import csv
import hashlib
import io
import json
import ssl
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import ResearchConfig

FACTOR_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_bytes(url: str) -> bytes:
    try:
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        context = ssl.create_default_context()
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0 (educational portfolio research)"})
            with urlopen(request, timeout=45, context=context) as response:
                return response.read()
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("unreachable")


def parse_yahoo(payload: bytes, symbol: str) -> pd.Series:
    body = json.loads(payload)
    if body["chart"].get("error"):
        raise ValueError(f"Yahoo error for {symbol}: {body['chart']['error']}")
    result = body["chart"]["result"][0]
    if result["meta"]["currency"] != "USD":
        raise ValueError(f"unexpected currency for {symbol}")
    prices = result["indicators"]["adjclose"][0]["adjclose"]
    dates = [datetime.fromtimestamp(t, ZoneInfo("America/New_York")).date() for t in result["timestamp"]]
    values = pd.Series(prices, index=pd.DatetimeIndex(dates, name="date"), name=symbol, dtype=float)
    if values.index.has_duplicates or not values.index.is_monotonic_increasing:
        raise ValueError(f"duplicate or unordered price dates: {symbol}")
    return values


def parse_french(payload: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        content = archive.read(name).decode("utf-8-sig")
    rows = []
    for row in csv.reader(io.StringIO(content)):
        if len(row) >= 5 and len(row[0].strip()) == 8 and row[0].strip().isdigit():
            values = [float(x) for x in row[1:5]]
            if any(x in (-99.99, -999.0) for x in values):
                raise ValueError("French factor data contains a missing-value sentinel")
            rows.append([pd.to_datetime(row[0].strip(), format="%Y%m%d"), *[x / 100 for x in values]])
    frame = pd.DataFrame(rows, columns=["date", "mkt_rf", "smb", "hml", "rf"]).set_index("date")
    if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("invalid French factor dates")
    return frame


def download(config: ResearchConfig, destination: Path) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be empty; preserve previous snapshots in separate directories")
    raw = destination / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    period1 = int(datetime.combine(date.fromisoformat(config.data_start), datetime.min.time(), timezone.utc).timestamp())
    period2 = int(datetime.combine(date.fromisoformat(config.end) + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    urls = {
        symbol: f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={period1}&period2={period2}&interval=1d"
        for symbol in (*config.symbols, config.market)
    }
    urls["factors"] = FACTOR_URL

    def get(item):
        name, url = item
        payload = fetch_bytes(url)
        suffix = ".zip" if name == "factors" else ".json"
        path = raw / f"{name}{suffix}"
        path.write_bytes(payload)
        print(f"Downloaded {name}: {len(payload):,} bytes", flush=True)
        return name, payload

    with ThreadPoolExecutor(max_workers=3) as pool:
        payloads = dict(pool.map(get, urls.items()))
    series = [parse_yahoo(payloads[s], s) for s in (*config.symbols, config.market)]
    # Outer join deliberately fails on unequal calendars rather than hiding missing prices.
    prices = pd.concat(series, axis=1).loc[config.data_start:config.end]
    factors = parse_french(payloads["factors"]).reindex(prices.index)
    prices.to_csv(destination / "adjusted_close.csv", float_format="%.12g", date_format="%Y-%m-%d")
    factors[["rf", "mkt_rf"]].to_csv(destination / "factors.csv", float_format="%.12g", date_format="%Y-%m-%d")
    manifest = {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_data_start": config.data_start,
        "requested_data_end": config.end,
        "symbols": list(prices.columns),
        "rows": len(prices),
        "first_date": str(prices.index[0].date()),
        "last_date": str(prices.index[-1].date()),
        "sources": urls,
        "price_field": "Yahoo chart indicators.adjclose; retrospectively adjusted USD close",
        "risk_free_field": "Kenneth French daily RF, percent divided by 100; no forward fill",
        "execution_model": "fractional adjusted-price research units; next-session close; not raw tradable shares",
        "files": {str(p.relative_to(destination)): sha256(p) for p in sorted(destination.rglob("*")) if p.is_file()},
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    load_data(destination, config)
    return manifest


def load_data(directory: Path, config: ResearchConfig) -> tuple[pd.DataFrame, pd.Series, dict]:
    manifest = json.loads((directory / "manifest.json").read_text())
    if not {"adjusted_close.csv", "factors.csv"}.issubset(manifest["files"]):
        raise ValueError("manifest must hash both processed inputs")
    for filename, expected in manifest["files"].items():
        if not (directory / filename).resolve().is_relative_to(directory.resolve()):
            raise ValueError("manifest paths must remain inside the snapshot")
        if sha256(directory / filename) != expected:
            raise ValueError(f"snapshot checksum mismatch: {filename}")
    prices = pd.read_csv(directory / "adjusted_close.csv", index_col="date", parse_dates=True)
    factors = pd.read_csv(directory / "factors.csv", index_col="date", parse_dates=True)
    required = [*config.symbols, config.market]
    if any(s not in prices for s in required):
        raise ValueError("snapshot does not contain all configured symbols")
    prices = prices[required]
    if prices.empty or prices.index.has_duplicates or not prices.index.is_monotonic_increasing:
        raise ValueError("invalid price index")
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("prices must be finite, positive, and complete; no forward fill")
    if factors.index.has_duplicates:
        raise ValueError("duplicate factor dates")
    rf = factors["rf"].reindex(prices.index)
    if not np.isfinite(rf.to_numpy()).all() or (rf <= -1).any():
        raise ValueError("missing or invalid daily risk-free returns")
    if prices.index[-1] < pd.Timestamp(config.end) - pd.Timedelta(days=7):
        raise ValueError("snapshot ends before requested backtest period")
    return prices, rf, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/daily-capm.json")
    parser.add_argument("--output", default="datasets/us-etf-2005-2025")
    args = parser.parse_args()
    manifest = download(ResearchConfig.load(args.config), Path(args.output))
    print(f"Validated snapshot: {manifest['rows']} rows, {manifest['first_date']} – {manifest['last_date']}")


if __name__ == "__main__":
    main()
