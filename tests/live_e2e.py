"""Live end-to-end check: spawn the server over stdio and call every tool.

Run:  python tests/live_e2e.py
Needs network access to gateway.polymarket.us. Exits non-zero on any failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def call(s: ClientSession, name: str, args: dict, show: int = 400) -> dict:
    res = await s.call_tool(name, args)
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    data = structured or json.loads(res.content[0].text)
    if isinstance(data, dict) and "result" in data and len(data) == 1:
        data = data["result"]
    is_error = getattr(res, "is_error", None) or getattr(res, "isError", None)
    status = "ERR " if (is_error or (isinstance(data, dict) and "error" in data)) else "ok  "
    print(f"[{status}] {name}({json.dumps(args)}) -> {json.dumps(data)[:show]}")
    if status.strip() == "ERR":
        raise SystemExit(f"tool {name} failed: {data}")
    return data


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "polymarket_us_mcp.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)
            def ro(t) -> bool:
                a = t.annotations
                return bool(a and (getattr(a, "read_only_hint", None) or getattr(a, "readOnlyHint", None)))
            assert all(ro(t) for t in tools.tools), "every tool must be readOnly"

            ev = await call(s, "pmus_list_events", {"limit": 3, "category": "politics"})
            assert ev["count"] >= 1
            e0 = ev["events"][0]
            slug = e0["markets"][0]["slug"]

            sr = await call(s, "pmus_search", {"query": "Fed", "limit": 3})
            assert sr["count"] >= 1
            await call(s, "pmus_get_event", {"slug_or_id": e0["slug"]})
            await call(s, "pmus_get_event", {"slug_or_id": str(e0["id"])})
            mk = await call(s, "pmus_get_market", {"slug_or_id": slug})
            assert mk["tick_size"] and mk["fee_coefficient"] is not None
            await call(s, "pmus_get_market", {"slug_or_id": str(mk["id"])})
            await call(s, "pmus_list_markets", {"limit": 3})
            bbo = await call(s, "pmus_get_bbo", {"slug": slug})
            assert bbo["best_bid"] is not None and bbo["best_ask"] is not None
            book = await call(s, "pmus_get_order_book", {"slug": slug, "depth": 3})
            assert book["bids"] and book["offers"]
            await call(s, "pmus_get_settlement", {"slug": slug})            # open -> settled False
            closed = await call(s, "pmus_list_events", {"limit": 1, "active": None, "closed": True, "order_by": "endDate"})
            if closed["count"]:
                cs = closed["events"][0]["markets"][0]["slug"]
                st = await call(s, "pmus_get_settlement", {"slug": cs})
                assert "settled" in st
            await call(s, "pmus_get_price_history", {"slug": slug})
            await call(s, "pmus_list_series", {"limit": 3})
            sp = await call(s, "pmus_list_sports", {})
            assert sp["count"] >= 1
            await call(s, "pmus_list_tags", {"limit": 3})
            await call(s, "pmus_list_tags", {"slug": "politics"})

            an_ = await call(s, "pmus_analyze_spread", {"slug": slug, "size": 50}, show=1200)
            for k in ("spread_ticks", "taker", "maker_join_best", "inside_spread_ladder", "liquidity", "execution_sim"):
                assert k in an_, f"missing {k}"
            scan = await call(s, "pmus_scan_extreme_markets", {"pages": 1, "limit": 5}, show=1500)
            assert scan["markets_scanned"] > 0
            bk = await call(s, "pmus_event_basket", {"event_slug": e0["slug"]}, show=800)
            assert bk["n_markets"] >= 1
            sports = await call(s, "pmus_list_events", {"limit": 5, "category": "sports"})
            ev_slug = sports["events"][0]["slug"] if sports["count"] else e0["slug"]
            lad = await call(s, "pmus_line_ladder_check", {"event_slug": ev_slug}, show=800)
            assert "hard_violations" in lad and "label_conflicts" in lad

            # error path: unknown slug returns actionable error, not a crash
            res = await s.call_tool("pmus_get_bbo", {"slug": "definitely-not-a-market"})
            txt = res.content[0].text
            assert "Not found" in txt, txt
            print("[ok  ] error path:", txt[:120])
    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
