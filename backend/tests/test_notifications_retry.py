"""Tests for DiscordNotifier._post retry behavior (BUG-029).

Without these tests, the regression that silently dropped trade_closed
notifications on Discord 5xx outages would re-appear the next time someone
"simplifies" the retry loop.

We patch ``httpx.AsyncClient`` inside the notifications module to return a
canned sequence of responses, so each test exercises one branch of the
retry policy without hitting Discord.
"""
from __future__ import annotations

import asyncio
from typing import Iterable
from unittest.mock import patch

import httpx
import pytest

from notifications import DiscordNotifier


WEBHOOK = "https://discord.com/api/webhooks/test/abc"


class _ScriptedResponses:
    """Yield a scripted sequence of httpx.Response objects.

    Each call to ``post`` consumes the next scripted item; if the item is
    an Exception subclass it is raised, otherwise it's returned as the
    response. After the script is exhausted, the last entry is reused.
    """

    def __init__(self, script: Iterable):
        self._script = list(script)
        self.calls: list[dict] = []

    def make_client(self, *_args, **_kwargs):  # acts as AsyncClient(...)
        outer = self

        class _FakeClient:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_a):
                return False

            async def post(self_inner, url, json=None, **_kw):
                outer.calls.append({"url": url, "payload": json})
                idx = min(len(outer.calls) - 1, len(outer._script) - 1)
                item = outer._script[idx]
                if isinstance(item, BaseException):
                    raise item
                return item

        return _FakeClient()


def _resp(status: int, body: bytes = b"{}") -> httpx.Response:
    return httpx.Response(status_code=status, content=body)


async def _no_sleep(*_a, **_kw):
    return None


def _run(coro):
    """Run a coroutine on a private loop without closing the global default.

    Using ``asyncio.run`` here breaks downstream tests that rely on
    ``asyncio.get_event_loop`` returning a usable loop on Python 3.12.
    """
    async def _wrapper():
        with patch("notifications.asyncio.sleep", new=_no_sleep):
            return await coro

    loop = asyncio.new_event_loop()
    prev = None
    try:
        try:
            prev = asyncio.get_event_loop()
        except RuntimeError:
            prev = None
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_wrapper())
    finally:
        loop.close()
        if prev is not None and not prev.is_closed():
            asyncio.set_event_loop(prev)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())


def test_post_succeeds_on_204():
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    script = _ScriptedResponses([_resp(204)])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "ok"}))
    assert len(script.calls) == 1


def test_post_retries_on_5xx_then_succeeds():
    """503 -> 502 -> 204 must NOT silently drop the message."""
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    script = _ScriptedResponses([_resp(503), _resp(502), _resp(204)])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "trade_closed"}))
    assert len(script.calls) == 3, "expected 2 retries before success"


def test_post_retries_on_network_exception_then_succeeds():
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    script = _ScriptedResponses([
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timeout"),
        _resp(204),
    ])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "trade_closed"}))
    assert len(script.calls) == 3


def test_post_gives_up_on_4xx_other_than_429():
    """400 = bad payload, retrying won't help. Must fail fast (1 call)."""
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    script = _ScriptedResponses([_resp(400, b'{"message":"bad"}')])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "x"}))
    assert len(script.calls) == 1


def test_post_caps_5xx_retries_at_max_attempts():
    """Persistent 503 must eventually give up, not loop forever."""
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    script = _ScriptedResponses([_resp(503)] * 20)
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "x"}))
    assert len(script.calls) == 8, f"MAX_ATTEMPTS=8, got {len(script.calls)}"


def test_post_retries_on_429_then_succeeds():
    notifier = DiscordNotifier(trades_url=WEBHOOK)
    rate_limited = httpx.Response(
        status_code=429,
        content=b'{"retry_after": 0.01}',
        headers={"Retry-After": "0.01"},
    )
    script = _ScriptedResponses([rate_limited, _resp(204)])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post(WEBHOOK, {"title": "x"}))
    assert len(script.calls) == 2


def test_post_no_op_when_url_blank():
    """Empty webhook URL = silent no-op (used when channel not configured)."""
    notifier = DiscordNotifier(trades_url="")
    script = _ScriptedResponses([_resp(204)])
    with patch("notifications.httpx.AsyncClient", side_effect=script.make_client):
        _run(notifier._post("", {"title": "x"}))
    assert len(script.calls) == 0
