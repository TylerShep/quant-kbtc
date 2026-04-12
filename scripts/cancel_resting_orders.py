#!/usr/bin/env python3
"""Cancel all resting orders on Kalshi."""

import asyncio
import os
import sys
import time
import base64
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
KALSHI_ENV = os.environ.get("KALSHI_ENV", "demo")
BASE_URL = ("https://api.elections.kalshi.com/trade-api/v2" if KALSHI_ENV == "prod"
            else "https://demo-api.kalshi.co/trade-api/v2")


def load_key():
    for p in [Path(KEY_PATH),
              Path(os.path.dirname(__file__)) / ".." / "backend" / "kalshi_private_key.pem",
              Path(os.path.dirname(__file__)) / ".." / "kalshi_private_key.pem"]:
        if p.exists():
            with open(p, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
    raise FileNotFoundError("Key not found")


def sign(pk, msg):
    return base64.b64encode(pk.sign(
        msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                   salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256()
    )).decode()


def headers(pk, method, path):
    ts = str(int(time.time() * 1000))
    return {"KALSHI-ACCESS-KEY": API_KEY_ID, "KALSHI-ACCESS-SIGNATURE": sign(pk, ts + method.upper() + path),
            "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}


async def main():
    pk = load_key()
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as c:
        path = "/trade-api/v2/portfolio/orders"
        r = await c.get("/portfolio/orders", headers=headers(pk, "GET", path),
                        params={"status": "resting", "limit": 100})
        r.raise_for_status()
        orders = r.json().get("orders", [])
        print(f"Found {len(orders)} resting order(s)")
        for o in orders:
            oid = o.get("order_id")
            cancel_path = f"/trade-api/v2/portfolio/orders/{oid}"
            r2 = await c.delete(f"/portfolio/orders/{oid}", headers=headers(pk, "DELETE", cancel_path))
            print(f"  Cancel {oid}: HTTP {r2.status_code}")
        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
