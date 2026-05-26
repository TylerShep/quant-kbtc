from __future__ import annotations

from typing import Iterable

from backtesting.contract_backtester import ContractBacktester, SimPosition
from backtesting.contract_timeline import ContractTick, ContractTimeline
from config import settings


def _set_bot(name: str, value):
    original = getattr(settings.bot, name)
    object.__setattr__(settings.bot, name, value)
    return original


def _make_candles(count: int, start_ts: float = 0.0, step: float = 900.0) -> list[dict]:
    candles: list[dict] = []
    price = 50000.0
    for i in range(count):
        candles.append(
            {
                "timestamp": start_ts + i * step,
                "open": price,
                "high": price + 20.0,
                "low": price - 20.0,
                "close": price + (i % 3) * 5.0,
                "volume": 1000.0,
            }
        )
    return candles


def _timeline(
    ticker: str,
    rows: Iterable[tuple[float, float | None, float | None, float | None, float]],
    *,
    close_time: float | None = None,
    result: str | None = None,
    expiration_value: float | None = None,
) -> ContractTimeline:
    timeline = ContractTimeline(
        ticker=ticker,
        close_time=close_time,
        result=result,
        expiration_value=expiration_value,
    )
    for ts, bid, ask, mid, obi in rows:
        spread = None
        if bid is not None and ask is not None:
            spread = ask - bid
        timeline.add_tick(
            ContractTick(
                timestamp=ts,
                ticker=ticker,
                mid_cents=mid,
                best_bid=bid,
                best_ask=ask,
                spread_cents=spread,
                obi=obi,
                total_bid_vol=1000.0,
                total_ask_vol=800.0,
                source="ob_mid",
            )
        )
    timeline.finalize()
    return timeline


def _run(
    timeline: ContractTimeline,
    *,
    candles: list[dict] | None = None,
    config: dict | None = None,
    settlement_data: dict | None = None,
):
    candles = candles or _make_candles(8)
    bt = ContractBacktester(
        candles=candles,
        contract_timelines={timeline.ticker: timeline},
        config=config or {"mode": "paper"},
        settlement_data=settlement_data or {},
    )
    result = bt.run(bankroll=10000.0)
    return bt, result


def test_contract_pnl_pct_uses_cents():
    pos = SimPosition(
        position_uid="u1",
        ticker="KXBTC-TEST",
        direction="long",
        contracts=10,
        entry_price=20.0,
        entry_timestamp=0.0,
        conviction="NORMAL",
        regime_at_entry="MEDIUM",
        entry_obi=0.7,
        entry_roc=0.1,
        signal_driver="OBI",
    )
    assert ContractBacktester._calc_pnl_pct(pos, 18.0) == -0.1


def test_hard_stop_fires_on_contract_move():
    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 19.0, 21.0, 20.0, 0.8),  # entry
            (1860.0, 17.0, 19.0, 18.0, 0.8),  # hard-stop
            (2700.0, 17.0, 19.0, 18.0, 0.8),
        ],
        close_time=7200.0,
    )
    bt, _ = _run(
        tl,
        config={
            "mode": "paper",
            "hard_stop_loss_pct": 0.10,
            "paper_reentry_cooldown_sec": 0.0,
            "paper_same_side_cooldown_sec": 0.0,
        },
    )
    assert bt.trades
    assert bt.trades[0]["exit_reason"] == "HARD_STOP_LOSS"
    assert bt.trades[0]["pnl_pct"] <= -0.10


def test_expiry_guard_holds_winner():
    ticker = "KXBTC-26MAY0614-B50000"
    trigger = settings.bot.expiry_guard_trigger_sec
    close_time = 2000.0
    ts_guard = close_time - (trigger - 1)
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1200.0, 20.0, 22.0, 21.0, 0.8),  # entry
            (ts_guard, 80.0, 82.0, 81.0, 0.8),  # near-expiry winner
        ],
        close_time=close_time,
    )
    bt, _ = _run(
        tl,
        config={
            "mode": "paper",
            "hard_stop_loss_pct": 0.0,
        },
    )
    assert bt.trades
    assert bt.trades[0]["exit_reason"] == "EXPIRY_GUARD"
    assert bt.trades[0]["pnl"] > 0


def test_contract_settled_pays_dollar():
    ticker = "KXBTC-26MAY0614-B50000"
    candles = _make_candles(6)

    tl_yes = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1200.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 20.0, 22.0, 21.0, 0.8),
        ],
        close_time=1800.0,
    )
    bt_yes, _ = _run(
        tl_yes,
        candles=candles,
        config={"mode": "paper", "hard_stop_loss_pct": 0.0},
        settlement_data={
            ticker: {"close_time": 1800.0, "result": "yes", "expiration_value": 100.0}
        },
    )
    assert bt_yes.trades
    assert bt_yes.trades[0]["exit_reason"] == "CONTRACT_SETTLED"
    assert bt_yes.trades[0]["exit_price"] == 100.0

    tl_no = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1200.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 20.0, 22.0, 21.0, 0.8),
        ],
        close_time=1800.0,
    )
    bt_no, _ = _run(
        tl_no,
        candles=candles,
        config={"mode": "paper", "hard_stop_loss_pct": 0.0},
        settlement_data={
            ticker: {"close_time": 1800.0, "result": "no", "expiration_value": 0.0}
        },
    )
    assert bt_no.trades
    assert bt_no.trades[0]["exit_reason"] == "CONTRACT_SETTLED"
    assert bt_no.trades[0]["exit_price"] == 0.0


def test_per_pair_cooldown_blocks_reentry():
    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 19.0, 21.0, 20.0, 0.8),  # entry
            (1810.0, 17.0, 19.0, 18.0, 0.8),  # hard stop
            (1820.0, 19.0, 21.0, 20.0, 0.8),
            (1830.0, 19.0, 21.0, 20.0, 0.8),
            (1840.0, 19.0, 21.0, 20.0, 0.8),  # would re-enter without cooldown
        ],
        close_time=7200.0,
    )
    bt, _ = _run(
        tl,
        config={
            "mode": "paper",
            "hard_stop_loss_pct": 0.10,
            "paper_reentry_cooldown_sec": 0.0,
            "paper_same_side_cooldown_sec": 300.0,
        },
    )
    assert len(bt.trades) == 1


def test_health_score_decay_fires():
    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 19.0, 21.0, 20.0, 0.8),  # entry
            (1810.0, 19.0, 21.0, 20.0, 0.8),  # health check
        ],
        close_time=7200.0,
    )

    orig_shadow = _set_bot("exit_intelligence_shadow_only", False)
    orig_enabled = _set_bot("exit_intelligence_enabled", True)
    orig_thresh = _set_bot("health_score_threshold", 99.0)
    orig_ticks = _set_bot("health_score_breach_ticks", 1)
    orig_confirm = _set_bot("health_exit_confirmation_enabled", False)
    try:
        bt, _ = _run(
            tl,
            config={"mode": "paper", "hard_stop_loss_pct": 0.0},
        )
    finally:
        object.__setattr__(settings.bot, "exit_intelligence_shadow_only", orig_shadow)
        object.__setattr__(settings.bot, "exit_intelligence_enabled", orig_enabled)
        object.__setattr__(settings.bot, "health_score_threshold", orig_thresh)
        object.__setattr__(settings.bot, "health_score_breach_ticks", orig_ticks)
        object.__setattr__(settings.bot, "health_exit_confirmation_enabled", orig_confirm)

    assert bt.trades
    assert bt.trades[0]["exit_reason"] == "HEALTH_SCORE_DECAY"


def test_blended_price_falls_back_to_yes_price():
    ticker = "KXBTC-26MAY0614-B50000"
    timeline = ContractTimeline(ticker=ticker, close_time=7200.0)
    timeline.add_tick(
        ContractTick(
            timestamp=0.0,
            ticker=ticker,
            mid_cents=20.0,
            best_bid=None,
            best_ask=None,
            spread_cents=None,
            obi=0.8,
            total_bid_vol=600.0,
            total_ask_vol=500.0,
            source="yes_price",
        )
    )
    timeline.add_tick(
        ContractTick(
            timestamp=900.0,
            ticker=ticker,
            mid_cents=20.0,
            best_bid=None,
            best_ask=None,
            spread_cents=None,
            obi=0.8,
            total_bid_vol=600.0,
            total_ask_vol=500.0,
            source="yes_price",
        )
    )
    timeline.add_tick(
        ContractTick(
            timestamp=1800.0,
            ticker=ticker,
            mid_cents=20.0,
            best_bid=None,
            best_ask=None,
            spread_cents=None,
            obi=0.8,
            total_bid_vol=600.0,
            total_ask_vol=500.0,
            source="yes_price",
        )
    )
    timeline.add_tick(
        ContractTick(
            timestamp=2000.0,
            ticker=ticker,
            mid_cents=25.0,
            best_bid=None,
            best_ask=None,
            spread_cents=None,
            obi=0.8,
            total_bid_vol=600.0,
            total_ask_vol=500.0,
            source="yes_price",
        )
    )
    timeline.finalize()

    bt, _ = _run(
        timeline,
        config={"mode": "paper", "hard_stop_loss_pct": 0.0},
    )
    assert bt.trades
    assert bt.trades[0]["entry_price"] == 20.0


def test_sim_clock_replaces_wall_clock(monkeypatch):
    seen: dict[str, object] = {}

    class _CaptureSpreadFilter:
        def __init__(self, time_fn=None):
            seen["time_fn"] = time_fn

        def update(self, _spread):
            return None

        def spread_history(self):
            return []

    monkeypatch.setattr(
        "backtesting.contract_backtester.SpreadRegimeFilter",
        _CaptureSpreadFilter,
    )

    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 19.0, 21.0, 20.0, 0.8),
        ],
        close_time=7200.0,
    )
    _run(tl, config={"mode": "paper", "hard_stop_loss_pct": 0.0})
    assert callable(seen.get("time_fn"))


def test_obi_history_isolated_per_ticker():
    ticker_a = "KXBTC-26MAY0614-B50000"
    ticker_b = "KXBTC-26MAY0614-B51000"
    candles = _make_candles(8)

    tl_a = _timeline(
        ticker_a,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 20.0, 22.0, 21.0, 0.8),
            (2700.0, 21.0, 23.0, 22.0, 0.8),
        ],
        close_time=7200.0,
    )
    tl_b = _timeline(
        ticker_b,
        [
            (450.0, 79.0, 81.0, 80.0, 0.1),
            (1350.0, 79.0, 81.0, 80.0, 0.1),
            (2250.0, 78.0, 80.0, 79.0, 0.1),
        ],
        close_time=7200.0,
    )

    bt = ContractBacktester(
        candles=candles,
        contract_timelines={ticker_a: tl_a, ticker_b: tl_b},
        config={
            "mode": "paper",
            "hard_stop_loss_pct": 0.0,
            "paper_reentry_cooldown_sec": 0.0,
            "paper_same_side_cooldown_sec": 0.0,
            "consecutive_readings": 2,
            "long_threshold": 0.7,
            "short_threshold": 0.3,
        },
    )
    bt.run(bankroll=10000.0)
    assert bt.trades
    assert bt.trades[0]["ticker"] == ticker_a
    assert bt.trades[0]["direction"] == "long"


def test_mixed_ticker_event_stream_keeps_entry_invariant():
    ticker = "KXBTC-26MAY0614-B50000"
    noise_ticker = "KXBTC-26MAY0614-B52000"
    candles = _make_candles(8)
    base_config = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.0,
        "paper_reentry_cooldown_sec": 0.0,
        "paper_same_side_cooldown_sec": 0.0,
        "consecutive_readings": 2,
        "long_threshold": 0.7,
        "short_threshold": 0.3,
    }

    tl_main = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 20.0, 22.0, 21.0, 0.8),
            (2700.0, 21.0, 23.0, 22.0, 0.8),
        ],
        close_time=7200.0,
    )
    bt_single = ContractBacktester(
        candles=candles,
        contract_timelines={ticker: tl_main},
        config=base_config,
    )
    bt_single.run(bankroll=10000.0)
    assert bt_single.trades

    tl_noise = _timeline(
        noise_ticker,
        [
            (450.0, 80.0, 82.0, 81.0, 0.2),
            (1350.0, 80.0, 82.0, 81.0, 0.2),
        ],
        close_time=7200.0,
    )
    bt_mixed = ContractBacktester(
        candles=candles,
        contract_timelines={ticker: tl_main, noise_ticker: tl_noise},
        config=base_config,
    )
    bt_mixed.run(bankroll=10000.0)
    assert bt_mixed.trades

    assert bt_mixed.trades[0]["ticker"] == bt_single.trades[0]["ticker"]
    assert bt_mixed.trades[0]["direction"] == bt_single.trades[0]["direction"]
    assert bt_mixed.trades[0]["timestamp"] == bt_single.trades[0]["timestamp"]


def test_forced_entry_replay_is_deterministic():
    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 18.0, 20.0, 19.0, 0.8),
            (1800.0, 17.0, 19.0, 18.0, 0.8),
            (2700.0, 16.0, 18.0, 17.0, 0.8),
        ],
        close_time=3600.0,
    )
    config = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.10,
        "disable_signal_entries": True,
        "forced_entry": {
            "ticker": ticker,
            "entry_ts": 900.0,
            "entry_price": 20.0,
            "direction": "long",
            "contracts": 1,
            "allow_open_on_first_tick": True,
        },
    }
    bt_a, _ = _run(tl, config=config)
    bt_b, _ = _run(tl, config=config)
    assert bt_a.trades == bt_b.trades


def test_exit_fill_mode_executable_changes_exit_price():
    ticker = "KXBTC-26MAY0614-B50000"
    tl = _timeline(
        ticker,
        [
            (0.0, 19.0, 21.0, 20.0, 0.8),
            (900.0, 19.0, 21.0, 20.0, 0.8),
            (1800.0, 17.0, 19.0, 18.0, 0.8),
        ],
        close_time=3600.0,
    )
    base = {
        "mode": "paper",
        "hard_stop_loss_pct": 0.10,
        "disable_signal_entries": True,
        "forced_entry": {
            "ticker": ticker,
            "entry_ts": 900.0,
            "entry_price": 20.0,
            "direction": "long",
            "contracts": 1,
            "allow_open_on_first_tick": True,
        },
    }
    bt_mark, _ = _run(tl, config={**base, "exit_fill_mode": "mark"})
    bt_exec, _ = _run(tl, config={**base, "exit_fill_mode": "executable"})
    assert bt_mark.trades and bt_exec.trades
    assert bt_mark.trades[0]["exit_reason"] in {"HARD_STOP_LOSS", "STOP_LOSS"}
    assert bt_exec.trades[0]["exit_reason"] in {"HARD_STOP_LOSS", "STOP_LOSS"}
    assert bt_mark.trades[0]["exit_price"] == 18.0
    assert bt_exec.trades[0]["exit_price"] != bt_mark.trades[0]["exit_price"]
    assert bt_exec.trades[0]["exit_timestamp"] <= bt_mark.trades[0]["exit_timestamp"]
