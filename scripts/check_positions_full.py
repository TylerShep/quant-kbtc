import asyncio, json
from data.kalshi_ws import KalshiOrderClient

async def main():
    client = KalshiOrderClient()
    data = await client.get_positions()
    positions = data.get("market_positions", [])
    btc = [p for p in positions if "KXBTC-26APR1014" in p.get("ticker", "")]
    print(f"KXBTC-26APR1014 positions: {len(btc)}")
    for p in btc:
        print(json.dumps(p, indent=2, default=str))

asyncio.run(main())
