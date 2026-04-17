"""Production incident fixture data.

Each fixture encodes the exact Kalshi API response sequence that caused
a known orphan bug. These are consumed by test_orphan_incident_replay.py.
"""
from tests.replay.helpers import (
    ExchangePosition,
    MarketStatus,
    TimelineEvent,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Incident 1: Settlement verify failure -> duplicate trade (451/454)
#
# Sequence:
#   1. Bot enters short on KXBTC-26APR1323-B74450 (1 contract @ 25c)
#   2. Contract settles -> verify_position_on_exchange FAILS (API timeout)
#   3. Old code: adopted as orphan -> orphan settles -> double-counted
#   4. Fixed code: no orphan created, ticker added to settled_tickers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INCIDENT_451_TICKER = "KXBTC-26APR1323-B74450"

INCIDENT_451_SETTLEMENT_VERIFY_FAILURE = [
    # Step 0: Bot has position, settlement triggers, verify FAILS 3 times
    TimelineEvent(
        positions=[],
        markets={INCIDENT_451_TICKER: MarketStatus(
            ticker=INCIDENT_451_TICKER, status="settled", result="no"
        )},
        verify_fails=True,
    ),
    # Step 1: After settlement handled, reconciliation runs.
    # Exchange still shows a stale position (Kalshi API lag).
    TimelineEvent(
        positions=[ExchangePosition(
            ticker=INCIDENT_451_TICKER, position_fp=-1.0,
            total_traded_dollars=0.25,
        )],
        markets={INCIDENT_451_TICKER: MarketStatus(
            ticker=INCIDENT_451_TICKER, status="settled", result="no"
        )},
    ),
    # Step 2: Position clears from exchange.
    TimelineEvent(
        positions=[],
        markets={INCIDENT_451_TICKER: MarketStatus(
            ticker=INCIDENT_451_TICKER, status="finalized", result="no"
        )},
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Incident 2: BUG-015 phantom accumulation (trade 431, 423 contracts)
#
# Sequence:
#   1. Reconciliation finds 6 contracts on KXBTC-26APR1316-B73050
#   2. Reconciliation runs 70 more times, each time seeing the same 6
#   3. Old code: 6 * 71 = 426 contracts accumulated
#   4. Fixed code: stays at 6 contracts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG015_TICKER = "KXBTC-26APR1316-B73050"

BUG015_ACCUMULATION_PRESSURE = [
    # Exchange consistently reports 6 contracts on this ticker
    TimelineEvent(
        positions=[ExchangePosition(
            ticker=BUG015_TICKER, position_fp=6.0,
            total_traded_dollars=1.02,
        )],
        markets={BUG015_TICKER: MarketStatus(
            ticker=BUG015_TICKER, status="open"
        )},
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Incident 3: Restart wipes _settled_tickers -> re-adoption
#
# Sequence:
#   1. Trade settles, ticker added to _settled_tickers
#   2. Bot restarts (snapshot/restore)
#   3. Reconciliation sees stale position on same ticker
#   4. Old code: re-adopted as orphan (settled_tickers lost)
#   5. Fixed code: settled_tickers restored from snapshot, skipped
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESTART_TICKER = "KXBTC-26APR1315-B72150"

RESTART_SETTLED_PERSISTENCE = [
    # Exchange shows stale position after restart
    TimelineEvent(
        positions=[ExchangePosition(
            ticker=RESTART_TICKER, position_fp=12.0,
            total_traded_dollars=1.32,
        )],
        markets={RESTART_TICKER: MarketStatus(
            ticker=RESTART_TICKER, status="open"
        )},
    ),
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Incident 4: Exit cooldown race (trade exits, reconciliation immediate)
#
# Sequence:
#   1. Bot exits position on ticker X
#   2. Reconciliation runs within 5 seconds
#   3. Exchange still shows position (Kalshi ledger lag)
#   4. Old code: adopted as orphan
#   5. Fixed code: cooldown blocks adoption for 90s
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COOLDOWN_TICKER = "KXBTC-26APR1317-B73125"

COOLDOWN_RACE = [
    TimelineEvent(
        positions=[ExchangePosition(
            ticker=COOLDOWN_TICKER, position_fp=2.0,
            total_traded_dollars=0.76,
        )],
        markets={COOLDOWN_TICKER: MarketStatus(
            ticker=COOLDOWN_TICKER, status="open"
        )},
    ),
]
