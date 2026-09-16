"""Thin HTTP client for the Polymarket US public gateway.

Only unauthenticated GET endpoints are used. No API key is ever loaded, so the
server is read-only by construction: it cannot place, modify, or cancel orders.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

GATEWAY_BASE_URL = os.environ.get("POLYMARKET_US_GATEWAY", "https://gateway.polymarket.us")
DEFAULT_TIMEOUT = float(os.environ.get("POLYMARKET_US_TIMEOUT", "20"))


class PolymarketUSError(Exception):
    """Raised for any gateway error, with an agent-actionable message."""


class GatewayClient:
    def __init__(self, base_url: str = GATEWAY_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "polymarket-us-mcp/0.1 (read-only)"},
        )

    async def get(self, path: str, **query: Any) -> Any:
        params: list[tuple[str, str]] = []
        for key, value in query.items():
            if value is None:
                continue
            if isinstance(value, list):
                params.extend((key, str(v)) for v in value)
            elif isinstance(value, bool):
                params.append((key, "true" if value else "false"))
            else:
                params.append((key, str(value)))
        url = f"{self._base}{path}"
        try:
            resp = await self._http.get(url, params=params)
        except httpx.TimeoutException as e:
            raise PolymarketUSError(f"Timeout calling {path}. Retry, or narrow the query (smaller limit).") from e
        except httpx.HTTPError as e:
            raise PolymarketUSError(f"Network error calling {path}: {e}") from e

        if resp.status_code == 404:
            raise PolymarketUSError(
                f"Not found: {path}. Check the slug/id (use pmus_search or pmus_list_events to discover slugs). "
                "For /settlement, 404 also means the market has not settled yet."
            )
        if resp.status_code == 400:
            raise PolymarketUSError(f"Bad request to {path}: {resp.text[:300]}")
        if resp.status_code == 429:
            raise PolymarketUSError("Rate limited by Polymarket US gateway (20 req/s per IP). Back off and retry.")
        if resp.status_code >= 500:
            raise PolymarketUSError(f"Polymarket US gateway error {resp.status_code}. Retry shortly.")
        if not resp.is_success:
            raise PolymarketUSError(f"HTTP {resp.status_code} from {path}: {resp.text[:300]}")
        if not resp.text:
            return {}
        return resp.json()

    async def aclose(self) -> None:
        await self._http.aclose()


_client: GatewayClient | None = None


def client() -> GatewayClient:
    global _client
    if _client is None:
        _client = GatewayClient()
    return _client
