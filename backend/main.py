"""
KBTC — Kalshi BTC 15-Min Trading Bot
Entry point: FastAPI app with lifespan managing all subsystems.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import structlog
import uvloop

uvloop.install()

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import settings
from coordinator import Coordinator
from notifications import init_notifier

import logging

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if not settings.bot.is_production
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        _LOG_LEVELS.get(settings.bot.log_level.upper(), logging.INFO)
    ),
)

logger = structlog.get_logger(__name__)

coordinator = Coordinator()

_start_time: float = 0.0
_heartbeat_task: asyncio.Task = None
_summary_task: asyncio.Task = None


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, s = divmod(s, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


async def _heartbeat_loop(notifier):
    """Send a heartbeat ping every 30 minutes."""
    await asyncio.sleep(60)
    while True:
        try:
            uptime = _format_uptime(time.monotonic() - _start_time)
            state = coordinator.data_manager.states.get(settings.bot.market)
            spot = state.spot_price if state else None
            ticker = state.kalshi_ticker if state else None
            has_pos = coordinator.paper_trader.has_position or coordinator.live_trader.has_position
            bankroll = coordinator.live_sizer.bankroll if coordinator.live_enabled else coordinator.paper_sizer.bankroll
            await notifier.heartbeat_ping(
                uptime_str=uptime,
                spot_price=spot,
                ticker=ticker,
                has_position=has_pos,
                bankroll=bankroll,
            )
        except Exception as e:
            logger.warning("heartbeat.failed", error=str(e))
        await asyncio.sleep(1800)


async def _periodic_summary_loop(notifier):
    """Send a performance summary every 4 hours and a daily summary at midnight UTC."""
    last_daily = -1
    interval_hours = 4
    await asyncio.sleep(300)
    while True:
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            # Pick the primary trader for summaries based on mode
            primary_trader = coordinator.live_trader if coordinator.live_enabled else coordinator.paper_trader
            primary_sizer = coordinator.live_sizer if coordinator.live_enabled else coordinator.paper_sizer

            if now.hour == 0 and last_daily != now.day:
                last_daily = now.day
                trades = primary_trader.trades
                today_trades = trades
                wins = sum(1 for t in today_trades if t.pnl >= 0)
                losses = len(today_trades) - wins
                gross = sum(t.pnl for t in today_trades)
                best = max((t.pnl for t in today_trades), default=0.0)
                worst = min((t.pnl for t in today_trades), default=0.0)
                await notifier.daily_summary(
                    total_trades=len(today_trades),
                    wins=wins,
                    losses=losses,
                    gross_pnl=gross,
                    best_trade_pnl=best,
                    worst_trade_pnl=worst,
                    start_bankroll=primary_sizer.daily_start_bankroll,
                    end_bankroll=primary_sizer.bankroll,
                    peak_drawdown_pct=primary_sizer.current_drawdown,
                )

            if now.hour % interval_hours == 0 and now.minute < 5:
                trades = primary_trader.trades
                wins = sum(1 for t in trades if t.pnl >= 0)
                losses = len(trades) - wins
                net_pnl = sum(t.pnl for t in trades)
                pos = primary_trader.position
                await notifier.periodic_summary(
                    hours=interval_hours,
                    trades_count=len(trades),
                    wins=wins,
                    losses=losses,
                    net_pnl=net_pnl,
                    bankroll=primary_sizer.bankroll,
                    drawdown_pct=primary_sizer.current_drawdown,
                    has_position=primary_trader.has_position,
                    position_ticker=pos.ticker if pos else None,
                )
        except Exception as e:
            logger.warning("periodic_summary.failed", error=str(e))
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _start_time, _heartbeat_task, _summary_task
    _start_time = time.monotonic()

    logger.info(
        "kbtc.starting",
        env=settings.bot.env,
        market=settings.bot.market,
        mode=settings.bot.trading_mode,
    )

    notifier = init_notifier()
    await coordinator.start()

    await notifier.bot_started(
        market=settings.bot.market,
        mode=settings.bot.trading_mode,
        bankroll=settings.bot.initial_bankroll,
    )

    _heartbeat_task = asyncio.create_task(_heartbeat_loop(notifier))
    _summary_task = asyncio.create_task(_periodic_summary_loop(notifier))

    yield

    logger.info("kbtc.shutting_down")

    if _heartbeat_task:
        _heartbeat_task.cancel()
    if _summary_task:
        _summary_task.cancel()

    uptime = _format_uptime(time.monotonic() - _start_time)
    await notifier.bot_stopped(
        uptime_str=uptime,
        bankroll=coordinator.position_sizer.bankroll,
    )
    await coordinator.stop()


app = FastAPI(
    title="KBTC Trading Bot",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.bot.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.routes import router as api_router
from api.ws import router as ws_router

app.include_router(api_router, prefix="/api")
app.include_router(ws_router, prefix="/api")


FRONTEND_DIR = Path(__file__).parent / "static"

if FRONTEND_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static-assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = FRONTEND_DIR / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
else:
    @app.get("/")
    async def root():
        return {"status": "ok", "service": "kbtc"}
