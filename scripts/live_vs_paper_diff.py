"""
Live-vs-paper diff analysis (Tier 0a) — diagnose the live PnL gap.

Background
----------
Bot bankroll dropped from $1000 to $35 (96% drawdown) over 34 live trades.
Paper trading the SAME strategy on the SAME days is profitable. We need
to understand the gap before adding any new strategy work.

Hypotheses (in priority order, derived from a survey of trades / kalshi_trades
/ ob_snapshots schemas):

  H1: PnL accounting bug. The `pnl` column understates wins. Where the
      reconciliation column `wallet_pnl` exists (only on `fill_source=fill_ws`
      trades, ~6 of 34), the average drift is ~$0.49 per trade and is
      always positive, suggesting recorded PnL is systematically too negative.

  H2: EXPIRY_409 dominance. 12 of 34 live trades exit via EXPIRY_409_SETTLED
      (we couldn't get out, contract expired). None have wallet reconciliation.
      Need to know whether these are "didn't try" vs "tried and failed".

  H3: Entry slippage. For trades with order book snapshots within +/-60s of
      entry, compare entry_price to observed mid.

  H4: Size-vs-liquidity. our_contracts / total_book_depth_within_2c_of_entry.
      Values >> 1 mean we ate the entire offer.

What this script does NOT do
----------------------------
  * Replay live trades through PaperTrader.exit(). Doing so would compound
    the suspected accounting bug (H1) since PaperTrader uses the same pnl
    formula. We need to fix accounting first; replay can come in Tier 1.
  * Modify any data. Read-only against trades / kalshi_trades / ob_snapshots
    / signal_log.

Output
------
  backend/backtest_reports/live_vs_paper_<ts>.json   structured data
  backend/backtest_reports/live_vs_paper_<ts>.md     human-readable report

Usage
-----
  # Local (uses host postgres mapped to 5433)
  DATABASE_URL=postgresql://kalshi:kalshi_secret@localhost:5433/kbtc \\
    python3 scripts/live_vs_paper_diff.py

  # On droplet (run inside a docker container with the network available)
  docker run --rm --network kbtc_kbtc-net \\
    -v /home/botuser/kbtc:/work -w /work \\
    -e DATABASE_URL=postgresql://kalshi:kalshi_secret@db:5432/kbtc \\
    --entrypoint python kbtc-bot:latest scripts/live_vs_paper_diff.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import psycopg
except ImportError:
    print("ERROR: psycopg not installed. Run inside the kbtc-bot container.", file=sys.stderr)
    sys.exit(1)


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_DEFAULT_REPORT_DIR = _REPO_ROOT / "backend" / "backtest_reports"


@dataclass
class TradeRow:
    id: int
    timestamp: datetime
    ticker: str
    direction: str
    contracts: int
    entry_price: float
    exit_price: Optional[float]
    pnl: Optional[float]
    wallet_pnl: Optional[float]
    pnl_drift: Optional[float]
    entry_cost_dollars: Optional[float]
    exit_cost_dollars: Optional[float]
    fill_source: Optional[str]
    exit_reason: Optional[str]
    closed_at: Optional[datetime]


@dataclass
class TradeAnalysis:
    trade_id: int
    ticker: str
    timestamp_iso: str
    exit_reason: str
    contracts: int
    entry_price_cents: float
    exit_price_cents: Optional[float]
    recorded_pnl: Optional[float]
    wallet_pnl: Optional[float]
    drift_known: Optional[float]
    drift_inferred: Optional[float] = None
    entry_mid_cents: Optional[float] = None
    entry_slippage_cents: Optional[float] = None
    book_depth_at_entry: Optional[float] = None
    size_vs_depth_ratio: Optional[float] = None
    notes: list[str] = field(default_factory=list)


def fetch_live_trades(conn) -> list[TradeRow]:
    sql = """
        SELECT id, timestamp, ticker, direction, contracts,
               entry_price, exit_price, pnl, wallet_pnl, pnl_drift,
               entry_cost_dollars, exit_cost_dollars,
               fill_source, exit_reason, closed_at
        FROM trades
        WHERE trading_mode = 'live'
        ORDER BY timestamp
    """
    rows: list[TradeRow] = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for r in cur.fetchall():
            rows.append(TradeRow(
                id=r[0], timestamp=r[1], ticker=r[2], direction=r[3],
                contracts=r[4],
                entry_price=float(r[5]) if r[5] is not None else 0.0,
                exit_price=float(r[6]) if r[6] is not None else None,
                pnl=float(r[7]) if r[7] is not None else None,
                wallet_pnl=float(r[8]) if r[8] is not None else None,
                pnl_drift=float(r[9]) if r[9] is not None else None,
                entry_cost_dollars=float(r[10]) if r[10] is not None else None,
                exit_cost_dollars=float(r[11]) if r[11] is not None else None,
                fill_source=r[12], exit_reason=r[13], closed_at=r[14],
            ))
    return rows


def fetch_ob_snapshot_at(conn, ticker: str, ts: datetime, window_seconds: int = 60) -> Optional[dict]:
    """Return the snapshot CLOSEST to `ts` within the window, or None."""
    sql = """
        SELECT timestamp, bids, asks, total_bid_vol, total_ask_vol, spread_cents
        FROM ob_snapshots
        WHERE ticker = %s
          AND timestamp BETWEEN %s - INTERVAL '%s seconds' AND %s + INTERVAL '%s seconds'
        ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s)))
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (ticker, ts, window_seconds, ts, window_seconds, ts))
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "timestamp": row[0],
        "bids": row[1],
        "asks": row[2],
        "total_bid_vol": float(row[3]) if row[3] is not None else None,
        "total_ask_vol": float(row[4]) if row[4] is not None else None,
        "spread_cents": int(row[5]) if row[5] is not None else None,
    }


def best_bid_ask_from_snapshot(snap: dict) -> tuple[Optional[float], Optional[float]]:
    """Order book bids/asks JSONB shape: list of [price_cents, size] pairs.

    For a YES-side BTC contract, asks are sorted ascending by price (best ask
    is the lowest price someone will sell at) and bids descending (best bid
    is the highest someone will buy at). We return them as cents (e.g. 24.0).
    """
    bids = snap.get("bids") or []
    asks = snap.get("asks") or []
    best_bid = None
    best_ask = None
    if bids:
        try:
            best_bid = float(bids[0][0])
        except (IndexError, TypeError, ValueError):
            pass
    if asks:
        try:
            best_ask = float(asks[0][0])
        except (IndexError, TypeError, ValueError):
            pass
    return best_bid, best_ask


def book_depth_within_window(snap: dict, side: str, ref_price: float, window_cents: float = 2.0) -> float:
    """Sum the contract size on `side` within `window_cents` of `ref_price`."""
    book = snap.get("asks") if side == "ask" else snap.get("bids")
    if not book:
        return 0.0
    total = 0.0
    for level in book:
        try:
            price = float(level[0])
            size = float(level[1])
        except (IndexError, TypeError, ValueError):
            continue
        if abs(price - ref_price) <= window_cents:
            total += size
    return total


def analyze_trade(conn, t: TradeRow, drift_avg_known: Optional[float]) -> TradeAnalysis:
    a = TradeAnalysis(
        trade_id=t.id,
        ticker=t.ticker,
        timestamp_iso=t.timestamp.isoformat(),
        exit_reason=t.exit_reason or "UNKNOWN",
        contracts=t.contracts,
        entry_price_cents=t.entry_price,
        exit_price_cents=t.exit_price,
        recorded_pnl=t.pnl,
        wallet_pnl=t.wallet_pnl,
        drift_known=t.pnl_drift,
    )

    if t.pnl_drift is None and drift_avg_known is not None:
        a.drift_inferred = round(drift_avg_known, 4)
        a.notes.append(
            f"wallet_pnl unknown ({t.fill_source}); drift inferred from "
            f"{abs(drift_avg_known):.2f} avg of trades that have wallet data"
        )

    snap = fetch_ob_snapshot_at(conn, t.ticker, t.timestamp, window_seconds=60)
    if snap is None:
        a.notes.append("no ob_snapshot within +/-60s of entry")
        return a

    best_bid, best_ask = best_bid_ask_from_snapshot(snap)
    if best_bid is None or best_ask is None:
        a.notes.append("ob_snapshot present but missing best bid/ask")
        return a

    mid = (best_bid + best_ask) / 2
    a.entry_mid_cents = round(mid, 2)
    if t.direction == "long":
        a.entry_slippage_cents = round(t.entry_price - best_ask, 2)
        depth = book_depth_within_window(snap, "ask", best_ask, window_cents=2.0)
    else:
        a.entry_slippage_cents = round(best_bid - t.entry_price, 2)
        depth = book_depth_within_window(snap, "bid", best_bid, window_cents=2.0)

    a.book_depth_at_entry = round(depth, 1)
    if depth > 0:
        a.size_vs_depth_ratio = round(t.contracts / depth, 3)
    return a


def summarize(analyses: list[TradeAnalysis], trades: list[TradeRow]) -> dict:
    n = len(analyses)
    recorded_total = sum(t.pnl for t in trades if t.pnl is not None)
    wallet_known_total = sum(t.wallet_pnl for t in trades if t.wallet_pnl is not None)
    drift_known_total = sum(t.pnl_drift for t in trades if t.pnl_drift is not None)
    n_with_wallet = sum(1 for t in trades if t.wallet_pnl is not None)
    n_with_drift = sum(1 for t in trades if t.pnl_drift is not None)

    drift_avg = drift_known_total / n_with_drift if n_with_drift else 0
    drift_extrapolated_total = drift_avg * (n - n_with_drift)
    inferred_wallet_total = recorded_total + drift_known_total + drift_extrapolated_total

    by_reason: dict[str, dict] = {}
    for t in trades:
        r = t.exit_reason or "UNKNOWN"
        bucket = by_reason.setdefault(r, {
            "n": 0, "recorded_pnl_total": 0.0,
            "wallet_pnl_total_known": 0.0, "n_with_wallet": 0,
            "drift_total_known": 0.0, "n_with_drift": 0,
        })
        bucket["n"] += 1
        bucket["recorded_pnl_total"] += t.pnl or 0
        if t.wallet_pnl is not None:
            bucket["wallet_pnl_total_known"] += t.wallet_pnl
            bucket["n_with_wallet"] += 1
        if t.pnl_drift is not None:
            bucket["drift_total_known"] += t.pnl_drift
            bucket["n_with_drift"] += 1

    for r, b in by_reason.items():
        b["recorded_pnl_total"] = round(b["recorded_pnl_total"], 4)
        b["wallet_pnl_total_known"] = round(b["wallet_pnl_total_known"], 4)
        b["drift_total_known"] = round(b["drift_total_known"], 4)
        b["recorded_avg_pnl"] = round(b["recorded_pnl_total"] / b["n"], 4) if b["n"] else 0
        if b["n_with_wallet"] > 0:
            b["wallet_avg_pnl_known"] = round(b["wallet_pnl_total_known"] / b["n_with_wallet"], 4)
        else:
            b["wallet_avg_pnl_known"] = None

    slippages = [a.entry_slippage_cents for a in analyses if a.entry_slippage_cents is not None]
    size_ratios = [a.size_vs_depth_ratio for a in analyses if a.size_vs_depth_ratio is not None]

    expiry_409_n = sum(1 for t in trades if t.exit_reason == "EXPIRY_409_SETTLED")

    return {
        "trade_count": n,
        "recorded_pnl_total": round(recorded_total, 4),
        "wallet_pnl_total_known": round(wallet_known_total, 4),
        "drift_total_known": round(drift_known_total, 4),
        "drift_avg_per_trade_with_data": round(drift_avg, 4),
        "trades_with_wallet_pnl": n_with_wallet,
        "trades_with_drift": n_with_drift,
        "trades_without_wallet_data": n - n_with_wallet,
        "drift_extrapolated_to_unknown_trades": round(drift_extrapolated_total, 4),
        "inferred_wallet_pnl_total": round(inferred_wallet_total, 4),
        "by_exit_reason": by_reason,
        "expiry_409_count": expiry_409_n,
        "expiry_409_pct": round(100 * expiry_409_n / n, 1) if n else 0,
        "entry_slippage_cents": {
            "n": len(slippages),
            "avg": round(statistics.mean(slippages), 3) if slippages else None,
            "median": round(statistics.median(slippages), 3) if slippages else None,
            "max": round(max(slippages), 3) if slippages else None,
            "min": round(min(slippages), 3) if slippages else None,
        },
        "size_vs_depth": {
            "n": len(size_ratios),
            "avg": round(statistics.mean(size_ratios), 3) if size_ratios else None,
            "median": round(statistics.median(size_ratios), 3) if size_ratios else None,
            "max": round(max(size_ratios), 3) if size_ratios else None,
            "trades_above_1x": sum(1 for r in size_ratios if r > 1),
        },
    }


def render_markdown(summary: dict, analyses: list[TradeAnalysis]) -> str:
    lines = []
    lines.append("# Live-vs-paper diff report\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"Trades analyzed: **{summary['trade_count']}**\n")
    lines.append("\n## Headline\n")
    rec = summary["recorded_pnl_total"]
    inf = summary["inferred_wallet_pnl_total"]
    lines.append(f"- **Recorded total PnL** (`trades.pnl` sum): **${rec:+.2f}**\n")
    lines.append(f"- **Wallet PnL on the {summary['trades_with_wallet_pnl']} trades that have it**: **${summary['wallet_pnl_total_known']:+.2f}**\n")
    lines.append(f"- **Inferred true total** (extrapolating drift to NULL rows): **${inf:+.2f}**\n")
    lines.append(f"- **Drift gap so far**: **${summary['drift_total_known']:+.2f}** across {summary['trades_with_drift']} reconciled trades")
    lines.append(f" (avg ${summary['drift_avg_per_trade_with_data']:+.3f}/trade)\n")
    lines.append("\n## H1: PnL accounting (the big one)\n")
    lines.append("Where we have wallet reconciliation, recorded `pnl` differs from `wallet_pnl` by:\n")
    lines.append("\n| exit_reason | n | with_wallet | recorded_avg | wallet_avg_known | drift_total |")
    lines.append("\n|---|---|---|---|---|---|")
    for reason, b in sorted(summary["by_exit_reason"].items(),
                            key=lambda kv: kv[1]["drift_total_known"], reverse=True):
        wal = f"${b['wallet_avg_pnl_known']:+.3f}" if b["wallet_avg_pnl_known"] is not None else "—"
        lines.append(f"\n| {reason} | {b['n']} | {b['n_with_wallet']} | "
                     f"${b['recorded_avg_pnl']:+.3f} | {wal} | "
                     f"${b['drift_total_known']:+.3f} |")
    lines.append("\n\n## H2: EXPIRY_409 dominance\n")
    lines.append(f"- **{summary['expiry_409_count']} of {summary['trade_count']} live trades** ({summary['expiry_409_pct']}%) exit via EXPIRY_409_SETTLED\n")
    lines.append("- These have no wallet reconciliation (couldn't issue a sell order before expiry)\n")
    lines.append("- Need to determine: 'didn't try' vs 'tried and Kalshi rejected with 409'\n")
    s = summary["entry_slippage_cents"]
    lines.append("\n## H3: Entry slippage (vs order book mid)\n")
    if s["n"] == 0:
        lines.append("- **No slippage data** — no trades had ob_snapshots within +/-60s of entry\n")
    else:
        lines.append(f"- N = {s['n']} trades with order book context at entry\n")
        lines.append(f"- Average slippage: **{s['avg']:+.2f} cents** (positive = paid above mid)\n")
        lines.append(f"- Median: {s['median']:+.2f}c, Min: {s['min']:+.2f}c, Max: {s['max']:+.2f}c\n")
    sr = summary["size_vs_depth"]
    lines.append("\n## H4: Size vs liquidity\n")
    if sr["n"] == 0:
        lines.append("- **No depth data** for any traded entry window\n")
    else:
        lines.append(f"- N = {sr['n']} trades with depth context\n")
        lines.append(f"- Median size/depth ratio: **{sr['median']}** "
                     f"(values > 1 mean we ate the entire near-best-price stack)\n")
        lines.append(f"- Max ratio: {sr['max']}, Trades >1x: {sr['trades_above_1x']}\n")
    lines.append("\n## Per-trade detail (worst drifts first)\n")
    lines.append("\n| ticker | exit | contracts | entry | exit | recorded | wallet | drift | slippage | depth | notes |")
    lines.append("\n|---|---|---|---|---|---|---|---|---|---|---|")
    for a in sorted(analyses, key=lambda x: -(x.drift_known or x.drift_inferred or 0)):
        wal = f"${a.wallet_pnl:+.3f}" if a.wallet_pnl is not None else "—"
        dr = a.drift_known if a.drift_known is not None else a.drift_inferred
        dr_s = f"${dr:+.3f}" if dr is not None else "—"
        slp = f"{a.entry_slippage_cents:+.1f}c" if a.entry_slippage_cents is not None else "—"
        dp = f"{a.book_depth_at_entry}" if a.book_depth_at_entry is not None else "—"
        rec = f"${a.recorded_pnl:+.3f}" if a.recorded_pnl is not None else "—"
        ex = f"{a.exit_price_cents:.0f}" if a.exit_price_cents is not None else "—"
        notes = "; ".join(a.notes)[:60]
        lines.append(f"\n| {a.ticker[-12:]} | {a.exit_reason} | {a.contracts} | "
                     f"{a.entry_price_cents:.0f} | {ex} | {rec} | {wal} | {dr_s} | "
                     f"{slp} | {dp} | {notes} |")
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-vs-paper diff analysis")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL"),
                        help="Postgres URL (defaults to $DATABASE_URL)")
    parser.add_argument("--out-dir", default=str(_DEFAULT_REPORT_DIR),
                        help="Where to write the report files")
    args = parser.parse_args()
    if not args.db_url:
        print("ERROR: provide --db-url or set DATABASE_URL", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"live_vs_paper_{stamp}.json"
    md_path = out_dir / f"live_vs_paper_{stamp}.md"

    conn = psycopg.connect(args.db_url)
    try:
        trades = fetch_live_trades(conn)
        if not trades:
            print("No live trades found.")
            return 0

        drift_known = [t.pnl_drift for t in trades if t.pnl_drift is not None]
        drift_avg = statistics.mean(drift_known) if drift_known else None

        analyses = [analyze_trade(conn, t, drift_avg) for t in trades]
        summary = summarize(analyses, trades)
    finally:
        conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "per_trade": [asdict(a) for a in analyses],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    md_path.write_text(render_markdown(summary, analyses))

    print(f"Report written to:")
    print(f"  {json_path}")
    print(f"  {md_path}")
    print()
    print("=== HEADLINE ===")
    print(f"  Trades analyzed:           {summary['trade_count']}")
    print(f"  Recorded PnL total:        ${summary['recorded_pnl_total']:+.2f}")
    print(f"  Wallet PnL (known {summary['trades_with_wallet_pnl']}):       ${summary['wallet_pnl_total_known']:+.2f}")
    print(f"  Drift total (known):       ${summary['drift_total_known']:+.2f}")
    print(f"  Avg drift / known trade:   ${summary['drift_avg_per_trade_with_data']:+.3f}")
    print(f"  Inferred true total:       ${summary['inferred_wallet_pnl_total']:+.2f}")
    print(f"  EXPIRY_409 trades:         {summary['expiry_409_count']} ({summary['expiry_409_pct']}%)")
    if summary["entry_slippage_cents"]["n"] > 0:
        s = summary["entry_slippage_cents"]
        print(f"  Entry slippage avg:        {s['avg']:+.2f} cents (n={s['n']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
