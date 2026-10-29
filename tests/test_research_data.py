import io
import json
import zipfile

import numpy as np
import pandas as pd
import pytest

from research.config import ResearchConfig
from research.data import load_data, parse_french, sha256


def zipped_factors(text):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("factors.csv", text)
    return stream.getvalue()


def test_daily_risk_free_is_percent_not_annual_rate():
    factors = parse_french(zipped_factors("Documentation\n,Mkt-RF,SMB,HML,RF\n20200102,1.00,-0.20,0.30,0.01\n"))
    assert factors.iloc[0]["rf"] == pytest.approx(0.0001)
    assert factors.iloc[0]["mkt_rf"] == pytest.approx(0.01)
    with pytest.raises(ValueError, match="sentinel"):
        parse_french(zipped_factors("20200102,-99.99,0,0,0\n"))


def write_snapshot(directory, prices, rf):
    prices.to_csv(directory / "adjusted_close.csv", index_label="date")
    pd.DataFrame({"rf": rf}).to_csv(directory / "factors.csv", index_label="date")
    manifest = {"files": {name: sha256(directory / name) for name in ("adjusted_close.csv", "factors.csv")}}
    (directory / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("invalid", ["checksum", "missing_price", "missing_rf", "unordered_dates"])
def test_bad_snapshot_cannot_silently_enter_backtest(tmp_path, invalid):
    dates = pd.bdate_range("2020-01-01", periods=10)
    prices = pd.DataFrame(100.0, index=dates, columns=["AAA", "BBB", "SPY"])
    rf = pd.Series(0.0001, index=dates)
    config = ResearchConfig(symbols=("AAA", "BBB"), data_start="2019-01-01", start="2020-01-08", end=str(dates[-1].date()), lookback=3, max_weight=0.8)
    if invalid == "missing_price":
        prices.iloc[4, 0] = np.nan
    elif invalid == "missing_rf":
        rf.iloc[4] = np.nan
    elif invalid == "unordered_dates":
        prices = prices.iloc[::-1]
    write_snapshot(tmp_path, prices, rf)
    if invalid == "checksum":
        with (tmp_path / "adjusted_close.csv").open("a") as handle:
            handle.write("\n")
    with pytest.raises(ValueError):
        load_data(tmp_path, config)
