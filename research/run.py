"""가격 읽기 → 목표 비중 계산 → 백테스트 → 터미널에서 성과 확인."""

import argparse
import importlib.metadata
import json
import platform
from pathlib import Path

from .backtest import build_targets, metrics, simulate
from .config import ResearchConfig
from .data import load_data, sha256


def run(config_path: Path, data_path: Path, output: Path, overwrite=False):
    if output.exists() and any(output.iterdir()) and not overwrite:
        raise ValueError("output is not empty; use a new path or --overwrite")

    # 1. 동봉 데이터의 해시·결측·날짜를 확인하고 설정을 읽는다.
    config = ResearchConfig.load(config_path)
    prices, risk_free, manifest = load_data(data_path, config)
    print(f"Data: {len(prices):,} sessions. Calculating daily weights...", flush=True)

    # 2. 각 날짜에 관측할 수 있었던 과거 자료만 사용해 목표 비중을 구한다.
    targets, signals = build_targets(prices, risk_free, config)

    # 3. 전략과 비교군에 같은 체결 시점·비용·회계 규칙을 적용한다.
    output.mkdir(parents=True, exist_ok=True)
    summary, saved_files = {}, []
    for name, weights in targets.items():
        result = simulate(prices, risk_free, weights, config, buy_and_hold=name == "spy_buy_hold")
        performance = metrics(result.curve, risk_free, config.annualization)
        summary[name] = performance

        for kind, frame in (("curve", result.curve), ("trades", result.trades), ("weights", result.holdings)):
            path = output / f"{name}_{kind}.csv"
            frame.to_csv(path, index=kind != "trades", float_format="%.12g", date_format="%Y-%m-%d")
            saved_files.append(path)
        print(f"{name}: CAGR {performance['cagr']:.2%}, MDD {performance['max_drawdown']:.2%}", flush=True)

    # 4. 거래 원장을 독립적으로 재검산할 수 있도록 입력·코드 해시도 남긴다.
    signals.to_csv(output / "signals.csv", index=False, float_format="%.12g", date_format="%Y-%m-%d")
    source_root = Path(__file__).resolve().parents[1]
    provenance = {
        "config": config.to_dict(),
        "data_manifest_sha256": sha256(data_path / "manifest.json"),
        "data_retrieved_at_utc": manifest["retrieved_at_utc"],
        "python": platform.python_version(),
        "libraries": {name: importlib.metadata.version(name) for name in ("numpy", "pandas", "scipy")},
        "research_source_sha256": {
            str(path.relative_to(source_root)): sha256(path)
            for path in sorted((source_root / "research").glob("*.py"))
        },
    }
    (output / "metrics.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    (output / "run.json").write_text(json.dumps(provenance, indent=2, allow_nan=False) + "\n")
    saved_files += [output / "signals.csv", output / "metrics.json", output / "run.json"]
    checksums = {path.name: sha256(path) for path in sorted(saved_files)}
    (output / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n")
    print(f"Results: {output.resolve()}")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/daily-capm.json"))
    parser.add_argument("--data", type=Path, default=Path("datasets"))
    parser.add_argument("--output", type=Path, default=Path("results/backtest"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run(args.config, args.data, args.output, args.overwrite)


if __name__ == "__main__":
    main()
