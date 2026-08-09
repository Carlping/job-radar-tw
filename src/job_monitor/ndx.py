from __future__ import annotations

from datetime import UTC, datetime

import httpx

NASDAQ_100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"


async def fetch_ndx_constituents(client: httpx.AsyncClient) -> tuple[list[dict], str]:
    response = await client.get(
        NASDAQ_100_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/quotes/nasdaq-ndx-index",
        },
    )
    response.raise_for_status()
    payload = response.json()
    rows = ((payload.get("data") or {}).get("data") or {}).get("rows") or []
    if len(rows) < 90:
        raise ValueError(
            f"Nasdaq response contained only {len(rows)} constituents; preserving previous snapshot"
        )
    return rows, datetime.now(UTC).date().isoformat()
