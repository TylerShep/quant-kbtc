"""
Performance attribution — decompose PnL into signal, regime, session,
execution, and exit-reason components.
Per the performance-attribution skill.
"""
from __future__ import annotations

from datetime import datetime, timezone


SESSIONS = {
    "ASIA":     (0, 8),
    "LONDON":   (8, 13),
    "US_OPEN":  (13, 15),
    "US_MAIN":  (15, 20),
    "US_CLOSE": (20, 24),
}


def _assign_session(ts: float) -> str:
    """Map a unix timestamp to a trading session name."""
    hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
    for session, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return session
    return "UNKNOWN"


def run_attribution(trades: list[dict]) -> dict:
    """Full PnL attribution from trade list.

    Each trade dict should include at minimum:
        pnl, pnl_pct, fees, direction, conviction, regime_at_entry,
        exit_reason, candles_held, timestamp, exit_timestamp
    """
    if not trades:
        return {"total_pnl_dollars": 0, "total_trades": 0}

    total_pnl = sum(t.get("pnl", 0) for t in trades)

    return {
        "total_pnl_dollars": round(total_pnl, 2),
        "total_trades": len(trades),
        "signal_attribution": _signal_attribution(trades, total_pnl),
        "regime_attribution": _regime_attribution(trades),
        "session_attribution": _session_attribution(trades),
        "execution_attribution": _execution_attribution(trades, total_pnl),
        "exit_reason_breakdown": _exit_reason_attribution(trades),
    }


def _signal_attribution(trades: list[dict], total_pnl: float) -> dict:
    results: dict = {}

    for conviction in ("HIGH", "NORMAL", "LOW"):
        subset = [t for t in trades if t.get("conviction") == conviction]
        if not subset:
            continue
        subset_pnl = sum(t["pnl"] for t in subset)
        wins = [t for t in subset if t["pnl"] > 0]
        results[conviction] = {
            "trades": len(subset),
            "pnl_dollars": round(subset_pnl, 2),
            "avg_pnl_pct": round(
                sum(t["pnl_pct"] for t in subset) / len(subset) * 100, 3
            ),
            "win_rate": round(len(wins) / len(subset), 3),
            "pnl_share_pct": round(subset_pnl / total_pnl * 100, 1) if total_pnl != 0 else 0,
        }

    for direction in ("long", "short"):
        subset = [t for t in trades if t.get("direction") == direction]
        if not subset:
            continue
        wins = [t for t in subset if t["pnl"] > 0]
        results[f"direction_{direction}"] = {
            "trades": len(subset),
            "pnl_dollars": round(sum(t["pnl"] for t in subset), 2),
            "win_rate": round(len(wins) / len(subset), 3),
        }

    return results


def _regime_attribution(trades: list[dict]) -> dict:
    results: dict = {}
    for regime in ("LOW", "MEDIUM", "HIGH", "UNKNOWN"):
        subset = [t for t in trades if t.get("regime_at_entry") == regime]
        if not subset:
            continue
        wins = [t for t in subset if t["pnl"] > 0]
        results[regime] = {
            "trades": len(subset),
            "pnl_dollars": round(sum(t["pnl"] for t in subset), 2),
            "win_rate": round(len(wins) / len(subset), 3),
            "avg_hold_candles": round(
                sum(t.get("candles_held", 0) for t in subset) / len(subset), 1
            ),
        }

    tradeable_regimes = [r for r in ("LOW", "MEDIUM") if r in results and results[r]["trades"] > 10]
    if tradeable_regimes:
        results["best_regime"] = max(tradeable_regimes, key=lambda r: results[r].get("win_rate", 0))

    return results


def _session_attribution(trades: list[dict]) -> dict:
    results: dict = {}
    for t in trades:
        ts = t.get("timestamp", 0)
        if isinstance(ts, str):
            continue
        session = _assign_session(ts)
        if session not in results:
            results[session] = {"trades": 0, "pnl": 0.0, "wins": 0, "pnl_pct_sum": 0.0}
        results[session]["trades"] += 1
        results[session]["pnl"] += t.get("pnl", 0)
        results[session]["pnl_pct_sum"] += t.get("pnl_pct", 0)
        if t.get("pnl", 0) > 0:
            results[session]["wins"] += 1

    return {
        session: {
            "trades": data["trades"],
            "pnl_dollars": round(data["pnl"], 2),
            "win_rate": round(data["wins"] / data["trades"], 3) if data["trades"] > 0 else 0,
            "avg_pnl_pct": round(data["pnl_pct_sum"] / data["trades"] * 100, 3) if data["trades"] > 0 else 0,
        }
        for session, data in results.items()
    }


def _execution_attribution(trades: list[dict], total_pnl: float) -> dict:
    total_fees = sum(t.get("fees", 0) for t in trades)
    theoretical_pnl = total_pnl + total_fees

    return {
        "total_fees_dollars": round(total_fees, 2),
        "theoretical_pnl": round(theoretical_pnl, 2),
        "actual_pnl": round(total_pnl, 2),
        "execution_drag": round(total_fees, 2),
        "fees_as_pct_of_gross": round(
            total_fees / theoretical_pnl * 100, 1
        ) if theoretical_pnl > 0 else 0,
    }


def _exit_reason_attribution(trades: list[dict]) -> dict:
    results: dict = {}
    for t in trades:
        reason = t.get("exit_reason", "UNKNOWN")
        if reason not in results:
            results[reason] = {"count": 0, "pnl_sum": 0.0, "pnl_pct_sum": 0.0}
        results[reason]["count"] += 1
        results[reason]["pnl_sum"] += t.get("pnl", 0)
        results[reason]["pnl_pct_sum"] += t.get("pnl_pct", 0)

    total = len(trades)
    return {
        reason: {
            "count": data["count"],
            "pct_of_all": round(data["count"] / total * 100, 1) if total > 0 else 0,
            "avg_pnl_pct": round(data["pnl_pct_sum"] / data["count"] * 100, 3) if data["count"] > 0 else 0,
            "pnl_dollars": round(data["pnl_sum"], 2),
        }
        for reason, data in results.items()
    }
