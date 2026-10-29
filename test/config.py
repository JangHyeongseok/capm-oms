import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[str, ...] = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
    market: str = "SPY"
    data_start: str = "2005-01-01"
    start: str = "2010-01-01"
    end: str = "2025-12-31"
    lookback: int = 252
    annualization: int = 252
    risk_aversion: float = 5.0
    max_weight: float = 0.25
    covariance_shrinkage: float = 0.10
    fee_bps: float = 5.0
    initial_capital: float = 100_000.0

    def __post_init__(self):
        if len(self.symbols) < 2 or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("at least two distinct assets are required")
        if self.market in self.symbols:
            raise ValueError("market benchmark must be separate from the asset universe")
        if not all(s.isascii() and s.isalpha() and s.isupper() for s in (*self.symbols, self.market)):
            raise ValueError("use uppercase alphabetic ticker symbols")
        if not date.fromisoformat(self.data_start) < date.fromisoformat(self.start) <= date.fromisoformat(self.end):
            raise ValueError("dates must satisfy data_start < start <= end")
        if type(self.lookback) is not int or self.lookback < 3:
            raise ValueError("lookback must be an integer >= 3")
        if type(self.annualization) is not int or self.annualization < 1:
            raise ValueError("annualization must be a positive integer")
        for name in ("risk_aversion", "max_weight", "covariance_shrinkage", "fee_bps", "initial_capital"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.risk_aversion <= 0 or self.initial_capital <= 0:
            raise ValueError("risk_aversion and initial_capital must be positive")
        if not 1 / len(self.symbols) <= self.max_weight <= 1:
            raise ValueError("infeasible max_weight for a fully invested long-only portfolio")
        if not 0 <= self.covariance_shrinkage <= 1 or not 0 <= self.fee_bps < 1000:
            raise ValueError("invalid shrinkage or fee_bps")

    @classmethod
    def load(cls, path: str | Path):
        values = json.loads(Path(path).read_text())
        if "symbols" in values:
            values["symbols"] = tuple(values["symbols"])
        return cls(**values)

    def to_dict(self):
        return asdict(self)
