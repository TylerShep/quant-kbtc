"""
Discord webhook notifier — sends rich embeds across five channels.

Trades channel:       trade_opened, trade_closed
Risk channel:         circuit_breaker_tripped, circuit_breaker_cleared,
                      position_sizing_failed, atr_regime_changed
Heartbeat channel:    heartbeat_ping, periodic_summary, daily_summary
Errors channel:       bot_started, bot_stopped, ws_disconnected,
                      db_error, unhandled_exception
Attribution channel:  daily_attribution, weekly_digest, tuning_cycle_report

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
    """Posts rich Discord embeds to five webhook URLs (trades, risk, heartbeat, errors, attribution)."""

    def __init__(
        self,
        trades_url: str = "",
        risk_url: str = "",
        heartbeat_url: str = "",
        errors_url: str = "",
        attribution_url: str = "",
        live_trades_url: str = "",
    ):
        self._trades_url = _clean_url(trades_url)
        self._risk_url = _clean_url(risk_url)
        self._heartbeat_url = _clean_url(heartbeat_url)
        self._errors_url = _clean_url(errors_url)
        self._attribution_url = _clean_url(attribution_url)
        self._live_trades_url = _clean_url(live_trades_url)
        self._post_lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._trades_url or self._risk_url or self._heartbeat_url or self._errors_url or self._attribution_url or self._live_trades_url)

    # ── Transport ──────────────────────────────────────────────────────────────

    async def _post(self, url: str, embed: dict) -> None:
        """POST a single embed to Discord with retries.

        Retry policy:
          * 200/204: success, return immediately.
          * 429 (rate-limited): sleep ``Retry-After`` seconds (capped at 60),
            then retry. Counts against the attempt budget.
          * 5xx (Discord-side outage / CDN failure): exponential backoff
            (0.5s, 1s, 2s, 4s, 8s, capped at 30s) then retry. We were
            previously dropping these silently after one attempt, which
            caused missing trade_closed notifications during Discord
            incidents (BUG-029).
          * 4xx other than 429 (bad payload, bad webhook URL): log and
            give up — retrying won't help.
          * Network exceptions: exponential backoff then retry, same as 5xx.

        Total attempt budget: ``MAX_ATTEMPTS`` across all failure modes.
        """
        if not url:
            return
        payload = {"embeds": [_sanitize_embed(embed)]}
        MAX_ATTEMPTS = 8
        async with self._post_lock:
            backoff = 0.5
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        try:
                            resp = await client.post(url, json=payload)
                        except (httpx.HTTPError, httpx.TransportError) as e:
                            if attempt >= MAX_ATTEMPTS:
                                logger.warning(
                                    "discord.post_error",
                                    error=str(e), attempts=attempt, url=url[:60],
                                )
                                return
                            await asyncio.sleep(min(backoff, 30.0))
                            backoff *= 2
                            continue

                        if resp.status_code in (200, 204):
                            return
                        if resp.status_code == 429:
                            wait = _retry_after(resp) + 0.05
                            await asyncio.sleep(min(max(wait, 0.05), 60.0))
                            continue
                        if 500 <= resp.status_code < 600:
                            if attempt >= MAX_ATTEMPTS:
                                logger.warning(
                                    "discord.post_failed_after_retries",
                                    status=resp.status_code,
                                    body=resp.text[:300],
                                    attempts=attempt,
                                    url=url[:60],
                                )
                                return
                            await asyncio.sleep(min(backoff, 30.0))
                            backoff *= 2
                            continue
                        logger.warning(
                            "discord.post_failed",
                            status=resp.status_code, body=resp.text[:300],
                            url=url[:60],
                        )
                        return
                    logger.warning("discord.exhausted_retries", url=url[:60])
            except Exception as e:
                logger.warning("discord.post_error", error=str(e), url=url[:60])

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
        mode: str = "paper",
    ) -> None:
        mode_badge = "[LIVE]" if mode == "live" else "[PAPER]"
        side_icon = "\U0001f53c LONG (YES)" if direction == "long" else "\U0001f53d SHORT (NO)"
        embed = {
            "title": f"\U0001f4c8 {mode_badge} Trade Opened \u2014 {ticker}",
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
        if mode == "live" and self._live_trades_url:
            await self._post(self._live_trades_url, embed)

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
        mode: str = "paper",
    ) -> None:
        mode_badge = "[LIVE]" if mode == "live" else "[PAPER]"
        won = pnl >= 0
        icon = "\u2705" if won else "\u274c"
        pnl_str = f"{'+'if pnl >= 0 else ''}${pnl:.4f}"
        embed = {
            "title": f"{icon} {mode_badge} Trade Closed \u2014 {ticker}",
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
        if mode == "live" and self._live_trades_url:
            await self._post(self._live_trades_url, embed)

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

    async def live_drought_alarm(
        self,
        last_live_age_str: str,
        paper_trades_36h: int,
        threshold_hours: int,
    ) -> None:
        """Live lane has gone too long without a trade despite paper firing.

        Catches the failure mode where edge_profile (or any other live-only
        filter) silently rejects everything and the operator doesn't notice.
        """
        embed = {
            "title": "\u26a0\ufe0f Live Trading Drought",
            "description": (
                f"Live lane has been dark for **{last_live_age_str}** while "
                f"paper has executed **{paper_trades_36h}** trades in the same "
                "36h window. The edge_profile filter or another live-only gate "
                "may be over-blocking. Investigate via the dashboard or run "
                "`scripts/edge_profile_review.py --print-only`."
            ),
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Last Live Trade", "value": last_live_age_str, "inline": True},
                {"name": "Paper Trades 36h", "value": str(paper_trades_36h), "inline": True},
                {"name": "Threshold", "value": f">{threshold_hours}h", "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def edge_skip_ratio_alarm(
        self,
        ratio: float,
        consecutive: int,
        top_reasons: list,
    ) -> None:
        """edge_profile has been rejecting >95% of would-be entries for
        consecutive 24h windows. Strong signal of calibration drift."""
        reasons_str = "\n".join(
            f"- `{r}`: {n}" for r, n in top_reasons[:5]
        ) or "(none)"
        embed = {
            "title": "\u26a0\ufe0f EDGE Skip Ratio Elevated",
            "description": (
                f"**{ratio:.1%}** of signal_log rows in the last 24h were "
                f"rejected by an EDGE_* filter, for **{consecutive}** "
                "consecutive checks. Likely calibration drift; review the "
                "weekly edge_profile report."
            ),
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "24h EDGE Ratio", "value": f"{ratio:.1%}", "inline": True},
                {"name": "Consecutive Breaches", "value": str(consecutive), "inline": True},
                {"name": "Top Skip Reasons", "value": reasons_str, "inline": False},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def direction_imbalance_alarm(
        self,
        short_rejected: int,
        long_rejected: int,
        live_short_count_7d: int,
    ) -> None:
        """Short rejections vastly outweigh long rejections AND no live
        shorts have happened in 7d. The short-side filter may be calibrated
        for a market regime that no longer applies."""
        ratio_str = (
            f"{short_rejected / long_rejected:.1f}x"
            if long_rejected > 0 else "inf"
        )
        embed = {
            "title": "\u26a0\ufe0f Direction Skip Imbalance",
            "description": (
                f"Short-side EDGE rejections (**{short_rejected}**) are "
                f"**{ratio_str}** the long-side rejections "
                f"(**{long_rejected}**) over the past 7 days, and "
                f"**{live_short_count_7d}** live shorts have actually "
                "traded. The short-side filter may be mis-calibrated for "
                "the current regime."
            ),
            "color": COLOR_ORANGE,
            "fields": [
                {"name": "Short Rejected (7d)", "value": str(short_rejected), "inline": True},
                {"name": "Long Rejected (7d)", "value": str(long_rejected), "inline": True},
                {"name": "Imbalance", "value": ratio_str, "inline": True},
                {"name": "Live Shorts (7d)", "value": str(live_short_count_7d), "inline": True},
            ],
            "footer": self._footer(),
        }
        await self._post(self._risk_url, embed)

    async def edge_profile_auto_applied(
        self,
        changes: list,
        backup_path: str,
        restart_status: str,
    ) -> None:
        """Phase 2.5 auto-apply state-change announcement.

        Posts to the RISK channel because this is a state mutation, not an
        attribution report. Includes the rollback template so the operator
        can revert without hunting for the backup path.

        ``changes`` is a list of dicts with keys: param, old, new, sed_cmd.
        ``restart_status`` is one of: 'restarted', 'deferred_position_open',
        'failed', 'skipped'.
        """
        change_lines = "\n".join(
            f"`{c['param']}`: `{c['old']}` \u2192 `{c['new']}`"
            for c in changes
        ) or "(none)"
        sed_lines = "\n".join(c["sed_cmd"] for c in changes) or "(none)"
        rollback_cmd = f"cp {backup_path} ~/kbtc/.env"
        restart_label = {
            "restarted": "Restarted",
            "deferred_position_open": "Restart deferred (live position open)",
            "failed": "Restart FAILED",
            "skipped": "Skipped (--no-restart)",
        }.get(restart_status, restart_status)
        color = COLOR_RED if restart_status == "failed" else COLOR_BLUE
        embed = {
            "title": f"\U0001f527 edge_profile auto-applied {len(changes)} change(s)",
            "description": (
                "Tier 1 (tightening-only) recommendations from the weekly "
                "review have been applied to the live env. Rollback command "
                "below if any look wrong."
            ),
            "color": color,
            "fields": [
                {"name": "Changes", "value": change_lines, "inline": False},
                {"name": "sed Commands", "value": f"```{sed_lines[:900]}```", "inline": False},
                {"name": "Restart", "value": restart_label, "inline": True},
                {"name": "Rollback", "value": f"```{rollback_cmd}```", "inline": False},
            ],
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

    # ── #kbtc-attribution ──────────────────────────────────────────────────────

    async def daily_attribution_report(self, date_str: str, attr: dict) -> None:
        total_pnl = attr.get("total_pnl_dollars", 0)
        total_trades = attr.get("total_trades", 0)
        color = COLOR_GREEN if total_pnl >= 0 else COLOR_RED
        pnl_str = f"{'+'if total_pnl >= 0 else ''}${total_pnl:.2f}"

        fields = [
            {"name": "Trades", "value": str(total_trades), "inline": True},
            {"name": "Net PnL", "value": pnl_str, "inline": True},
        ]

        sig = attr.get("signal_attribution", {})
        for conviction in ("HIGH", "NORMAL", "LOW"):
            if conviction in sig:
                s = sig[conviction]
                fields.append({
                    "name": f"{conviction} Conviction",
                    "value": f"{s['trades']} trades | WR {s['win_rate']:.0%} | ${s['pnl_dollars']:+.2f}",
                    "inline": False,
                })

        session = attr.get("session_attribution", {})
        if session:
            lines = []
            for sname, sdata in session.items():
                pnl_s = sdata.get("pnl_dollars", 0)
                marker = "+" if pnl_s >= 0 else ""
                lines.append(f"{sname}: {marker}${pnl_s:.2f} ({sdata.get('trades', 0)}t)")
            fields.append({"name": "Sessions", "value": "\n".join(lines), "inline": False})

        exe = attr.get("execution_attribution", {})
        fee_pct = exe.get("fees_as_pct_of_gross", 0)
        if exe:
            fields.append({
                "name": "Fee Drag",
                "value": f"${exe.get('total_fees_dollars', 0):.2f} ({fee_pct:.1f}% of gross)",
                "inline": True,
            })

        embed = {
            "title": f"\U0001f4ca Daily Attribution \u2014 {date_str}",
            "color": color,
            "fields": fields,
            "footer": self._footer(),
        }
        await self._post(self._attribution_url, embed)

    async def tuning_cycle_report(
        self,
        edge_consistency: float,
        avg_oos_sharpe: float,
        should_apply: bool,
        reason: str,
        changes: Optional[dict] = None,
        health_alerts: Optional[list[str]] = None,
    ) -> None:
        if should_apply:
            color = COLOR_GREEN
        elif health_alerts:
            color = COLOR_ORANGE
        else:
            color = COLOR_YELLOW
        fields = [
            {"name": "Edge Consistency", "value": f"{edge_consistency:.1%}", "inline": True},
            {"name": "OOS Sharpe", "value": f"{avg_oos_sharpe:.2f}", "inline": True},
            {"name": "Auto-Apply", "value": "Yes" if should_apply else "No", "inline": True},
            {"name": "Reason", "value": reason or "\u2014", "inline": False},
        ]
        if changes:
            change_lines = [f"`{k}`: {v.get('from')} \u2192 {v.get('to')}" for k, v in changes.items()]
            fields.append({"name": "Proposed Changes", "value": "\n".join(change_lines), "inline": False})
        if health_alerts:
            alert_lines = "\n".join(f"- {a}" for a in health_alerts)
            fields.append({"name": "\u26a0\ufe0f Signal Health Alerts", "value": alert_lines, "inline": False})
        embed = {
            "title": "\U0001f527 Auto-Tuner Cycle Report",
            "color": color,
            "fields": fields,
            "footer": self._footer(),
        }
        await self._post(self._attribution_url, embed)

    async def ml_data_ready(self, row_count: int, sample_win_rate: float) -> None:
        embed = {
            "title": "\U0001f9e0 ML Training Data Ready",
            "description": (
                f"**{row_count}** labeled paper trades with full feature coverage "
                f"(including MFE/MAE). Time to train the XGBoost entry gate model."
            ),
            "color": COLOR_PURPLE,
            "fields": [
                {"name": "Labeled Rows", "value": str(row_count), "inline": True},
                {"name": "Win Rate", "value": f"{sample_win_rate:.1%}", "inline": True},
                {"name": "Next Step", "value": "Run `python scripts/train_xgb.py`", "inline": False},
            ],
            "footer": self._footer(),
        }
        await self._post(self._attribution_url, embed)

    async def weekly_digest(
        self,
        week_start: str,
        week_end: str,
        total_pnl: float,
        total_trades: int,
        conviction_breakdown: dict,
        regime_breakdown: dict,
        session_breakdown: dict,
        fee_drag_pct: float,
        flipped_sessions: list[str],
        flipped_regimes: list[str],
    ) -> None:
        color = COLOR_GREEN if total_pnl >= 0 else COLOR_RED
        pnl_str = f"{'+'if total_pnl >= 0 else ''}${total_pnl:.2f}"

        fields = [
            {"name": "Period", "value": f"{week_start} \u2192 {week_end}", "inline": False},
            {"name": "Trades", "value": str(total_trades), "inline": True},
            {"name": "Net PnL", "value": pnl_str, "inline": True},
            {"name": "Fee Drag", "value": f"{fee_drag_pct:.1f}% of gross", "inline": True},
        ]

        if conviction_breakdown:
            lines = [f"{k}: ${v:+.2f}" for k, v in conviction_breakdown.items()]
            fields.append({"name": "PnL by Conviction", "value": "\n".join(lines), "inline": True})

        if regime_breakdown:
            lines = [f"{k}: ${v:+.2f}" for k, v in regime_breakdown.items()]
            fields.append({"name": "PnL by Regime", "value": "\n".join(lines), "inline": True})

        if session_breakdown:
            lines = [f"{k}: ${v:+.2f}" for k, v in session_breakdown.items()]
            fields.append({"name": "PnL by Session", "value": "\n".join(lines), "inline": True})

        alerts: list[str] = []
        if flipped_sessions:
            alerts.append(f"Sessions flipped unprofitable: {', '.join(flipped_sessions)}")
        if flipped_regimes:
            alerts.append(f"Regimes flipped unprofitable: {', '.join(flipped_regimes)}")
        if alerts:
            fields.append({"name": "\u26a0\ufe0f Drift Alerts", "value": "\n".join(alerts), "inline": False})

        embed = {
            "title": f"\U0001f4c5 Weekly Attribution Digest",
            "color": color,
            "fields": fields,
            "footer": self._footer(),
        }
        await self._post(self._attribution_url, embed)


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
            attribution_url=settings.bot.discord_attribution_webhook,
            live_trades_url=settings.bot.discord_live_trades_webhook,
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
        attribution_url=settings.bot.discord_attribution_webhook,
        live_trades_url=settings.bot.discord_live_trades_webhook,
    )
    return _notifier
