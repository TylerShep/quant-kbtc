#!/usr/bin/env python3
"""Force-close ALL open Kalshi positions via market orders.
Standalone — does not require the bot to be running."""

import asyncio
import os
import sys
import time
import base64
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo")

if KALSHI_ENV == "prod":
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
else:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


def load_key():
    candidates = [
        Path(KEY_PATH),
        Path(os.path.dirname(__file__)) / ".." / "backend" / "kalshi_private_key.pem",
        Path(os.path.dirname(__file__)) / ".." / "kalshi_private_key.pem",
        Path(os.path.dirname(__file__)) / ".." / KEY_PATH,
    ]
    for p in candidates:
        if p.exists():
            with open(p, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
    raise FileNotFoundError(f"Private key not found. Tried: {[str(c) for c in candidates]}")


def sign(private_key, message: str) -> str:
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def get_headers(private_key, method: str, path: str) -> dict:
    ts_ms = str(int(time.time() * 1000))
    msg = ts_ms + method.upper() + path
    return {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, msg),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }


async def main():
    pk = load_key()
    print(f"Environment: {KALSHI_ENV}")
    print(f"Base URL: {BASE_URL}")
    print("=" * 60)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as c:
        # Get positions
        path = "/trade-api/v2/portfolio/positions"
        r = await c.get("/portfolio/positions", headers=get_headers(pk, "GET", path))
        r.raise_for_status()
        market_positions = r.json().get("market_positions", [])

        open_positions = []
        for mp in market_positions:
            raw_pos = float(mp.get("position_fp", 0))
            contracts = abs(int(raw_pos))
            if contracts > 0:
                direction = "long" if raw_pos > 0 else "short"
                open_positions.append({
                    "ticker": mp.get("ticker"),
                    "direction": direction,
                    "contracts": contracts,
                })

        if not open_positions:
            print("No open positions found. Nothing to close.")
            return

        print(f"Found {len(open_positions)} open position(s). Closing...\n")

        for pos in open_positions:
            ticker = pos["ticker"]
            direction = pos["direction"]
            contracts = pos["contracts"]

            print(f"Closing {ticker}: {direction.upper()} x{contracts}")

            path = "/trade-api/v2/portfolio/orders"
            # To close a LONG: sell yes (action=sell, side=yes) at worst price (yes_price=1)
            # To close a SHORT: sell no (action=sell, side=no) at worst price (no_price=1)
            strategies = []
            if direction == "long":
                strategies = [
                    {"action": "sell", "side": "yes", "yes_price": 1},
                    {"action": "buy", "side": "no", "no_price": 1},
                ]
            else:
                strategies = [
                    {"action": "sell", "side": "no", "no_price": 1},
                    {"action": "buy", "side": "yes", "yes_price": 1},
                ]

            placed = False
            for strat in strategies:
                body = {
                    "ticker": ticker,
                    "count": contracts,
                    "type": "market",
                    **strat,
                }
                desc = f"action={strat['action']}, side={strat['side']}"
                print(f"  Trying: {desc}")
                try:
                    r = await c.post("/portfolio/orders",
                                     headers=get_headers(pk, "POST", path),
                                     json=body)
                    if r.status_code in (200, 201):
                        placed = True
                        result = r.json()
                        break
                    else:
                        print(f"    HTTP {r.status_code}: {r.text[:200]}")
                except Exception as e:
                    print(f"    Error: {e}")

            if not placed:
                print(f"  FAILED to close {ticker} — all strategies rejected")
                continue

            order_id = result.get("order", {}).get("order_id", "?")
            status = result.get("order", {}).get("status", "?")
            print(f"  Order placed: id={order_id}, status={status}")

            for _ in range(10):
                await asyncio.sleep(1)
                check_path = f"/trade-api/v2/portfolio/orders/{order_id}"
                try:
                    r2 = await c.get(f"/portfolio/orders/{order_id}",
                                     headers=get_headers(pk, "GET", check_path))
                    r2.raise_for_status()
                    od = r2.json().get("order", {})
                    s = od.get("status", "")
                    fc = od.get("fill_count_fp", od.get("filled_count", 0))
                    print(f"  Poll: status={s}, filled={fc}")
                    if s in ("filled", "executed", "canceled", "cancelled"):
                        break
                except Exception as e:
                    print(f"  Poll error: {e}")

            print()

        # Verify
        print("=" * 60)
        print("VERIFICATION — re-checking positions...\n")
        await asyncio.sleep(2)

        path = "/trade-api/v2/portfolio/positions"
        r = await c.get("/portfolio/positions", headers=get_headers(pk, "GET", path))
        r.raise_for_status()
        remaining = [
            mp for mp in r.json().get("market_positions", [])
            if abs(int(float(mp.get("position_fp", 0)))) > 0
        ]

        if remaining:
            print(f"WARNING: {len(remaining)} position(s) still open!")
            for mp in remaining:
                print(f"  {mp.get('ticker')}: position_fp={mp.get('position_fp')}")
        else:
            print("CONFIRMED: ALL POSITIONS CLOSED.")

        # Final balance
        path = "/trade-api/v2/portfolio/balance"
        r = await c.get("/portfolio/balance", headers=get_headers(pk, "GET", path))
        r.raise_for_status()
        bal = r.json()
        print(f"\nFinal wallet balance: ${bal.get('balance', 0) / 100:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
