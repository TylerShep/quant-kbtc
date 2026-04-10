"""
HTML report generator for backtesting results.
Uses embedded Chart.js for interactive charts — no matplotlib dependency needed.
Outputs per-run artifacts (trades.csv, params.json) alongside HTML.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path


def generate_html_report(data: dict, output_path: str | Path) -> None:
    report_type = data.get("type", "backtest")
    if report_type == "walk_forward":
        html = _generate_walk_forward_report(data)
    else:
        html = _generate_backtest_report(data)

    out = Path(output_path)
    out.write_text(html)

    # Per-run artifact export (per backtesting-framework skill)
    _export_artifacts(data, out.parent)


def _export_artifacts(data: dict, output_dir: Path) -> None:
    """Export trades.csv, params.json, and summary.json alongside the HTML."""
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = data.get("trades", [])
    if trades:
        csv_path = output_dir / "trades.csv"
        fieldnames = list(trades[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(trades)

    config = data.get("config", {})
    if config:
        with open(output_dir / "params.json", "w") as f:
            json.dump(config, f, indent=2)

    results = data.get("results", data.get("final_params", {}))
    with open(output_dir / "summary.json", "w") as f:
        json.dump(results, f, indent=2, default=str)


def _red_flag_banner(results: dict) -> str:
    """Generate a warning banner if overfitting red flags are triggered."""
    flags = results.get("overfitting_red_flags", {})
    triggered = [k.replace("_", " ").title() for k, v in flags.items() if v]
    if not triggered:
        return ""
    flag_list = ", ".join(triggered)
    return f"""
<div style="background:#f8514933;border:2px solid #f85149;border-radius:8px;padding:16px;margin-bottom:20px;">
  <strong style="color:#f85149;font-size:1.1rem;">OVERFITTING RED FLAGS</strong>
  <p style="margin-top:8px;color:#f0883e;">{flag_list}</p>
  <p style="color:#8b949e;font-size:0.85rem;">These metrics suggest the backtest results may not generalize to live trading.</p>
</div>"""


def _generate_backtest_report(data: dict) -> str:
    results = data.get("results", {})
    trades = data.get("trades", [])
    equity = data.get("equity_curve", [])
    signal_log = data.get("signal_log", [])
    config = data.get("config", {})

    equity_json = json.dumps(equity[:5000])
    pnl_by_trade = json.dumps([t["pnl"] for t in trades])
    candles_held = json.dumps([t.get("candles_held", 0) for t in trades])

    exit_reasons = results.get("exit_reasons", {})
    exit_labels = json.dumps(list(exit_reasons.keys()))
    exit_values = json.dumps(list(exit_reasons.values()))

    regime = results.get("regime_breakdown", {})
    regime_labels = json.dumps(list(regime.keys()))
    regime_wr = json.dumps([s["win_rate"] * 100 for s in regime.values()])
    regime_counts = json.dumps([s["total"] for s in regime.values()])

    signal_obi = _signal_accuracy(signal_log, trades, "obi_dir")
    signal_roc = _signal_accuracy(signal_log, trades, "roc_dir")

    trade_rows = _trade_table_rows(trades[:200])

    ts = datetime.fromtimestamp(data.get("timestamp", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    red_flags_html = _red_flag_banner(results)

    breakeven_wr = results.get("breakeven_win_rate", 0)
    actual_wr = results.get("win_rate", 0)
    wr_margin = actual_wr - breakeven_wr

    # Attribution summary from trades
    attribution_html = ""
    try:
        from backtesting.attribution import run_attribution
        attr = run_attribution(trades)
        attr_signal = attr.get("signal_attribution", {})
        attr_exit = attr.get("exit_reason_breakdown", {})
        attr_session = attr.get("session_attribution", {})

        conviction_rows = ""
        for level in ("HIGH", "NORMAL", "LOW"):
            if level in attr_signal:
                s = attr_signal[level]
                conviction_rows += f"<tr><td>{level}</td><td>{s['trades']}</td><td>${s['pnl_dollars']:,.2f}</td><td>{s['win_rate']:.1%}</td><td>{s['pnl_share_pct']:.1f}%</td></tr>"

        session_rows = ""
        for sess, s in attr_session.items():
            session_rows += f"<tr><td>{sess}</td><td>{s['trades']}</td><td>${s['pnl_dollars']:,.2f}</td><td>{s['win_rate']:.1%}</td></tr>"

        attribution_html = f"""
<h2>Performance Attribution</h2>
<div class="row">
<div>
  <h3 style="color:#8b949e;font-size:0.9rem;">By Conviction</h3>
  <div class="table-wrap"><table>
  <thead><tr><th>Conviction</th><th>Trades</th><th>PnL</th><th>Win Rate</th><th>PnL Share</th></tr></thead>
  <tbody>{conviction_rows}</tbody>
  </table></div>
</div>
<div>
  <h3 style="color:#8b949e;font-size:0.9rem;">By Session</h3>
  <div class="table-wrap"><table>
  <thead><tr><th>Session</th><th>Trades</th><th>PnL</th><th>Win Rate</th></tr></thead>
  <tbody>{session_rows}</tbody>
  </table></div>
</div>
</div>"""
    except Exception:
        pass

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KBTC Backtest Report — {ts}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0d1117; color:#c9d1d9; padding:24px; }}
  h1 {{ font-size:1.5rem; margin-bottom:4px; color:#58a6ff; }}
  h2 {{ font-size:1.1rem; margin:24px 0 12px; color:#8b949e; border-bottom:1px solid #21262d; padding-bottom:6px; }}
  h3 {{ margin:12px 0 8px; }}
  .meta {{ color:#8b949e; font-size:0.85rem; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px; }}
  .card .label {{ font-size:0.75rem; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; }}
  .card .value {{ font-size:1.4rem; font-weight:600; margin-top:4px; }}
  .green {{ color:#3fb950; }} .red {{ color:#f85149; }} .blue {{ color:#58a6ff; }}
  .chart-container {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; margin-bottom:16px; }}
  canvas {{ max-height:300px; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.8rem; }}
  th {{ text-align:left; padding:8px; background:#161b22; color:#8b949e; border-bottom:1px solid #21262d; position:sticky; top:0; }}
  td {{ padding:6px 8px; border-bottom:1px solid #21262d; }}
  .table-wrap {{ max-height:400px; overflow-y:auto; background:#0d1117; border:1px solid #21262d; border-radius:8px; }}
  .config {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:12px; font-family:monospace; font-size:0.8rem; white-space:pre-wrap; }}
</style>
</head>
<body>
<h1>KBTC Backtest Report</h1>
<div class="meta">{ts} &middot; {results.get('total_trades',0)} trades &middot; {results.get('total_days',0)} days &middot; Bankroll ${data.get('bankroll',0):,.0f} &middot; {data.get('elapsed_sec',0):.1f}s</div>

{red_flags_html}

<div class="grid">
  <div class="card"><div class="label">Total Return</div><div class="value {'green' if results.get('total_return_pct',0)>=0 else 'red'}">{results.get('total_return_pct',0):+.2f}%</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value {'green' if results.get('win_rate',0)>=0.5 else 'red'}">{results.get('win_rate',0):.1%}</div></div>
  <div class="card"><div class="label">Sharpe</div><div class="value {'green' if results.get('sharpe_ratio',0)>=1 else 'blue'}">{results.get('sharpe_ratio',0):.2f}</div></div>
  <div class="card"><div class="label">Sortino</div><div class="value blue">{results.get('sortino_ratio',0):.2f}</div></div>
  <div class="card"><div class="label">Max Drawdown</div><div class="value red">{results.get('max_drawdown_pct',0):.2f}%</div></div>
  <div class="card"><div class="label">Profit Factor</div><div class="value blue">{results.get('profit_factor',0):.2f}</div></div>
  <div class="card"><div class="label">Recovery Factor</div><div class="value blue">{results.get('recovery_factor',0):.2f}</div></div>
  <div class="card"><div class="label">Total PnL</div><div class="value {'green' if results.get('total_pnl',0)>=0 else 'red'}">${results.get('total_pnl',0):,.2f}</div></div>
  <div class="card"><div class="label">Total Fees</div><div class="value red">${results.get('total_fees',0):,.2f}</div></div>
  <div class="card"><div class="label">Break-Even WR</div><div class="value {'green' if wr_margin>0 else 'red'}">{breakeven_wr:.1%}</div></div>
  <div class="card"><div class="label">WR Margin</div><div class="value {'green' if wr_margin>0 else 'red'}">{wr_margin:+.1%}</div></div>
  <div class="card"><div class="label">Avg Hold</div><div class="value blue">{results.get('avg_candles_held',0):.1f} candles</div></div>
</div>

<h2>Equity Curve</h2>
<div class="chart-container"><canvas id="equityChart"></canvas></div>

<h2>Trade PnL Distribution</h2>
<div class="chart-container"><canvas id="pnlChart"></canvas></div>

<div class="row">
<div>
  <h2>Exit Reasons</h2>
  <div class="chart-container"><canvas id="exitChart"></canvas></div>
</div>
<div>
  <h2>Win Rate by Regime</h2>
  <div class="chart-container"><canvas id="regimeChart"></canvas></div>
</div>
</div>

<div class="row">
<div>
  <h2>Trade Duration Distribution</h2>
  <div class="chart-container"><canvas id="durationChart"></canvas></div>
</div>
<div>
  <h2>Signal Accuracy</h2>
  <div class="chart-container"><canvas id="signalChart"></canvas></div>
</div>
</div>

{attribution_html}

<h2>Trade Log (first 200)</h2>
<div class="table-wrap">
<table>
<thead><tr><th>#</th><th>Direction</th><th>Entry</th><th>Exit</th><th>PnL</th><th>PnL%</th><th>Fees</th><th>Exit Reason</th><th>Conviction</th><th>Regime</th><th>Hold</th></tr></thead>
<tbody>{trade_rows}</tbody>
</table>
</div>

{f'<h2>Config Overrides</h2><div class="config">{json.dumps(config, indent=2)}</div>' if config else ''}

<script>
const chartDefaults = {{ responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}, scales:{{x:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}} }};
Chart.defaults.color = '#c9d1d9';

new Chart(document.getElementById('equityChart'), {{
  type:'line',
  data:{{ labels:Array.from({{length:{json.dumps(len(equity[:5000]))}}},(_,i)=>i), datasets:[{{data:{equity_json},borderColor:'#58a6ff',borderWidth:1.5,pointRadius:0,fill:true,backgroundColor:'rgba(88,166,255,0.1)'}}] }},
  options:{{ ...chartDefaults, plugins:{{legend:{{display:false}},tooltip:{{mode:'index',intersect:false}}}} }}
}});

const pnlData = {pnl_by_trade};
new Chart(document.getElementById('pnlChart'), {{
  type:'bar',
  data:{{ labels:pnlData.map((_,i)=>i+1), datasets:[{{data:pnlData,backgroundColor:pnlData.map(v=>v>=0?'rgba(63,185,80,0.7)':'rgba(248,81,73,0.7)')}}] }},
  options:chartDefaults
}});

new Chart(document.getElementById('exitChart'), {{
  type:'doughnut',
  data:{{ labels:{exit_labels}, datasets:[{{data:{exit_values},backgroundColor:['#58a6ff','#3fb950','#f85149','#d29922','#8b949e','#bc8cff','#f0883e','#56d364']}}] }},
  options:{{ responsive:true, plugins:{{legend:{{position:'right',labels:{{color:'#c9d1d9',font:{{size:11}}}}}}}} }}
}});

new Chart(document.getElementById('regimeChart'), {{
  type:'bar',
  data:{{ labels:{regime_labels}, datasets:[
    {{label:'Win Rate %',data:{regime_wr},backgroundColor:'rgba(63,185,80,0.7)',yAxisID:'y'}},
    {{label:'Trades',data:{regime_counts},backgroundColor:'rgba(88,166,255,0.4)',yAxisID:'y1'}}
  ] }},
  options:{{ ...chartDefaults, plugins:{{legend:{{display:true,labels:{{color:'#c9d1d9'}}}}}}, scales:{{...chartDefaults.scales, y1:{{position:'right',ticks:{{color:'#8b949e'}},grid:{{display:false}}}}}} }}
}});

const durData = {candles_held};
const durBins = {{}};
durData.forEach(v => {{ durBins[v] = (durBins[v]||0)+1; }});
const durLabels = Object.keys(durBins).sort((a,b)=>a-b);
new Chart(document.getElementById('durationChart'), {{
  type:'bar',
  data:{{ labels:durLabels.map(l=>l+' candles'), datasets:[{{data:durLabels.map(l=>durBins[l]),backgroundColor:'rgba(188,140,255,0.7)'}}] }},
  options:chartDefaults
}});

new Chart(document.getElementById('signalChart'), {{
  type:'bar',
  data:{{ labels:['OBI','ROC'], datasets:[
    {{label:'Correct',data:[{signal_obi['correct']},{signal_roc['correct']}],backgroundColor:'rgba(63,185,80,0.7)'}},
    {{label:'Incorrect',data:[{signal_obi['incorrect']},{signal_roc['incorrect']}],backgroundColor:'rgba(248,81,73,0.7)'}},
    {{label:'Neutral',data:[{signal_obi['neutral']},{signal_roc['neutral']}],backgroundColor:'rgba(139,148,158,0.4)'}}
  ] }},
  options:{{ ...chartDefaults, plugins:{{legend:{{display:true,labels:{{color:'#c9d1d9'}}}}}}, scales:{{...chartDefaults.scales, x:{{stacked:true,...chartDefaults.scales.x}}, y:{{stacked:true,...chartDefaults.scales.y}}}} }}
}});
</script>
</body></html>"""


def _generate_walk_forward_report(data: dict) -> str:
    windows = data.get("windows", [])
    consistency = data.get("edge_consistency", 0)
    final_params = data.get("final_params", {})
    param_space = data.get("param_space", {})
    overfitting = data.get("overfitting_diagnosis") or {}

    ts = datetime.fromtimestamp(data.get("timestamp", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    window_labels = json.dumps([f"W{w['window_id']}" for w in windows])
    train_sharpes = json.dumps([w["train_sharpe"] for w in windows])
    test_sharpes = json.dumps([w["test_sharpe"] for w in windows])
    gaps = json.dumps([w["overfitting_gap"] for w in windows])

    window_rows = ""
    for w in windows:
        gap_class = "red" if w["overfitting_gap"] > 1.0 else ""
        window_rows += f"""<tr>
          <td>{w['window_id']}</td>
          <td>{w['train_sharpe']:.2f}</td>
          <td>{w['test_sharpe']:.2f}</td>
          <td>{w['test_win_rate']:.1%}</td>
          <td>{w['test_trades']}</td>
          <td class="{gap_class}">{w['overfitting_gap']:.2f}</td>
          <td><code>{json.dumps(w['best_params'])}</code></td>
        </tr>"""

    # Overfitting diagnosis section
    of_html = ""
    if overfitting:
        rec = overfitting.get("recommendation", "N/A")
        rec_color = "#3fb950" if "DEPLOY" in rec else "#f85149" if "ABANDON" in rec or "HIGH" in rec else "#d29922"
        of_html = f"""
<h2>Overfitting Diagnosis</h2>
<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:16px;">
  <div style="font-size:1.2rem;font-weight:600;color:{rec_color};margin-bottom:12px;">{rec}</div>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));">
    <div class="card"><div class="label">Avg Train Sharpe</div><div class="value blue">{overfitting.get('avg_train_sharpe',0):.2f}</div></div>
    <div class="card"><div class="label">Avg Test Sharpe</div><div class="value blue">{overfitting.get('avg_test_sharpe',0):.2f}</div></div>
    <div class="card"><div class="label">Avg Gap</div><div class="value {'red' if overfitting.get('high_overfitting') else 'green'}">{overfitting.get('avg_overfitting_gap',0):.2f}</div></div>
    <div class="card"><div class="label">% Profitable</div><div class="value {'green' if overfitting.get('pct_windows_profitable',0)>0.5 else 'red'}">{overfitting.get('pct_windows_profitable',0):.1%}</div></div>
    <div class="card"><div class="label">Edge Confirmed</div><div class="value {'green' if overfitting.get('edge_confirmed') else 'red'}">{'YES' if overfitting.get('edge_confirmed') else 'NO'}</div></div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KBTC Walk-Forward Report — {ts}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0d1117; color:#c9d1d9; padding:24px; }}
  h1 {{ font-size:1.5rem; margin-bottom:4px; color:#58a6ff; }}
  h2 {{ font-size:1.1rem; margin:24px 0 12px; color:#8b949e; border-bottom:1px solid #21262d; padding-bottom:6px; }}
  .meta {{ color:#8b949e; font-size:0.85rem; margin-bottom:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:12px; margin-bottom:20px; }}
  .card {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:14px; }}
  .card .label {{ font-size:0.75rem; color:#8b949e; text-transform:uppercase; }}
  .card .value {{ font-size:1.4rem; font-weight:600; margin-top:4px; }}
  .green {{ color:#3fb950; }} .red {{ color:#f85149; }} .blue {{ color:#58a6ff; }}
  .chart-container {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:16px; margin-bottom:16px; }}
  canvas {{ max-height:300px; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.8rem; }}
  th {{ text-align:left; padding:8px; background:#161b22; color:#8b949e; border-bottom:1px solid #21262d; }}
  td {{ padding:6px 8px; border-bottom:1px solid #21262d; }}
  .table-wrap {{ max-height:400px; overflow-y:auto; background:#0d1117; border:1px solid #21262d; border-radius:8px; }}
  code {{ font-size:0.75rem; color:#d29922; }}
  .config {{ background:#161b22; border:1px solid #21262d; border-radius:8px; padding:12px; font-family:monospace; font-size:0.85rem; white-space:pre-wrap; }}
</style>
</head>
<body>
<h1>KBTC Walk-Forward Optimization Report</h1>
<div class="meta">{ts} &middot; {len(windows)} windows &middot; Objective: {data.get('objective','sharpe_ratio')} &middot; {data.get('elapsed_sec',0):.1f}s</div>

<div class="grid">
  <div class="card"><div class="label">Edge Consistency</div><div class="value {'green' if consistency>=0.5 else 'red'}">{consistency:.1%}</div></div>
  <div class="card"><div class="label">Windows</div><div class="value blue">{len(windows)}</div></div>
  <div class="card"><div class="label">Avg Test Sharpe</div><div class="value blue">{sum(w['test_sharpe'] for w in windows)/max(len(windows),1):.2f}</div></div>
  <div class="card"><div class="label">Avg Test WR</div><div class="value blue">{sum(w['test_win_rate'] for w in windows)/max(len(windows),1):.1%}</div></div>
</div>

{of_html}

<h2>Recommended Parameters</h2>
<div class="config">{json.dumps(final_params, indent=2) if final_params else 'No parameters met the consistency threshold.'}</div>

<div class="row">
<div>
  <h2>Sharpe: Train vs Test</h2>
  <div class="chart-container"><canvas id="sharpeChart"></canvas></div>
</div>
<div>
  <h2>Overfitting Gap</h2>
  <div class="chart-container"><canvas id="gapChart"></canvas></div>
</div>
</div>

<h2>Window Details</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Window</th><th>Train Sharpe</th><th>Test Sharpe</th><th>Test WR</th><th>Trades</th><th>Gap</th><th>Params</th></tr></thead>
<tbody>{window_rows}</tbody>
</table>
</div>

<h2>Parameter Space</h2>
<div class="config">{json.dumps(param_space, indent=2)}</div>

<script>
const chartDefaults = {{ responsive:true, maintainAspectRatio:false, scales:{{x:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}} }};
Chart.defaults.color = '#c9d1d9';

new Chart(document.getElementById('sharpeChart'), {{
  type:'bar',
  data:{{ labels:{window_labels}, datasets:[
    {{label:'Train',data:{train_sharpes},backgroundColor:'rgba(88,166,255,0.6)'}},
    {{label:'Test',data:{test_sharpes},backgroundColor:'rgba(63,185,80,0.6)'}}
  ] }},
  options:{{ ...chartDefaults, plugins:{{legend:{{display:true,labels:{{color:'#c9d1d9'}}}}}} }}
}});

new Chart(document.getElementById('gapChart'), {{
  type:'bar',
  data:{{ labels:{window_labels}, datasets:[{{data:{gaps},backgroundColor:{json.dumps([('rgba(248,81,73,0.7)' if g>1 else 'rgba(139,148,158,0.5)') for g in [w['overfitting_gap'] for w in windows]])}}}] }},
  options:{{ ...chartDefaults, plugins:{{legend:{{display:false}}}} }}
}});
</script>
</body></html>"""


def _signal_accuracy(signal_log: list[dict], trades: list[dict], signal_key: str) -> dict:
    """Compute how often a signal direction matched the trade outcome."""
    correct = incorrect = neutral = 0
    trade_outcomes = {}
    for t in trades:
        trade_outcomes[t["timestamp"]] = "long" if t["pnl"] > 0 and t["direction"] == "long" else \
                                          "short" if t["pnl"] > 0 and t["direction"] == "short" else \
                                          "wrong"

    for sig in signal_log:
        direction = sig.get(signal_key, "neutral")
        if direction == "neutral":
            neutral += 1
            continue
        ts = sig.get("timestamp")
        if ts in trade_outcomes:
            outcome_dir = trade_outcomes[ts]
            if outcome_dir == direction:
                correct += 1
            elif outcome_dir == "wrong":
                incorrect += 1
            else:
                incorrect += 1
        else:
            neutral += 1

    return {"correct": correct, "incorrect": incorrect, "neutral": neutral}


def _trade_table_rows(trades: list[dict]) -> str:
    rows = ""
    for i, t in enumerate(trades, 1):
        pnl_class = "green" if t["pnl"] >= 0 else "red"
        rows += f"""<tr>
          <td>{i}</td>
          <td>{t['direction']}</td>
          <td>${t['entry_price']:,.2f}</td>
          <td>${t['exit_price']:,.2f}</td>
          <td class="{pnl_class}">${t['pnl']:+,.4f}</td>
          <td class="{pnl_class}">{t['pnl_pct']:+.4f}</td>
          <td>${t['fees']:.4f}</td>
          <td>{t['exit_reason']}</td>
          <td>{t['conviction']}</td>
          <td>{t['regime_at_entry']}</td>
          <td>{t['candles_held']}</td>
        </tr>"""
    return rows
