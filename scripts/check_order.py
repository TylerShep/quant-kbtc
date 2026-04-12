import asyncio
from data.kalshi_ws import KalshiOrderClient

async def main():
    client = KalshiOrderClient()
    order = await client.get_order("66e0471a-5271-458d-a09f-b1e164074139")
    od = order.get("order", {})
    for k in ("status", "filled_count", "remaining_count", "side", "type", "action", "ticker", "yes_price", "no_price"):
        print(f"  {k}: {od.get(k)}")

asyncio.run(main())
