"""
KBTC — Kalshi BTC 15-Min Trading Bot
Entry point: FastAPI app with lifespan managing all subsystems.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import structlog
import uvloop

uvloop.install()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from coordinator import Coordinator

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "kbtc.starting",
        env=settings.bot.env,
        market=settings.bot.market,
        mode=settings.bot.trading_mode,
    )
    await coordinator.start()
    yield
    logger.info("kbtc.shutting_down")
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


@app.get("/")
async def root():
    return {"status": "ok", "service": "kbtc"}
