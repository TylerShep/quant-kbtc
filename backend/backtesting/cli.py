#!/usr/bin/env python3
"""
Backtesting CLI — run backtests, walk-forward optimization, auto-tune, and generate reports.

Usage:
    python -m backtesting run --from-db --symbol BTC --series KXBTC
    python -m backtesting run --csv data/candles_btc_15m.csv
    python -m backtesting walk-forward --from-db --symbol BTC --series KXBTC
    python -m backtesting tune --from-db --symbol BTC
    python -m backtesting report --input backtest_reports/latest.json

`Backtester` points to the contract-price engine by default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


def _load_data(args) -> tuple[list[dict], dict, dict]:
    """Load candle + contract timeline + settlement data."""
    if getattr(args, "from_db", False):
        return _load_from_db(args)

    from backtesting.data_loader import load_candles_csv, validate_candles

    print(f"Loading candles from {args.csv}...")
    candles = load_candles_csv(args.csv)
    validation = validate_candles(candles)
    print(f"  Loaded {validation['total_candles']} candles, "
          f"{validation['date_range_days']} days, {validation['gaps']} gaps")

    if not validation["valid"]:
        print(f"  WARNING: insufficient candles ({validation['total_candles']})")
        if not getattr(args, "force", False):
            sys.exit(1)

    contract_timelines: dict = {}
    settlement_data: dict = {}
    if getattr(args, "ob_csv", None):
        print(f"Loading OB snapshots from {args.ob_csv}...")
        ob_history = _load_ob_csv(args.ob_csv)
        from backtesting.contract_timeline import ContractTick, ContractTimeline

        ticker = getattr(args, "contract_ticker", "SIM-CONTRACT")
        timeline = ContractTimeline(ticker=ticker)
        for ts, snap in sorted(ob_history.items()):
            timeline.add_tick(
                ContractTick(
                    timestamp=ts,
                    ticker=ticker,
                    mid_cents=snap.get("mid_cents"),
                    best_bid=snap.get("best_bid"),
                    best_ask=snap.get("best_ask"),
                    spread_cents=snap.get("spread_cents"),
                    obi=snap.get("obi", 0.5),
                    total_bid_vol=snap.get("total_bid_vol", 0.0),
                    total_ask_vol=snap.get("total_ask_vol", 0.0),
                    source="ob_mid",
                )
            )
        timeline.finalize()
        contract_timelines[ticker] = timeline
        print(f"  Loaded {len(timeline.ticks)} contract ticks")

    return candles, contract_timelines, settlement_data


def _load_from_db(args) -> tuple[list[dict], dict, dict]:
    """Load candle + contract data from the live Postgres DB."""
    from backtesting.data_loader import (
        load_candles_db,
        load_contract_timelines_db,
        load_settlement_outcomes_db,
        validate_candles,
    )

    async def _fetch():
        from database import get_pool, close_pool
        pool = await get_pool()
        try:
            symbol = getattr(args, "symbol", "BTC")
            source = getattr(args, "source", "live_spot,binance")
            series = getattr(args, "series", "KXBTC")
            candles = await load_candles_db(pool, symbol=symbol, source=source)
            if not candles:
                return candles, {}, {}
            start_ts = candles[0]["timestamp"]
            end_ts = candles[-1]["timestamp"] + 900.0
            timelines = await load_contract_timelines_db(
                pool,
                start_ts=start_ts,
                end_ts=end_ts,
                series=series,
            )
            settlements = await load_settlement_outcomes_db(
                pool,
                start_ts=start_ts - 86400.0,
                end_ts=end_ts + 86400.0,
                series=series,
            )
            for ticker, meta in settlements.items():
                timeline = timelines.get(ticker)
                if timeline is None:
                    continue
                timeline.close_time = meta.get("close_time")
                timeline.result = meta.get("result")
                timeline.expiration_value = meta.get("expiration_value")
            return candles, timelines, settlements
        finally:
            await close_pool()

    candles, contract_timelines, settlement_data = asyncio.run(_fetch())
    validation = validate_candles(candles)
    print(f"  Loaded {validation['total_candles']} candles from DB, "
          f"{validation['date_range_days']} days, {validation['gaps']} gaps")
    total_ticks = sum(len(t.ticks) for t in contract_timelines.values())
    print(f"  Loaded {len(contract_timelines)} contract timelines ({total_ticks} ticks)")
    print(f"  Loaded {len(settlement_data)} settlement rows")

    if not validation["valid"]:
        print(f"  WARNING: insufficient candles ({validation['total_candles']})")
        if not getattr(args, "force", False):
            sys.exit(1)

    return candles, contract_timelines, settlement_data


def cmd_run(args):
    from backtesting.backtester import Backtester

    candles, contract_timelines, settlement_data = _load_data(args)

    config = {}
    if args.config:
        config = json.loads(args.config)

    print(f"Running backtest with bankroll=${args.bankroll:,.0f}...")
    start = time.time()
    bt = Backtester(
        candles,
        contract_timelines,
        config,
        settlement_data=settlement_data,
    )
    results = bt.run(bankroll=args.bankroll)
    elapsed = time.time() - start

    _print_results(results, elapsed)

    filter_conv = getattr(args, "filter_conviction", None)
    if filter_conv:
        _print_conviction_subset(bt.trades, filter_conv)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_data = {
        "type": "backtest",
        "timestamp": time.time(),
        "config": config,
        "bankroll": args.bankroll,
        "results": results,
        "trades": bt.trades,
        "equity_curve": bt.equity_curve,
        "signal_log": bt.signal_log,
        "elapsed_sec": round(elapsed, 2),
    }

    latest_path = output_dir / "latest.json"
    with open(latest_path, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"\nResults saved to {latest_path}")

    if not args.no_report:
        from backtesting.report import generate_html_report
        html_path = output_dir / "latest.html"
        generate_html_report(report_data, html_path)
        print(f"HTML report saved to {html_path}")


def cmd_walk_forward(args):
    from backtesting.walk_forward import WalkForwardOptimizer

    candles, contract_timelines, settlement_data = _load_data(args)

    param_space = _default_param_space()
    if args.params:
        param_space = json.loads(args.params)

    print(f"Running walk-forward optimization...")
    print(f"  Param space: {list(param_space.keys())}")
    combos = 1
    for v in param_space.values():
        combos *= len(v)
    print(f"  Total combinations per window: {combos}")

    start = time.time()
    optimizer = WalkForwardOptimizer(
        candles,
        contract_timelines,
        settlement_data=settlement_data,
        base_config={
            "mode": "paper",
            "ml_gate_mode": "disabled",
            "exit_fill_mode": "mark",
        },
    )
    results = optimizer.run(param_space, objective=args.objective)
    elapsed = time.time() - start

    print(f"\nWalk-Forward Results ({len(results)} windows, {elapsed:.1f}s)")
    print("-" * 70)
    for r in results:
        gap_color = "!" if r.overfitting_gap > 1.0 else " "
        print(f"  Window {r.window_id:2d}: "
              f"Train Sharpe={r.train_sharpe:6.2f}  "
              f"Test Sharpe={r.test_sharpe:6.2f}  "
              f"WR={r.test_win_rate:.1%}  "
              f"Trades={r.test_trades:3d}  "
              f"Gap={r.overfitting_gap:5.2f}{gap_color}")

    consistency = optimizer.edge_consistency(results)
    final_params = optimizer.select_final_params(results)

    overfitting = None
    try:
        overfitting = optimizer.diagnose_overfitting(results)
    except Exception:
        pass

    print(f"\nEdge Consistency: {consistency:.1%}")
    if overfitting:
        print(f"Overfitting Diagnosis: {overfitting.get('recommendation', 'N/A')}")
    print(f"Final Params: {final_params}")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_data = {
        "type": "walk_forward",
        "timestamp": time.time(),
        "param_space": param_space,
        "objective": args.objective,
        "windows": [
            {
                "window_id": r.window_id,
                "train_range": list(r.train_range),
                "test_range": list(r.test_range),
                "best_params": r.best_params,
                "train_sharpe": r.train_sharpe,
                "test_sharpe": r.test_sharpe,
                "test_win_rate": r.test_win_rate,
                "test_trades": r.test_trades,
                "overfitting_gap": r.overfitting_gap,
            }
            for r in results
        ],
        "edge_consistency": consistency,
        "final_params": final_params,
        "overfitting_diagnosis": overfitting,
        "elapsed_sec": round(elapsed, 2),
    }

    wf_path = output_dir / "walk_forward_latest.json"
    with open(wf_path, "w") as f:
        json.dump(report_data, f, indent=2, default=str)
    print(f"\nResults saved to {wf_path}")

    if not args.no_report:
        from backtesting.report import generate_html_report
        html_path = output_dir / "walk_forward_latest.html"
        generate_html_report(report_data, html_path)
        print(f"HTML report saved to {html_path}")


def cmd_tune(args):
    from backtesting.auto_tuner import run_tuning_cycle

    candles, contract_timelines, _settlement_data = _load_data(args)

    auto_apply = getattr(args, "auto_apply", False)

    async def _run():
        pool = None
        if getattr(args, "from_db", False):
            from database import get_pool, close_pool
            pool = await get_pool()
        try:
            return await run_tuning_cycle(
                candles, contract_timelines, pool=pool, auto_apply=auto_apply,
            )
        finally:
            if pool:
                from database import close_pool
                await close_pool()

    print("Running auto-tuning cycle...")
    start = time.time()
    result = asyncio.run(_run())
    elapsed = time.time() - start

    print(f"\nTuning Results ({elapsed:.1f}s)")
    print("=" * 50)
    print(f"  Edge Consistency: {result.edge_consistency:.1%}")
    print(f"  Avg OOS Sharpe:   {result.avg_oos_sharpe:.2f}")
    print(f"  Should Apply:     {result.should_apply}")
    print(f"  Reason:           {result.reason}")
    if result.changes:
        print(f"\n  Recommended Changes:")
        for key, change in result.changes.items():
            print(f"    {key:25s}  {change['from']} -> {change['to']}")
    else:
        print(f"\n  No parameter changes recommended.")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "tuning_latest.json", "w") as f:
        json.dump({
            "timestamp": result.timestamp,
            "current_params": result.current_params,
            "recommended_params": result.recommended_params,
            "edge_consistency": result.edge_consistency,
            "avg_oos_sharpe": result.avg_oos_sharpe,
            "should_apply": result.should_apply,
            "reason": result.reason,
            "changes": result.changes,
        }, f, indent=2)
    print(f"\nResults saved to {output_dir / 'tuning_latest.json'}")


def cmd_report(args):
    from backtesting.report import generate_html_report

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found")
        sys.exit(1)

    with open(input_path) as f:
        data = json.load(f)

    output_path = Path(args.output) if args.output else input_path.with_suffix(".html")
    generate_html_report(data, output_path)
    print(f"HTML report saved to {output_path}")


def _default_param_space() -> dict:
    return {
        "hard_stop_loss_pct": [0.0, 0.10, 0.20, 0.30, 0.50],
        "paper_same_side_cooldown_sec": [30.0, 60.0, 90.0, 120.0],
        "health_score_threshold": [25.0, 35.0, 45.0],
    }


def _load_ob_csv(path: str) -> dict:
    """Load OB snapshots from CSV (timestamp,obi,total_bid_vol,total_ask_vol)."""
    import csv
    ob = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = float(row["timestamp"])
            ob[ts] = {
                "obi": float(row.get("obi", 0.5)),
                "total_bid_vol": float(row.get("total_bid_vol", 0)),
                "total_ask_vol": float(row.get("total_ask_vol", 0)),
                "bids": [],
                "asks": [],
            }
    return ob


def _print_results(results: dict, elapsed: float):
    print(f"\nBacktest Results ({elapsed:.1f}s)")
    print("=" * 50)
    print(f"  Total Trades:    {results.get('total_trades', 0)}")
    print(f"  Win Rate:        {results.get('win_rate', 0):.1%}")
    print(f"  Sharpe Ratio:    {results.get('sharpe_ratio', 0):.2f}")
    print(f"  Sortino Ratio:   {results.get('sortino_ratio', 0):.2f}")
    print(f"  Total Return:    {results.get('total_return_pct', 0):.2f}%")
    print(f"  Max Drawdown:    {results.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Profit Factor:   {results.get('profit_factor', 0):.2f}")
    print(f"  Recovery Factor: {results.get('recovery_factor', 0):.2f}")
    print(f"  Avg Win:         {results.get('avg_win_pct', 0):.4f}")
    print(f"  Avg Loss:        {results.get('avg_loss_pct', 0):.4f}")
    print(f"  Avg Hold:        {results.get('avg_candles_held', 0):.1f} candles")
    print(f"  Total PnL:       ${results.get('total_pnl', 0):,.2f}")
    print(f"  Total Fees:      ${results.get('total_fees', 0):,.2f}")
    print(f"  Break-Even WR:   {results.get('breakeven_win_rate', 0):.1%}")

    flags = results.get("overfitting_red_flags", {})
    if flags:
        triggered = [k for k, v in flags.items() if v]
        if triggered:
            print(f"\n  !! OVERFITTING RED FLAGS: {', '.join(triggered)}")

    exits = results.get("exit_reasons", {})
    if exits:
        print(f"\n  Exit Reasons:")
        for reason, count in sorted(exits.items(), key=lambda x: -x[1]):
            print(f"    {reason:25s} {count:4d}")

    regime = results.get("regime_breakdown", {})
    if regime:
        print(f"\n  Regime Breakdown:")
        for r, stats in regime.items():
            print(f"    {r:10s}  trades={stats['total']:4d}  WR={stats['win_rate']:.1%}")

    passes = results.get("passes_minimum", False)
    print(f"\n  Passes Minimum Thresholds: {'YES' if passes else 'NO'}")


def _print_conviction_subset(trades: list[dict], conviction: str) -> None:
    """Print win-rate, profit factor, and trade count for a single conviction tier.

    Used to gate the ROC LOW activation — spec targets:
      win rate > 52%, profit factor > 1.2, n >= 20 net of fees.
    """
    subset = [t for t in trades if t.get("conviction") == conviction]
    n = len(subset)
    print(f"\n  Filter: conviction = {conviction}")
    print("  " + "-" * 48)
    if n == 0:
        print(f"    No trades at conviction={conviction}")
        return
    wins = [t for t in subset if t.get("pnl", 0) > 0]
    losses = [t for t in subset if t.get("pnl", 0) < 0]
    win_rate = len(wins) / n if n else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    total_pnl = sum(t.get("pnl", 0) for t in subset)
    total_fees = sum(t.get("fees", 0) for t in subset)
    avg_pnl = total_pnl / n if n else 0.0

    pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
    print(f"    Trades (n):      {n}")
    print(f"    Win Rate:        {win_rate:.1%}    (target > 52%)")
    print(f"    Profit Factor:   {pf_str}    (target > 1.20)")
    print(f"    Total PnL:       ${total_pnl:,.2f}    (net of ${total_fees:,.2f} fees)")
    print(f"    Avg PnL/trade:   ${avg_pnl:,.4f}")

    passes = (n >= 20) and (win_rate > 0.52) and (pf > 1.2)
    gate_str = "PASS" if passes else "FAIL"
    notes = []
    if n < 20:
        notes.append(f"n={n} < 20")
    if win_rate <= 0.52:
        notes.append("WR <= 52%")
    if pf <= 1.2:
        notes.append("PF <= 1.20")
    suffix = f"  ({', '.join(notes)})" if notes else ""
    print(f"    Activation Gate: {gate_str}{suffix}")


def _add_data_flags(parser):
    """Add common --csv / --from-db / --symbol / --source flags to a subparser."""
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv", help="Path to candles CSV")
    group.add_argument("--from-db", action="store_true", help="Load data from Postgres DB")
    parser.add_argument("--ob-csv", help="Path to OB snapshots CSV (CSV mode only)")
    parser.add_argument("--symbol", default="BTC", help="Symbol for DB queries (default: BTC)")
    parser.add_argument("--series", default="KXBTC", help="Kalshi ticker prefix (default: KXBTC)")
    parser.add_argument("--source", default="live_spot,binance",
                        help="Comma-separated sources for DB queries (default: live_spot,binance)")
    parser.add_argument("--force", action="store_true", help="Run even with insufficient data")


def main():
    parser = argparse.ArgumentParser(
        description="KBTC Backtesting CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # --- run ---
    p_run = sub.add_parser("run", help="Run a single backtest")
    _add_data_flags(p_run)
    p_run.add_argument("--bankroll", type=float, default=1000.0)
    p_run.add_argument("--config", help="JSON string of config overrides")
    p_run.add_argument("--output", default="backtest_reports")
    p_run.add_argument("--no-report", action="store_true", help="Skip HTML report generation")
    p_run.add_argument(
        "--filter-conviction",
        choices=["HIGH", "NORMAL", "LOW"],
        help="After the full run, print win-rate / profit-factor / n for trades at this conviction only",
    )

    # --- walk-forward ---
    p_wf = sub.add_parser("walk-forward", help="Run walk-forward optimization")
    _add_data_flags(p_wf)
    p_wf.add_argument("--params", help="JSON string of param space")
    p_wf.add_argument("--objective", default="sharpe_ratio")
    p_wf.add_argument("--output", default="backtest_reports")
    p_wf.add_argument("--no-report", action="store_true")

    # --- tune ---
    p_tune = sub.add_parser("tune", help="Run auto-tuning cycle")
    _add_data_flags(p_tune)
    p_tune.add_argument("--auto-apply", action="store_true",
                        help="Apply recommended params to bot_state (default: recommend only)")
    p_tune.add_argument("--output", default="backtest_reports")

    # --- report ---
    p_report = sub.add_parser("report", help="Generate HTML report from JSON results")
    p_report.add_argument("--input", required=True, help="Path to results JSON")
    p_report.add_argument("--output", help="Output HTML path (default: same name .html)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "walk-forward":
        cmd_walk_forward(args)
    elif args.command == "tune":
        cmd_tune(args)
    elif args.command == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
