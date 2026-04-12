#!/usr/bin/env python3
"""Directly query Kalshi API for open positions and wallet balance.
Standalone script — does not depend on the bot running."""

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
    print(f"API Key: {API_KEY_ID[:8]}...")
    print("=" * 60)

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as c:
        # 1. Check balance
        path = "/trade-api/v2/portfolio/balance"
        r = await c.get("/portfolio/balance", headers=get_headers(pk, "GET", path))
        r.raise_for_status()
        balance_data = r.json()
        balance_cents = balance_data.get("balance", 0)
        print(f"\nWALLET BALANCE: ${balance_cents / 100:.2f}")
        print(f"  Raw response: {balance_data}")

        # 2. Check positions
        path = "/trade-api/v2/portfolio/positions"
        r = await c.get("/portfolio/positions", headers=get_headers(pk, "GET", path))
        r.raise_for_status()
        positions_data = r.json()
        market_positions = positions_data.get("market_positions", [])

        open_positions = []
        for mp in market_positions:
            raw_pos = float(mp.get("position_fp", 0))
            contracts = abs(int(raw_pos))
            if contracts > 0:
                open_positions.append(mp)

        print(f"\nOPEN POSITIONS: {len(open_positions)}")
        if open_positions:
            print("-" * 60)
            for mp in open_positions:
                ticker = mp.get("ticker", "?")
                raw_pos = float(mp.get("position_fp", 0))
                contracts = abs(int(raw_pos))
                direction = "LONG" if raw_pos > 0 else "SHORT"
                total_cost = float(mp.get("total_traded_dollars", 0))
                avg_entry = round((total_cost / contracts) * 100) if contracts > 0 else 0
                print(f"  {ticker}: {direction} x{contracts} @ ~{avg_entry}c")
                print(f"    Raw: {mp}")
        else:
            print("  *** NO OPEN POSITIONS — ALL CLEAR ***")

        # 3. Check resting orders
        path = "/trade-api/v2/portfolio/orders"
        r = await c.get("/portfolio/orders", headers=get_headers(pk, "GET", path),
                        params={"status": "resting", "limit": 100})
        r.raise_for_status()
        orders_data = r.json()
        resting = orders_data.get("orders", [])
        print(f"\nRESTING ORDERS: {len(resting)}")
        for o in resting:
            print(f"  {o.get('ticker')}: {o.get('side')} x{o.get('count')} "
                  f"@ yes={o.get('yes_price')} no={o.get('no_price')} "
                  f"id={o.get('order_id')}")

    print("\n" + "=" * 60)
    if open_positions:
        print("WARNING: You have open positions on Kalshi!")
    else:
        print("CONFIRMED: No open positions on Kalshi.")


if __name__ == "__main__":
    asyncio.run(main())
