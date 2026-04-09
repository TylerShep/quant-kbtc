"""
Discord webhook notifier — sends rich embeds across four channels.

Trades channel:    trade_opened, trade_closed
Risk channel:      circuit_breaker_tripped, circuit_breaker_cleared,
                   position_sizing_failed, atr_regime_changed
Heartbeat channel: heartbeat_ping, periodic_summary, daily_summary
Errors channel:    bot_started, bot_stopped, ws_disconnected,
                   db_error, unhandled_exception

Silently no-ops when webhook_url is empty so the bot works without Discord.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

COLOR_GREEN = 0x57F287
COLOR_RED = 0xED4245
COLOR_YELLOW = 0xFEE75C
COLOR_BLUE = 0x5865F2
COLOR_ORANGE = 0xE67E22
COLOR_PURPLE = 0x9B59B6

_EMBED_TITLE_MAX = 256
_EMBED_DESC_MAX = 4096
_EMBED_FIELD_VALUE_MAX = 1024
_EMBED_FOOTER_MAX = 2048


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _clean_url(url: str) -> str:
    u = (url or "").strip()
    if len(u) >= 2 and u[0] == u[-1] and u[0] in ('"', "'"):
        u = u[1:-1].strip()
    return u


def _embed_text(value: object, max_len: int, fallback: str = "\u2014") -> str:
    s = str(value).strip() if value is not None else fallback
    if not s:
        s = fallback
    return s[:max_len - 1] + "\u2026" if len(s) > max_len else s


def _sanitize_embed(embed: dict) -> dict:
    out = dict(embed)
    if "title" in out:
        out["title"] = _embed_text(out["title"], _EMBED_TITLE_MAX)
    if "description" in out and out["description"] is not None:
        out["description"] = _embed_text(out["description"], _EMBED_DESC_MAX)
    if "footer" in out and isinstance(out["footer"], dict):
        out["footer"] = {"text": _embed_text(out["footer"].get("text"), _EMBED_FOOTER_MAX)}
    if "fields" in out and out["fields"]:
        out["fields"] = [
            {
                "name": _embed_text(f.get("name"), 256, "Field"),
                "value": _embed_text(f.get("value"), _EMBED_FIELD_VALUE_MAX),
                "inline": bool(f.get("inline", False)),
            }
            for f in out["fields"]
            if isinstance(f, dict)
        ]
    if "color" in out:
        try:
            out["color"] = int(out["color"]) & 0xFFFFFF
        except (TypeError, ValueError):
            out["color"] = COLOR_BLUE
    return out


def _retry_after(resp: httpx.Response) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("retry_after") is not None:
            return float(data["retry_after"])
    except (ValueError, TypeError):
        pass
    return 1.0


class DiscordNotifier:
    """Posts rich Discord embeds to four webhook URLs (trades, risk, heartbeat, errors)."""

    def __init__(
        self,
        trades_url: str = "",
        risk_url: str = "",
        heartbeat_url: str = "",
        errors_url: str = "",
    ):
        self._trades_url = _clean_url(trades_url)
        self._risk_url = _clean_url(risk_url)
        self._heartbeat_url = _clean_url(heartbeat_url)
        self._errors_url = _clean_url(errors_url)
        self._post_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._trades_url or self._risk_url or self._heartbeat_url or self._errors_url)

    # ── Transport ──────────────────────────────────────────────────────────────

    async def _post(self, url: str, embed: dict) -> None:
        if not url:
            return
        payload = {"embeds": [_sanitize_embed(embed)]}
        async with self._post_lock:
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    for _ in range(8):
                        resp = await client.post(url, json=payload)
                        if resp.status_code in (200, 204):
                            return
                        if resp.status_code == 429:
                            wait = _retry_after(resp) + 0.05
                            await asyncio.sleep(min(max(wait, 0.05), 60.0))
                            continue
                        logger.warning("discord.post_failed", status=resp.status_code, body=resp.text[:300])
                        return
                    logger.warning("discord.rate_limited", url=url[:60])
            except Exception as e:
                logger.warning("discord.post_error", error=str(e))

    def _footer(self) -> dict:
        return {"text": f"KBTC Bot \u00b7 {_ts()}"}

    # ── #kbtc-trades ───────────────────────────────────────────────────────────

    async def trade_opened(
        self,
        ticker: str,
        direction: str,
        contracts: int,
        entry_price: float,
        conviction: str,
        obi: float = 0.0,
        roc: float = 0.0,
    ) -> None:
        side_icon = "\U0001f53c LONG (YES)" if direction == "long" else "\U0001f53d SHORT (NO)"
        embed = {
            "title": f"\U0001f4c8 Trade Opened \u2014 {ticker}",
            "color": COLOR_BLUE,
            "fields": [
                {"name": "Side", "value": side_icon, "inline": True},
                {"name": "Entry Price", "value": f"{entry_price}\u00a2", "inline": True},
                {"name": "Contracts", "value": str(contracts), "inline": True},
                {"name": "Conviction", "value": conviction, "inline": True},
                {"name": "OBI", "value": f"{obi:.3f}", "inline": True},
                {"name": "ROC", "value": f"{roc:+.3f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._trades_url, embed)

    async def trade_closed(
        self,
        ticker: str,
        direction: str,
        contracts: int,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        candles_held: int,
        bankroll: float,
    ) -> None:
        won = pnl >= 0
        icon = "\u2705" if won else "\u274c"
        pnl_str = f"{'+'if pnl >= 0 else ''}${pnl:.4f}"
        embed = {
            "title": f"{icon} Trade Closed \u2014 {ticker}",
            "color": COLOR_GREEN if won else COLOR_RED,
            "fields": [
                {"name": "Direction", "value": direction.upper(), "inline": True},
                {"name": "Contracts", "value": str(contracts), "inline": True},
                {"name": "Entry", "value": f"{entry_price}\u00a2", "inline": True},
                {"name": "Exit", "value": f"{exit_price}\u00a2", "inline": True},
                {"name": "PnL", "value": pnl_str, "inline": True},
                {"name": "PnL %", "value": f"{pnl_pct:+.2%}", "inline": True},
                {"name": "Exit Reason", "value": exit_reason, "inline": True},
                {"name": "Candles Held", "value": str(candles_held), "inline": True},
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._trades_url, embed)

    # ── #kbtc-risk ─────────────────────────────────────────────────────────────

    async def circuit_breaker_tripped(
        self,
        reason: str,
        daily_loss_pct: float,
        weekly_loss_pct: float,
        drawdown_pct: float,
        bankroll: float,
    ) -> None:
        embed = {
            "title": f"\U0001f6d1 Circuit Breaker \u2014 {reason}",
            "description": "Trading has been halted. No new positions will open until the breaker clears.",
            "color": COLOR_RED,
            "fields": [
                {"name": "Breaker", "value": reason, "inline": True},
                {"name": "Daily Loss", "value": f"{daily_loss_pct:.2%}", "inline": True},
                {"name": "Weekly Loss", "value": f"{weekly_loss_pct:.2%}", "inline": True},
                {"name": "Drawdown", "value": f"{drawdown_pct:.2%}", "inline": True},
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def circuit_breaker_cleared(self, bankroll: float) -> None:
        embed = {
            "title": "\U0001f7e2 Circuit Breaker Cleared",
            "description": "All risk thresholds are within limits. Trading has resumed.",
            "color": COLOR_GREEN,
            "fields": [
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def position_sizing_failed(self, size_dollars: float, price: float, bankroll: float) -> None:
        embed = {
            "title": "\u26a0\ufe0f Position Sizing Failed",
            "description": "Bankroll too low to open even a minimum position.",
            "color": COLOR_YELLOW,
            "fields": [
                {"name": "Calculated Size", "value": f"${size_dollars:.2f}", "inline": True},
                {"name": "Contract Price", "value": f"{price}\u00a2", "inline": True},
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def atr_regime_changed(self, old_regime: str, new_regime: str, atr_value: Optional[float] = None) -> None:
        color = COLOR_RED if new_regime == "HIGH" else (COLOR_YELLOW if new_regime == "MEDIUM" else COLOR_GREEN)
        desc = "HIGH regime blocks new entries." if new_regime == "HIGH" else ""
        fields = [
            {"name": "Previous", "value": old_regime, "inline": True},
            {"name": "Current", "value": new_regime, "inline": True},
        ]
        if atr_value is not None:
            fields.append({"name": "ATR Value", "value": f"{atr_value:.4f}", "inline": True})
        embed = {
            "title": f"\U0001f30a ATR Regime Change \u2014 {old_regime} \u2192 {new_regime}",
            "description": desc,
            "color": color,
            "fields": fields,
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    # ── #kbtc-heartbeat ────────────────────────────────────────────────────────

    async def heartbeat_ping(
        self,
        uptime_str: str,
        spot_price: Optional[float],
        ticker: Optional[str],
        has_position: bool,
        bankroll: float,
    ) -> None:
        fields = [
            {"name": "Uptime", "value": uptime_str, "inline": True},
            {"name": "BTC Spot", "value": f"${spot_price:,.2f}" if spot_price else "N/A", "inline": True},
            {"name": "Contract", "value": ticker or "None", "inline": True},
            {"name": "Position", "value": "Open" if has_position else "Flat", "inline": True},
            {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
        ]
        embed = {
            "title": "\U0001f493 Heartbeat",
            "color": COLOR_BLUE,
            "fields": fields,
            "footer": self._footer(),
        }
        await self._post(self._heartbeat_url, embed)

    async def periodic_summary(
        self,
        hours: int,
        trades_count: int,
        wins: int,
        losses: int,
        net_pnl: float,
        bankroll: float,
        drawdown_pct: float,
        has_position: bool,
        position_ticker: Optional[str] = None,
    ) -> None:
        pnl_str = f"{'+'if net_pnl >= 0 else ''}${net_pnl:.4f}"
        pos_str = f"Open ({position_ticker})" if has_position and position_ticker else ("Open" if has_position else "Flat")
        embed = {
            "title": f"\U0001f4ca {hours}h Performance Summary",
            "color": COLOR_GREEN if net_pnl >= 0 else COLOR_RED,
            "fields": [
                {"name": "Trades", "value": str(trades_count), "inline": True},
                {"name": "W / L", "value": f"{wins} / {losses}", "inline": True},
                {"name": "Net PnL", "value": pnl_str, "inline": True},
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
                {"name": "Drawdown", "value": f"{drawdown_pct:.2%}", "inline": True},
                {"name": "Position", "value": pos_str, "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._heartbeat_url, embed)

    async def daily_summary(
        self,
        total_trades: int,
        wins: int,
        losses: int,
        gross_pnl: float,
        best_trade_pnl: float,
        worst_trade_pnl: float,
        start_bankroll: float,
        end_bankroll: float,
        peak_drawdown_pct: float,
    ) -> None:
        pnl_str = f"{'+'if gross_pnl >= 0 else ''}${gross_pnl:.4f}"
        winrate = f"{wins / total_trades:.1%}" if total_trades > 0 else "N/A"
        embed = {
            "title": "\U0001f4c5 Daily Summary",
            "description": f"End-of-day report for **{datetime.now(timezone.utc).strftime('%Y-%m-%d')}**.",
            "color": COLOR_GREEN if gross_pnl >= 0 else COLOR_RED,
            "fields": [
                {"name": "Total Trades", "value": str(total_trades), "inline": True},
                {"name": "Win Rate", "value": winrate, "inline": True},
                {"name": "Net PnL", "value": pnl_str, "inline": True},
                {"name": "Best Trade", "value": f"${best_trade_pnl:+.4f}", "inline": True},
                {"name": "Worst Trade", "value": f"${worst_trade_pnl:+.4f}", "inline": True},
                {"name": "Peak Drawdown", "value": f"{peak_drawdown_pct:.2%}", "inline": True},
                {"name": "Start Bankroll", "value": f"${start_bankroll:,.2f}", "inline": True},
                {"name": "End Bankroll", "value": f"${end_bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._heartbeat_url, embed)

    # ── #kbtc-errors ───────────────────────────────────────────────────────────

    async def bot_started(self, market: str, mode: str, bankroll: float) -> None:
        embed = {
            "title": "\U0001f7e2 KBTC Bot Started",
            "description": "Bot is online and ready to trade.",
            "color": COLOR_GREEN,
            "fields": [
                {"name": "Market", "value": market, "inline": True},
                {"name": "Mode", "value": mode.upper(), "inline": True},
                {"name": "Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)

    async def bot_stopped(self, uptime_str: str, bankroll: float) -> None:
        embed = {
            "title": "\U0001f534 KBTC Bot Stopped",
            "description": "Bot has shut down.",
            "color": COLOR_RED,
            "fields": [
                {"name": "Uptime", "value": uptime_str, "inline": True},
                {"name": "Final Bankroll", "value": f"${bankroll:,.2f}", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)

    async def ws_disconnected(self, feed: str, error: str, attempt: int) -> None:
        embed = {
            "title": f"\u26a1 WebSocket Disconnected \u2014 {feed}",
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Feed", "value": feed, "inline": True},
                {"name": "Attempt", "value": str(attempt), "inline": True},
                {"name": "Error", "value": error[:200], "inline": False},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)

    async def db_error(self, operation: str, error: str) -> None:
        embed = {
            "title": f"\U0001f4be Database Error \u2014 {operation}",
            "color": COLOR_RED,
            "fields": [
                {"name": "Operation", "value": operation, "inline": True},
                {"name": "Error", "value": error[:500], "inline": False},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)

    async def trade_quarantined(
        self,
        ticker: str,
        direction: str,
        pnl: float,
        error_reason: str,
        rapid_count: int,
    ) -> None:
        embed = {
            "title": f"\U0001f6a8 Trade Quarantined \u2014 {ticker}",
            "description": f"Trade diverted to `errored_trades` table and excluded from equity.",
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Direction", "value": direction.upper(), "inline": True},
                {"name": "PnL", "value": f"${pnl:+.4f}", "inline": True},
                {"name": "Reason", "value": error_reason, "inline": True},
                {"name": "Rapid Count", "value": str(rapid_count), "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)

    async def unhandled_exception(self, location: str, error: str) -> None:
        embed = {
            "title": f"\U0001f4a5 Unhandled Exception \u2014 {location}",
            "color": COLOR_RED,
            "fields": [
                {"name": "Location", "value": location, "inline": True},
                {"name": "Error", "value": error[:800], "inline": False},
            ],
            "footer": self._footer(),
        }
        await self._post(self._errors_url, embed)


# Singleton, initialized lazily by main.py lifespan
_notifier: Optional[DiscordNotifier] = None


def get_notifier() -> DiscordNotifier:
    global _notifier
    if _notifier is None:
        from config import settings
        _notifier = DiscordNotifier(
            trades_url=settings.bot.discord_trades_webhook,
            risk_url=settings.bot.discord_risk_webhook,
            heartbeat_url=settings.bot.discord_heartbeat_webhook,
            errors_url=settings.bot.discord_errors_webhook,
        )
    return _notifier


def init_notifier() -> DiscordNotifier:
    """Explicitly initialize (called from main.py lifespan)."""
    global _notifier
    from config import settings
    _notifier = DiscordNotifier(
        trades_url=settings.bot.discord_trades_webhook,
        risk_url=settings.bot.discord_risk_webhook,
        heartbeat_url=settings.bot.discord_heartbeat_webhook,
        errors_url=settings.bot.discord_errors_webhook,
    )
    return _notifier
