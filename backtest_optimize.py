#!/usr/bin/env python3
"""
Backtest Optimization Matrix — Round 16
Tests spacing × range × grid_count combinations for WETH/USDC grid bot.
Standalone script: imports from paperbot but reads CSV directly.
"""
import sys
import math
from pathlib import Path
from itertools import product

# Ensure paperbot is importable
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np

from paperbot.strategies.grid import Grid
from paperbot.engine import Engine, BacktestResult

# ── Configuration ──────────────────────────────────────────────────────────
CSV_PATH = ROOT / "data" / "eth_usdc_1h_binance.csv"
INITIAL_CAPITAL = 5.0
TAKER_FEE_PCT = 0.05
SLIPPAGE_PCT = 0.3
GAS_USD = 0.003
MIN_TRADE_USD = 0.10
ANCHOR = 2446.0  # current config anchor

# Parameter grid
SPACINGS = [4.0, 5.0, 6.0, 7.0, 8.0]
RANGES = [15.0, 20.0, 25.0, 30.0, 35.0]
GRID_COUNTS = [8, 10, 12, 14]  # levels per side → range = spacing * grid_count


def load_data():
    """Load and sort OHLCV data chronologically."""
    df = pd.read_csv(CSV_PATH)
    # Convert ms timestamp to datetime
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("timestamp").sort_index()
    print(f"Loaded {len(df)} candles: {df.index[0]} → {df.index[-1]}")
    print(f"Price range: ${df['close'].min():.2f} – ${df['close'].max():.2f}")
    print(f"Start price: ${df['close'].iloc[0]:.2f}, End price: ${df['close'].iloc[-1]:.2f}")
    change_pct = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
    print(f"Period change: {change_pct:+.2f}%")
    return df


def compute_sharpe(equity_points, annualize=True):
    """Compute Sharpe ratio from equity curve."""
    if len(equity_points) < 2:
        return 0.0
    totals = [p.total_usd for p in equity_points]
    returns = np.diff(totals) / np.array(totals[:-1])
    if len(returns) < 2 or np.std(returns) == 0:
        return 0.0
    sharpe = np.mean(returns) / np.std(returns)
    if annualize:
        # Hourly data, annualize with sqrt(8760)
        sharpe *= math.sqrt(8760)
    return sharpe


def run_backtest(df, spacing_pct, range_pct, floating=False):
    """Run a single backtest and return metrics."""
    grid = Grid(anchor=ANCHOR, spacing_pct=spacing_pct, range_pct=range_pct)
    steps = len(grid.buy_levels)
    if steps == 0:
        return None  # No grid levels

    engine = Engine(
        grid=grid,
        capital_usd=INITIAL_CAPITAL,
        taker_fee_pct=TAKER_FEE_PCT,
        slippage_pct=SLIPPAGE_PCT,
        gas_usd=GAS_USD,
        min_trade_usd=MIN_TRADE_USD,
        floating=floating,
        reanchor_pct=25.0,
        setup_cost_usd=0.0,
    )
    result = engine.run(df)
    sharpe = compute_sharpe(result.equity)
    total_levels = 2 * steps + 1  # buy levels + sell levels + anchor

    return {
        "spacing": spacing_pct,
        "range": range_pct,
        "grid_count": steps,  # levels per side
        "total_levels": total_levels,
        "floating": floating,
        "pnl_pct": result.pnl_pct,
        "pnl_usd": result.pnl_usd,
        "final_usd": result.final_total_usd,
        "max_dd": result.max_drawdown_pct,
        "win_rate": result.win_rate_pct,
        "n_buys": result.n_buys,
        "n_sells": result.n_sells,
        "total_trades": len(result.trades),
        "sharpe": sharpe,
        "total_fees": result.total_fees_usd,
    }


def format_row(r, label=""):
    """Format a single result row for markdown."""
    return (
        f"| {label} sp={r['spacing']:.0f}% rng={r['range']:.0f}% "
        f"{'flot' if r['floating'] else 'fijo'} | "
        f"grd={r['total_levels']:>3} | "
        f"{r['pnl_pct']:>+7.2f}% | "
        f"${r['pnl_usd']:>+6.3f} | "
        f"{r['win_rate']:>5.1f}% | "
        f"{r['max_dd']:>5.2f}% | "
        f"{r['sharpe']:>+7.2f} | "
        f"{r['total_trades']:>4} | "
        f"${r['total_fees']:.4f} |"
    )


def generate_markdown(results_fixed, results_floating, by_spacing_range, df=None):
    """Generate comprehensive markdown report."""
    lines = []
    lines.append("# Backtest Optimization Matrix — Round 16")
    lines.append("")
    lines.append(f"**Fecha**: 2026-08-30 · **Timeframe**: 1h · **Velas**: 1000")
    if df is not None:
        lines.append(f"**Periodo**: {df.index[0]} → {df.index[-1]} · **Anchor**: {ANCHOR}")
    else:
        lines.append(f"**Anchor**: {ANCHOR}")
    lines.append(f"**Capital**: ${INITIAL_CAPITAL} · **Fee**: {TAKER_FEE_PCT}% · "
                 f"**Slippage**: {SLIPPAGE_PCT}% · **Gas**: ${GAS_USD}/op")
    lines.append("")
    lines.append("**Parámetros**:")
    lines.append(f"- Spacing: {SPACINGS}")
    lines.append(f"- Range: {RANGES}")
    lines.append(f"- Grid counts (niveles/lado): {GRID_COUNTS}")
    lines.append(f"- Total combos: {len(SPACINGS) * len(RANGES) * 2} (fijo + flotante)")
    lines.append("")

    # ── Summary table: fixed grid ──
    lines.append("## 1. Grid Fijo (sin re-anchor)")
    lines.append("")
    header = "| Config | Grid | PnL% | PnL$ | Win% | MaxDD | Sharpe | Trades | Fees |"
    sep =    "|--------|------|------|------|------|-------|--------|--------|------|"
    lines.append(header)
    lines.append(sep)
    for r in sorted(results_fixed, key=lambda x: x["pnl_pct"], reverse=True):
        lines.append(format_row(r))
    lines.append("")

    # ── Summary table: floating grid ──
    lines.append("## 2. Grid Flotante (re-anchor 25%)")
    lines.append("")
    lines.append(header)
    lines.append(sep)
    for r in sorted(results_floating, key=lambda x: x["pnl_pct"], reverse=True):
        lines.append(format_row(r))
    lines.append("")

    # ── Heatmap: PnL% by spacing × range (fixed) ──
    lines.append("## 3. Heatmap PnL% — Grid Fijo (spacing × range)")
    lines.append("")
    header_hm = "| Spacing \\ Range |"
    for rng in RANGES:
        header_hm += f" {rng:.0f}% |"
    sep_hm = "|---|" + "|".join(["---"] * len(RANGES)) + "|"
    lines.append(header_hm)
    lines.append(sep_hm)
    for sp in SPACINGS:
        row = f"| {sp:.0f}% |"
        for rng in RANGES:
            key = (sp, rng, False)
            if key in by_spacing_range:
                r = by_spacing_range[key]
                row += f" {r['pnl_pct']:>+6.2f}% |"
            else:
                row += " n/a |"
        lines.append(row)
    lines.append("")

    # ── Heatmap: PnL% by spacing × range (floating) ──
    lines.append("## 4. Heatmap PnL% — Grid Flotante (spacing × range)")
    lines.append("")
    lines.append(header_hm)
    lines.append(sep_hm)
    for sp in SPACINGS:
        row = f"| {sp:.0f}% |"
        for rng in RANGES:
            key = (sp, rng, True)
            if key in by_spacing_range:
                r = by_spacing_range[key]
                row += f" {r['pnl_pct']:>+6.2f}% |"
            else:
                row += " n/a |"
        lines.append(row)
    lines.append("")

    # ── Heatmap: Sharpe (fixed) ──
    lines.append("## 5. Heatmap Sharpe Ratio — Grid Fijo")
    lines.append("")
    lines.append(header_hm)
    lines.append(sep_hm)
    for sp in SPACINGS:
        row = f"| {sp:.0f}% |"
        for rng in RANGES:
            key = (sp, rng, False)
            if key in by_spacing_range:
                r = by_spacing_range[key]
                row += f" {r['sharpe']:>+6.2f} |"
            else:
                row += " n/a |"
        lines.append(row)
    lines.append("")

    # ── Heatmap: MaxDD (fixed) ──
    lines.append("## 6. Heatmap Max Drawdown% — Grid Fijo (menor = mejor)")
    lines.append("")
    lines.append(header_hm)
    lines.append(sep_hm)
    for sp in SPACINGS:
        row = f"| {sp:.0f}% |"
        for rng in RANGES:
            key = (sp, rng, False)
            if key in by_spacing_range:
                r = by_spacing_range[key]
                row += f" {r['max_dd']:>5.2f}% |"
            else:
                row += " n/a |"
        lines.append(row)
    lines.append("")

    # ── Best / Worst ──
    all_results = results_fixed + results_floating
    active_results = [r for r in all_results if r["total_trades"] > 0]

    lines.append("## 7. Ranking Global (con actividad)")
    lines.append("")
    lines.append("| # | Config | PnL% | Sharpe | MaxDD | Trades | Win% |")
    lines.append("|---|--------|------|--------|-------|--------|------|")
    for i, r in enumerate(sorted(active_results, key=lambda x: x["pnl_pct"], reverse=True)[:15], 1):
        flot = "flot" if r["floating"] else "fijo"
        lines.append(
            f"| {i} | sp={r['spacing']:.0f}% rng={r['range']:.0f}% {flot} | "
            f"{r['pnl_pct']:>+7.2f}% | {r['sharpe']:>+7.2f} | "
            f"{r['max_dd']:>5.2f}% | {r['total_trades']:>4} | {r['win_rate']:>5.1f}% |"
        )
    lines.append("")

    # ── Config comparison ──
    lines.append("## 8. Comparación con Config Actual (spacing=3.5%, range=20%)")
    lines.append("")
    # Run the current config too
    current_key = (3.5, 20.0, False)
    if current_key in by_spacing_range:
        r = by_spacing_range[current_key]
        lines.append(f"**Config actual** (sp=3.5% rng=20% fijo): PnL={r['pnl_pct']:+.2f}%, "
                     f"Sharpe={r['sharpe']:+.2f}, MaxDD={r['max_dd']:.2f}%, "
                     f"Trades={r['total_trades']}, WinRate={r['win_rate']:.1f}%")
    lines.append("")

    # ── Grid count analysis ──
    lines.append("## 9. Análisis por Grid Count (niveles por lado)")
    lines.append("")
    for gc in GRID_COUNTS:
        subset = [r for r in all_results if r["grid_count"] == gc and r["total_trades"] > 0]
        if subset:
            avg_pnl = np.mean([r["pnl_pct"] for r in subset])
            avg_sharpe = np.mean([r["sharpe"] for r in subset])
            avg_dd = np.mean([r["max_dd"] for r in subset])
            lines.append(f"- **Grid {gc} niveles/lado** ({2*gc+1} total): "
                        f"PnL medio={avg_pnl:+.2f}%, Sharpe medio={avg_sharpe:+.2f}, "
                        f"MaxDD medio={avg_dd:.2f}% ({len(subset)} combos)")
    lines.append("")

    # ── Conclusions ──
    lines.append("## 10. Conclusiones")
    lines.append("")

    best_fixed = max(results_fixed, key=lambda x: x["pnl_pct"])
    best_active_fixed = max(
        [r for r in results_fixed if r["total_trades"] > 0],
        key=lambda x: x["pnl_pct"],
        default=best_fixed
    )
    best_floating = max(results_floating, key=lambda x: x["pnl_pct"])

    lines.append(f"**Mejor grid fijo**: sp={best_active_fixed['spacing']:.0f}% "
                 f"rng={best_active_fixed['range']:.0f}% → "
                 f"PnL={best_active_fixed['pnl_pct']:+.2f}%, "
                 f"Sharpe={best_active_fixed['sharpe']:+.2f}, "
                 f"MaxDD={best_active_fixed['max_dd']:.2f}%")
    lines.append(f"**Mejor grid flotante**: sp={best_floating['spacing']:.0f}% "
                 f"rng={best_floating['range']:.0f}% → "
                 f"PnL={best_floating['pnl_pct']:+.2f}%, "
                 f"Sharpe={best_floating['sharpe']:+.2f}, "
                 f"MaxDD={best_floating['max_dd']:.2f}%")
    lines.append("")

    # Check if current config is in the matrix
    if current_key in by_spacing_range:
        r = by_spacing_range[current_key]
        better_fixed = [x for x in results_fixed if x["pnl_pct"] > r["pnl_pct"] and x["total_trades"] > 0]
        lines.append(f"**¿La config actual es óptima?** No hay {len(better_fixed)} configs fijas "
                     f"con más operaciones que superan en PnL ({r['pnl_pct']:+.2f}%).")
    lines.append("")
    lines.append("---")
    lines.append("*Generado por backtest_optimize.py — Round 16*")

    return "\n".join(lines)


def main():
    print("=" * 60)
    print("BACKTEST OPTIMIZATION — Round 16")
    print("=" * 60)

    df = load_data()

    # Run all spacing × range combos × {fixed, floating}
    all_results_fixed = []
    all_results_floating = []
    by_spacing_range = {}

    total = len(SPACINGS) * len(RANGES) * 2
    done = 0

    for sp, rng in product(SPACINGS, RANGES):
        for floating in [False, True]:
            done += 1
            r = run_backtest(df, sp, rng, floating=floating)
            if r is None:
                print(f"  [{done}/{total}] sp={sp:.0f}% rng={rng:.0f}% {'flot' if floating else 'fijo'} → SKIPPED (no levels)")
                continue

            by_spacing_range[(sp, rng, floating)] = r
            if floating:
                all_results_floating.append(r)
            else:
                all_results_fixed.append(r)

            status = "✓" if r["total_trades"] > 0 else "○"
            print(f"  [{done}/{total}] sp={sp:.0f}% rng={rng:.0f}% {'flot' if floating else 'fijo'} "
                  f"→ {r['pnl_pct']:>+7.2f}% | WR {r['win_rate']:>5.1f}% | DD {r['max_dd']:>5.2f}% | "
                  f"Sharpe {r['sharpe']:>+7.2f} | {r['total_trades']:>3} trades {status}")

    # Also run the current config (spacing=3.5, range=20) for comparison
    print("\n--- Current config (sp=3.5% rng=20%) ---")
    r_cur = run_backtest(df, 3.5, 20.0, floating=False)
    if r_cur:
        by_spacing_range[(3.5, 20.0, False)] = r_cur
        all_results_fixed.append(r_cur)
        print(f"  PnL={r_cur['pnl_pct']:+.2f}%, WR={r_cur['win_rate']:.1f}%, DD={r_cur['max_dd']:.2f}%, "
              f"Sharpe={r_cur['sharpe']:+.2f}, Trades={r_cur['total_trades']}")

    # Generate report
    md = generate_markdown(all_results_fixed, all_results_floating, by_spacing_range, df=df)
    out_path = ROOT / "data" / "backtest_optimization_round16.md"
    out_path.write_text(md)
    print(f"\n✓ Report written to {out_path}")
    print(f"  Total fixed: {len(all_results_fixed)}, floating: {len(all_results_floating)}")


if __name__ == "__main__":
    main()
