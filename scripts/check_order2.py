import asyncio, json
from data.kalshi_ws import KalshiOrderClient

async def main():
    client = KalshiOrderClient()
    # Full order detail
    order = await client.get_order("66e0471a-5271-458d-a09f-b1e164074139")
    print("=== Full order ===")
    print(json.dumps(order, indent=2, default=str))

    # Market status
    market = await client.get_market("KXBTC-26APR1014-B72950")
    mk = market.get("market", {})
    print("\n=== Market ===")
    for k in ("status", "result", "close_time"):
        print(f"  {k}: {mk.get(k)}")

asyncio.run(main())
