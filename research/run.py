"""Run the frozen daily CAPM experiment and produce an auditable report."""

import argparse
import html
import importlib.metadata
import json
import os
import platform
import tempfile
from dataclasses import replace
from pathlib import Path

# Cache in a writable temporary directory, including sandboxed environments.
_plot_cache = Path(tempfile.gettempdir()) / "daily-capm-plot-cache"
_plot_cache.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_plot_cache))
os.environ.setdefault("XDG_CACHE_HOME", str(_plot_cache))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .backtest import build_targets, metrics, simulate
from .config import ResearchConfig
from .data import load_data, sha256

LABELS = {
    "capm_mean_variance": "CAPM + mean-variance", "minimum_variance": "Minimum variance",
    "equal_weight": "Equal weight (1/9)", "spy_buy_hold": "SPY buy & hold",
}
KOREAN_LABELS = dict(zip(LABELS.values(), ("CAPM + 평균·분산", "최소분산", "동일비중 (1/9)", "SPY 매수 후 보유")))


def create_figures(results, output: Path):
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]}, layout="constrained")
    for name, result in results.items():
        nav = result.curve["nav"]
        axes[0].plot(nav.index, nav / nav.iloc[0], label=LABELS[name], linewidth=1.5)
        axes[1].plot(nav.index, nav / nav.cummax() - 1, linewidth=1.1)
    axes[0].set(title="Growth of $1 after costs | signal at t, execution at next close", ylabel="Portfolio value (log scale)", yscale="log")
    axes[0].legend(loc="upper left", fontsize=9, ncols=2)
    axes[1].set(ylabel="Drawdown")
    axes[1].yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1))
    for ax in axes:
        ax.grid(alpha=0.2)
    fig.savefig(output / "performance.png", dpi=160)
    plt.close(fig)
    weights = results["capm_mean_variance"].holdings
    fig, ax = plt.subplots(figsize=(12, 4), layout="constrained")
    ax.stackplot(weights.index, *[weights[c] for c in weights], labels=weights.columns, alpha=0.9)
    ax.set(title="CAPM + mean-variance: daily portfolio weights after trading", ylabel="Weight", ylim=(0, 1))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1))
    ax.legend(loc="upper left", ncols=len(weights.columns), fontsize=8)
    fig.savefig(output / "weights.png", dpi=160)
    plt.close(fig)


def formatted_table(frame, percentage_columns=()):
    display = frame.copy()
    for column in percentage_columns:
        if column in display:
            display[column] = display[column].map(lambda x: f"{x:.2%}")
    for column in ("sharpe", "annualized_turnover"):
        if column in display:
            display[column] = display[column].map(lambda x: "—" if x is None or pd.isna(x) else f"{x:.2f}")
    for column in ("final_nav", "total_fees"):
        if column in display:
            display[column] = display[column].map(lambda x: f"${x:,.2f}")
    if "strategy" in display:
        display["strategy"] = display["strategy"].replace(KOREAN_LABELS)
    display = display.rename(columns={
        "strategy": "전략", "cagr": "연복리수익률", "annualized_volatility": "연변동성",
        "sharpe": "샤프지수", "max_drawdown": "최대 낙폭", "annualized_turnover": "연회전율 (배)",
        "total_fees": "누적 비용", "final_nav": "최종 자산", "period": "기간", "year": "연도",
        "fee_bps": "편도 비용 (bp)", **KOREAN_LABELS,
    }).rename_axis(columns=None)
    return display.to_html(index=False, border=0, classes="results", escape=True)


def report(output: Path, config, summary, yearly, periods, sensitivity, provenance):
    main = pd.DataFrame([{"strategy": LABELS[name], **{key: values[key] for key in ("cagr", "annualized_volatility", "sharpe", "max_drawdown", "annualized_turnover", "total_fees", "final_nav")}} for name, values in summary.items()])
    table = formatted_table(main, ("cagr", "annualized_volatility", "max_drawdown"))
    sensitivity_table = formatted_table(sensitivity[["strategy", "fee_bps", "cagr", "sharpe", "max_drawdown"]], ("cagr", "max_drawdown"))
    periods_table = formatted_table(periods[["period", "strategy", "cagr", "sharpe", "max_drawdown"]], ("cagr", "max_drawdown"))
    yearly_table = formatted_table(yearly.pivot(index="year", columns="strategy", values="return").rename(columns=LABELS).reset_index(), LABELS.values())
    capm = summary["capm_mean_variance"]
    comparison = "높았습니다" if capm["cagr"] > summary["spy_buy_hold"]["cagr"] else "낮았습니다"
    body = f'''<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daily CAPM Portfolio · Research Report</title>
<style>
body{{margin:0;background:#f2f5f8;color:#16263b;font:16px/1.7 system-ui,-apple-system,sans-serif}}main{{max-width:1120px;margin:40px auto;padding:32px;background:white;border-radius:16px}}h1{{font-size:32px;line-height:1.25}}h2{{font-size:22px;margin-top:38px}}.badge{{font-size:13px;color:#246351}}.muted{{color:#566477}}img{{width:100%;height:auto}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}th,td{{padding:10px;text-align:right;border-bottom:1px solid #dce3ec}}th:first-child,td:first-child{{text-align:left}}th{{background:#edf2f7}}code{{background:#edf2f7;padding:2px 5px}}.callout{{background:#eef4ff;padding:18px 22px;border-left:4px solid #476edb}}a{{color:#295bc3}}@media(max-width:700px){{main{{margin:0;padding:20px;border-radius:0}}h1{{font-size:26px}}}}
</style><main>
<div class="badge">REPRODUCIBLE RESEARCH · DAILY · NO LIVE ORDERS</div>
<h1>CAPM 기반 일별 자산배분 시스템</h1>
<p class="muted">{summary['capm_mean_variance']['start']} — {summary['capm_mean_variance']['end']} · 실제 ETF 조정 종가 · USD · 거래비용 반영</p>
<div class="callout">투자 가설: 개별 차트 신호 대신 시장 민감도와 자산 간 공분산으로 보유 비중을 정한다. 기대수익만 높이는 것보다 위험과 집중도를 함께 관리한다.</div>
<h2>실험 결과</h2>
<p>CAPM 전략의 연복리수익률은 {capm['cagr']:.2%}, 최대 낙폭은 {capm['max_drawdown']:.2%}입니다. 이 기간 SPY의 연복리수익률보다 {comparison}. 이 수치는 아래에 고정한 조건의 과거 모의 결과이며 미래 성과를 보장하지 않습니다.</p>
<div class="scroll">{table}</div>
<p class="muted">연복리수익률: 복리로 환산한 연평균 수익 · 연변동성: 일별 수익률의 흔들림을 연율화 · 샤프지수: 무위험수익 대비 위험조정성과 · 최대 낙폭: 이전 최고점 대비 최대 하락 · 연회전율: 연간 매수+매도 금액/거래 직전 자산의 합계(양방향). 비용 {config.fee_bps:g}bp는 거래금액의 {config.fee_bps / 100:.3f}%입니다.</p>
<img src="performance.png" alt="누적 성과와 최대 낙폭 비교 그래프">
<h2>어떻게 비중을 정하는가</h2>
<ol><li>직전 {config.lookback}거래일로 각 ETF의 시장 베타와 공분산을 추정합니다. 시장은 SPY, 무위험수익률은 Kenneth French 일별 RF입니다.</li>
<li><code>μᵢ = RF 평균 + βᵢ × 시장 초과수익 평균</code>으로 기대수익을 추정하고 연율화합니다. CAPM의 알파는 0으로 둡니다.</li>
<li><code>max μᵀw − λ/2 × wᵀΣw</code>를 풉니다. λ={config.risk_aversion:g}, 비중 합계 100%, 공매도·차입 없음, 자산당 최대 {config.max_weight:.0%}, 공분산 대각 축소 {config.covariance_shrinkage:.0%}입니다.</li>
<li>t일 종가까지 관측 → t+1 거래일 종가에 체결 → t+1에서 t+2로 가는 수익부터 새 비중에 반영합니다. 첫 체결 전에는 현금입니다.</li></ol>
<p>수익률 추정 기간과 파라미터는 결과 확인 전에 정했고, 이 실행에서 성과에 맞춰 탐색하지 않았습니다. 2010년 이후는 매일 과거 구간만 쓰는 순차 검증 구간입니다. 보유 비중과 달라진 금액만 매수·매도합니다.</p>
<img src="weights.png" alt="시간에 따른 CAPM 전략의 섹터 비중">
<h2>기간별 확인</h2><div class="scroll">{periods_table}</div>
<h2>거래비용 민감도</h2><p>비중 신호를 그대로 두고 비용만 바꿉니다. 비용을 최적화 목적함수에 넣지는 않았습니다.</p><div class="scroll">{sensitivity_table}</div>
<h2>연도별 수익률</h2><div class="scroll">{yearly_table}</div>
<h2>재현과 추적</h2>
<p>원시 응답·가공 CSV의 SHA-256을 검증한 후 실행했습니다. 데이터 스냅숏: <code>{html.escape(provenance['data_manifest_sha256'][:16])}…</code>. 모델 설정은 <a href="run.json">run.json</a>, 수치는 <a href="metrics.json">metrics.json</a>, 날짜별 성과는 <a href="capm_mean_variance_curve.csv">curve.csv</a>, 매수·매도는 <a href="capm_mean_variance_trades.csv">trades.csv</a>, 추정치는 <a href="signals.csv">signals.csv</a>에서 확인할 수 있습니다.</p>
<h2>해석의 범위</h2>
<p>가격은 Yahoo Finance의 사후 조정 종가입니다. 배당·분할을 반영한 수익률의 대용치로 쓰며, 체결 원장은 분수 단위의 조정가격 기준 연구용 수량입니다. 실제 주식 수량이나 증권사 체결 이력이 아닙니다. 배당 현금을 별도로 더하지 않습니다. 매도와 매수 모두 다음 종가에 전량 체결된다고 가정하며 스프레드·슬리피지는 고정 비용에만 근사합니다. 세금, 시장 충격, 호가·거래량 한도, 주문 거절, 공휴일 주문 처리는 모델링하지 않았습니다.</p>
<p>현재 선택한 9개 기존 섹터 ETF와 SPY에 대한 실험으로, 전체 상장·상장폐지 종목을 다룬 생존편향 없는 주식 선택 검증은 아닙니다. 현대 섹터 구성과 완전히 같은 분류도 아닙니다. 조정가격과 RF는 당시 입수본이 아닌 현재 다운로드본이므로 소급 수정과 배당 시점 처리의 한계가 있습니다. CAPM의 단기 기대수익은 매우 불안정할 수 있습니다.</p>
<p>기존 OMS는 별도의 모의 주문·중복 방지·대사 시연이며 이 연구 원장과 연결되지 않았습니다. 현재 OMS의 정수 주식 지정가 매수 제한과 달리 이 백테스트는 연구용 분수 단위 매수·매도를 지원합니다. 실거래 연결은 제출 범위 밖입니다.</p>
<p class="muted">출처: <a href="https://finance.yahoo.com/quote/SPY/history/">Yahoo Finance historical data</a> · <a href="https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_factors.html">Kenneth French factors</a> · <a href="https://docs.scipy.org/doc/scipy/reference/optimize.minimize-slsqp.html">SciPy SLSQP</a></p>
</main></html>'''
    (output / "report.html").write_text(body, encoding="utf-8")


def run(config_path: Path, data_path: Path, output: Path, overwrite=False):
    if output.exists() and any(output.iterdir()) and not overwrite:
        raise ValueError("output is not empty; use a new path or --overwrite")
    output.mkdir(parents=True, exist_ok=True)
    config = ResearchConfig.load(config_path)
    prices, rf, manifest = load_data(data_path, config)
    print(f"Validated frozen data: {len(prices):,} sessions; computing rolling targets…", flush=True)
    targets, diagnostics = build_targets(prices, rf, config)
    summary, results = {}, {}
    for name, target in targets.items():
        result = simulate(prices, rf, target, config, buy_and_hold=name == "spy_buy_hold")
        results[name] = result
        summary[name] = metrics(result.curve, rf, config.annualization)
        for kind, frame in (("curve", result.curve), ("trades", result.trades), ("weights", result.holdings)):
            frame.to_csv(output / f"{name}_{kind}.csv", index=kind != "trades", float_format="%.12g", date_format="%Y-%m-%d")
        print(f"{name}: CAGR {summary[name]['cagr']:.2%}, MDD {summary[name]['max_drawdown']:.2%}", flush=True)
    diagnostics.to_csv(output / "signals.csv", index=False, float_format="%.12g", date_format="%Y-%m-%d")
    yearly_rows, period_rows, sensitivity_rows = [], [], []
    for name, result in results.items():
        daily = result.curve["net_return"].iloc[1:]
        for year, values in daily.groupby(daily.index.year):
            yearly_rows.append({"year": year, "strategy": name, "return": float((1 + values).prod() - 1)})
        for low, high in ((2010, 2014), (2015, 2019), (2020, 2025)):
            positions = np.flatnonzero((result.curve.index.year >= low) & (result.curve.index.year <= high))
            positions = positions[positions > 0]
            if len(positions) >= 2:
                curve = result.curve.iloc[positions[0] - 1:positions[-1] + 1]
                period_rows.append({"period": f"{low}–{high}", "strategy": LABELS[name], **metrics(curve, rf, config.annualization)})
    for name in ("capm_mean_variance", "equal_weight"):
        for fee_bps in sorted({0, config.fee_bps, 10, 25}):
            result = results[name] if fee_bps == config.fee_bps else simulate(prices, rf, targets[name], replace(config, fee_bps=fee_bps))
            sensitivity_rows.append({"strategy": LABELS[name], "fee_bps": fee_bps, **metrics(result.curve, rf, config.annualization)})
    yearly, periods, sensitivity = pd.DataFrame(yearly_rows), pd.DataFrame(period_rows), pd.DataFrame(sensitivity_rows)
    yearly.to_csv(output / "yearly_returns.csv", index=False)
    periods.to_csv(output / "period_metrics.csv", index=False)
    sensitivity.to_csv(output / "cost_sensitivity.csv", index=False)
    source_root = Path(__file__).resolve().parents[1]
    provenance = {
        "config": config.to_dict(), "data_manifest_sha256": sha256(data_path / "manifest.json"),
        "data_retrieved_at_utc": manifest["retrieved_at_utc"],
        "python": platform.python_version(),
        "libraries": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scipy", "matplotlib")},
        "research_source_sha256": {str(p.relative_to(source_root)): sha256(p) for p in sorted((source_root / "research").glob("*.py"))},
        "notes": ["No parameter search was used for this run.", "Signals use trailing data; execution is at the following session close.", "Adjusted-price fractional research ledger, not the teaching OMS database."],
    }
    (output / "metrics.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    (output / "run.json").write_text(json.dumps(provenance, indent=2, allow_nan=False) + "\n")
    create_figures(results, output)
    report(output, config, summary, yearly, periods, sensitivity, provenance)
    generated = {p.name: sha256(p) for p in sorted(output.iterdir()) if p.is_file() and p.name != "checksums.json"}
    (output / "checksums.json").write_text(json.dumps(generated, indent=2) + "\n")
    print(f"Report: {(output / 'report.html').resolve()}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/daily-capm.json"))
    parser.add_argument("--data", type=Path, default=Path("datasets/us-etf-2005-2025"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/daily-capm"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run(args.config, args.data, args.output, args.overwrite)


if __name__ == "__main__":
    main()
