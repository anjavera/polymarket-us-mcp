"""Polymarket US read-only MCP server (FastMCP, stdio).

Tools cover the public gateway (events, markets, book/BBO, settlement, search,
series, sports, tags, price history) plus analytics: spread/fee/payoff
breakdowns, an extreme-probability scanner, and event basket checks.

No API key is loaded anywhere. Nothing here can trade.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from polymarket_us_mcp import analytics as an
from polymarket_us_mcp.client import PolymarketUSError, client

mcp = FastMCP(
    "polymarket-us",
    instructions=(
        "Read-only access to Polymarket US (the CFTC-regulated exchange, not the global "
        "Polymarket). Prices are USD 0-1 = implied probability of YES. Markets are single "
        "instruments: long = YES, short = synthetic NO. Discover markets with "
        "pmus_search / pmus_list_events, then use pmus_analyze_spread for fee-adjusted "
        "payoffs and inside-spread opportunities, pmus_scan_extreme_markets for "
        "high/low-probability markets, pmus_event_basket for multi-outcome pricing, and "
        "pmus_line_ladder_check for impossible pricing across spread/total lines. If a market "
        "carries label_warning, its question and rules text disagree about what YES means."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _trunc(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _market_summary(m: dict[str, Any], desc_chars: int = 0) -> dict[str, Any]:
    bid, ask = an.num(m.get("bestBidQuote")), an.num(m.get("bestAskQuote"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    out = {
        "slug": m.get("slug"),
        "id": m.get("id"),
        "outcome": m.get("title") or m.get("titleShort"),
        "question": m.get("question"),
        "best_bid": bid,
        "best_ask": ask,
        "implied_prob_yes": an.r4(mid),
        "spread": an.r4(ask - bid) if mid is not None else None,
        "tick_size": m.get("orderPriceMinTickSize"),
        "fee_coefficient": m.get("feeCoefficient"),
        "status": m.get("status"),
        "active": m.get("active"),
        "closed": m.get("closed"),
        "market_type": m.get("marketType"),
        "end_date": m.get("endDate"),
        "min_trade_qty": m.get("minimumTradeQty"),
    }
    long_side = next((x.get("description") for x in (m.get("marketSides") or []) if isinstance(x, dict) and x.get("long")), None)
    if long_side:
        out["long_side"] = long_side
    warn = an.spread_label_check(m.get("question"), m.get("title"), m.get("description"))
    if warn:
        out["label_warning"] = warn
    if desc_chars:
        out["description"] = _trunc(m.get("description"), desc_chars)
    return out


def _event_summary(e: dict[str, Any], desc_chars: int = 0, max_markets: int = 50) -> dict[str, Any]:
    mkts = e.get("markets") or []
    out = {
        "slug": e.get("slug"),
        "id": e.get("id"),
        "title": e.get("title"),
        "category": e.get("category"),
        "series_slug": e.get("seriesSlug"),
        "start_date": e.get("startDate"),
        "end_date": e.get("endDate"),
        "active": e.get("active"),
        "closed": e.get("closed"),
        "tags": [t.get("slug") for t in (e.get("tags") or []) if isinstance(t, dict)],
        "market_count": len(mkts),
        "markets": [_market_summary(m, desc_chars) for m in mkts[:max_markets]],
    }
    if len(mkts) > max_markets:
        out["markets_truncated"] = len(mkts) - max_markets
    if desc_chars:
        out["description"] = _trunc(e.get("description"), desc_chars)
    return out


async def _fetch_market(slug: str) -> dict[str, Any]:
    data = await client().get(f"/v1/market/slug/{slug}")
    return data.get("market", data)


async def _fetch_bbo(slug: str) -> dict[str, Any]:
    data = await client().get(f"/v1/markets/{slug}/bbo")
    return data.get("marketData", data)


async def _fetch_book(slug: str) -> dict[str, Any]:
    data = await client().get(f"/v1/markets/{slug}/book")
    return data.get("marketData", data)


def _err(e: Exception) -> dict[str, Any]:
    return {"error": str(e)}


# --------------------------------------------------------------------------- #
# Discovery tools
# --------------------------------------------------------------------------- #
@mcp.tool(name="pmus_search", annotations=RO)
async def pmus_search(
    query: str = Field(description="Free-text search, e.g. 'Fed rate cut', 'bitcoin', 'Senate'"),
    limit: int = Field(default=10, ge=1, le=50),
    status: Literal["active", "closed", "upcoming"] | None = Field(default="active"),
) -> dict[str, Any]:
    """Search Polymarket US events by text. Returns compact event + market summaries
    with current best bid/ask so you can pick a market slug for deeper tools."""
    try:
        data = await client().get("/v1/search", query=query, limit=limit, status=status)
    except PolymarketUSError as e:
        return _err(e)
    events = data.get("events") or []
    return {"count": len(events), "events": [_event_summary(e) for e in events]}


@mcp.tool(name="pmus_list_events", annotations=RO)
async def pmus_list_events(
    limit: int = Field(default=20, ge=1, le=100),
    offset: int = Field(default=0, ge=0),
    active: bool | None = Field(default=True),
    closed: bool | None = Field(default=False),
    category: str | None = Field(default=None, description="e.g. politics, sports, macro, crypto, culture"),
    tag_slug: str | None = Field(default=None, description="Tag slug from pmus_list_tags, e.g. 'elections'"),
    series_id: int | None = Field(default=None),
    order_by: str | None = Field(default="volume", description="Field to sort by, e.g. volume, startDate, endDate"),
    order_direction: Literal["asc", "desc"] = Field(default="desc"),
    end_date_min: str | None = Field(default=None, description="ISO date, e.g. 2026-10-01"),
    end_date_max: str | None = Field(default=None, description="ISO date"),
    description_chars: int = Field(default=0, ge=0, le=2000, description="Include truncated descriptions (0 = omit)"),
) -> dict[str, Any]:
    """List events with filters and pagination. Each event includes its markets with
    best bid/ask and implied probability. Use offset to page."""
    try:
        data = await client().get(
            "/v1/events", limit=limit, offset=offset, active=active, closed=closed,
            categories=category, tagSlug=tag_slug, seriesId=series_id,
            orderBy=order_by, orderDirection=order_direction,
            endDateMin=end_date_min, endDateMax=end_date_max,
        )
    except PolymarketUSError as e:
        return _err(e)
    events = data.get("events") or []
    return {
        "count": len(events), "offset": offset,
        "next_offset": offset + len(events) if len(events) == limit else None,
        "events": [_event_summary(e, description_chars) for e in events],
    }


@mcp.tool(name="pmus_get_event", annotations=RO)
async def pmus_get_event(
    slug_or_id: str = Field(description="Event slug (e.g. 'usse-midterms-2026-11-03') or numeric id"),
    description_chars: int = Field(default=600, ge=0, le=5000),
) -> dict[str, Any]:
    """Get one event with all of its markets, rules excerpt, tags, and quotes."""
    path = f"/v1/events/{slug_or_id}" if slug_or_id.isdigit() else f"/v1/events/slug/{slug_or_id}"
    try:
        data = await client().get(path)
    except PolymarketUSError as e:
        return _err(e)
    e = data.get("event", data)
    out = _event_summary(e, description_chars, max_markets=200)
    out["market_groups"] = e.get("marketGroups")
    out["market_counts"] = e.get("marketCounts")
    return out


@mcp.tool(name="pmus_list_markets", annotations=RO)
async def pmus_list_markets(
    limit: int = Field(default=20, ge=1, le=100),
    offset: int = Field(default=0, ge=0),
    active: bool | None = Field(default=True),
    closed: bool | None = Field(default=False),
    category: str | None = Field(default=None),
    order_by: str | None = Field(default=None),
    order_direction: Literal["asc", "desc"] = Field(default="desc"),
    description_chars: int = Field(default=0, ge=0, le=2000),
) -> dict[str, Any]:
    """List markets directly (flat, not grouped by event). For an event's markets
    prefer pmus_get_event — the gateway's eventSlug filter is unreliable."""
    try:
        data = await client().get(
            "/v1/markets", limit=limit, offset=offset, active=active, closed=closed,
            categories=category, orderBy=order_by, orderDirection=order_direction,
        )
    except PolymarketUSError as e:
        return _err(e)
    mkts = data.get("markets") or []
    return {
        "count": len(mkts), "offset": offset,
        "next_offset": offset + len(mkts) if len(mkts) == limit else None,
        "markets": [_market_summary(m, description_chars) for m in mkts],
    }


@mcp.tool(name="pmus_get_market", annotations=RO)
async def pmus_get_market(
    slug_or_id: str = Field(description="Market slug (e.g. 'paccc-usse-midterms-2026-11-03-dem') or numeric id"),
    description_chars: int = Field(default=1500, ge=0, le=10000, description="Rules text length to include"),
) -> dict[str, Any]:
    """Get one market: rules/settlement text, tick size, fee coefficient, sides, quotes."""
    path = f"/v1/market/id/{slug_or_id}" if slug_or_id.isdigit() else f"/v1/market/slug/{slug_or_id}"
    try:
        data = await client().get(path)
    except PolymarketUSError as e:
        return _err(e)
    m = data.get("market", data)
    out = _market_summary(m, description_chars)
    out.update({
        "event_slug": m.get("eventSlug"),
        "category": m.get("category"),
        "start_date": m.get("startDate"),
        "game_start_time": m.get("gameStartTime"),
        "outcomes": m.get("outcomes"),
        "sides": [
            {"description": s.get("description"), "long": s.get("long"), "price": an.num(s.get("price")),
             "quote": an.num(s.get("quote")), "tradable": s.get("tradable")}
            for s in (m.get("marketSides") or []) if isinstance(s, dict)
        ],
        "tags": [t.get("slug") for t in (m.get("tags") or []) if isinstance(t, dict)],
        "combo_enabled": m.get("comboEnabled"),
    })
    return out


# --------------------------------------------------------------------------- #
# Market data tools
# --------------------------------------------------------------------------- #
@mcp.tool(name="pmus_get_bbo", annotations=RO)
async def pmus_get_bbo(slug: str = Field(description="Market slug")) -> dict[str, Any]:
    """Best bid/offer snapshot: bid, ask, last trade, depth counts, open interest,
    long/short display quotes, and market state."""
    try:
        d = await _fetch_bbo(slug)
    except PolymarketUSError as e:
        return _err(e)
    bid, ask = an.num(d.get("bestBid")), an.num(d.get("bestAsk"))
    return {
        "market_slug": d.get("marketSlug", slug),
        "state": d.get("state"),
        "best_bid": bid, "best_ask": ask,
        "mid": an.r4((bid + ask) / 2) if bid is not None and ask is not None else None,
        "spread": an.r4(ask - bid) if bid is not None and ask is not None else None,
        "current_px": an.num(d.get("currentPx")),
        "last_trade_px": an.num(d.get("lastTradePx")),
        "long_quote_yes": an.num(d.get("longQuote")),
        "short_quote_no": an.num(d.get("shortQuote")),
        "bid_levels": d.get("bidDepth"), "ask_levels": d.get("askDepth"),
        "bid_shares": an.num(d.get("bidShares")), "ask_shares": an.num(d.get("askShares")),
        "shares_traded": an.num(d.get("sharesTraded")),
        "open_interest": an.num(d.get("openInterest")),
        "settlement_px": an.num(d.get("settlementPx")),
        "last_sample_ts": (d.get("lastPriceSample") or {}).get("ts"),
    }


@mcp.tool(name="pmus_get_order_book", annotations=RO)
async def pmus_get_order_book(
    slug: str = Field(description="Market slug"),
    depth: int = Field(default=10, ge=1, le=200, description="Levels per side to return"),
) -> dict[str, Any]:
    """Order book (bids and offers, best first) with per-level price and quantity,
    cumulative size, and market stats."""
    try:
        d = await _fetch_book(slug)
    except PolymarketUSError as e:
        return _err(e)

    def levels(side: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out, cum = [], 0.0
        for lvl in side[:depth]:
            px, qty = an.num(lvl.get("px")), an.num(lvl.get("qty")) or 0.0
            cum += qty
            out.append({"px": px, "qty": qty, "cum_qty": round(cum, 2)})
        return out

    bids, offers = d.get("bids") or [], d.get("offers") or []
    stats = d.get("stats") or {}
    return {
        "market_slug": d.get("marketSlug", slug),
        "state": d.get("state"),
        "transact_time": d.get("transactTime"),
        "bid_levels_total": len(bids), "ask_levels_total": len(offers),
        "bids": levels(bids), "offers": levels(offers),
        "stats": {k: an.num(v) if isinstance(v, (dict, str)) else v for k, v in stats.items()},
    }


@mcp.tool(name="pmus_get_settlement", annotations=RO)
async def pmus_get_settlement(slug: str = Field(description="Market slug")) -> dict[str, Any]:
    """Settlement result for a closed market (1 = YES, 0 = NO, 0.5 = void/50-50).
    Returns settled=false if the market has not settled yet."""
    try:
        d = await client().get(f"/v1/markets/{slug}/settlement")
    except PolymarketUSError as e:
        if "Not found" in str(e):
            return {"market_slug": slug, "settled": False, "note": "No settlement yet (or unknown slug)."}
        return _err(e)
    val = an.num(d.get("settlement", d.get("settlementPrice")))
    return {
        "market_slug": d.get("slug", d.get("marketSlug", slug)),
        "settled": True,
        "settlement_value": val,
        "outcome": {1.0: "YES", 0.0: "NO", 0.5: "VOID/50-50"}.get(val, "partial/other"),
        "settled_at": d.get("settledAt"),
    }


@mcp.tool(name="pmus_get_price_history", annotations=RO)
async def pmus_get_price_history(
    slug: str = Field(description="Market slug"),
    interval: Literal["INTERVAL_ALL", "INTERVAL_1M", "INTERVAL_1W", "INTERVAL_1D", "INTERVAL_6H", "INTERVAL_1H", "INTERVAL_LIVE"] | None = Field(
        default="INTERVAL_1W", description="Fixed lookback window; ignored if start/end given"),
    fidelity: int = Field(default=60, ge=1, description="Sample resolution in minutes"),
    start_ts: int | None = Field(default=None, description="Unix seconds (use with end_ts instead of interval)"),
    end_ts: int | None = Field(default=None),
    max_points: int = Field(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """Historical long (YES, from best ask) and short (NO, from 1 - best bid) prices.
    Note: the gateway currently returns empty history for many markets."""
    q: dict[str, Any] = {"symbol": slug, "fidelity": fidelity}
    if start_ts is not None and end_ts is not None:
        q["timestamp.startTimestamp"] = start_ts
        q["timestamp.endTimestamp"] = end_ts
    else:
        q["fixedInterval"] = interval
    try:
        d = await client().get("/v1/price-history", **q)
    except PolymarketUSError as e:
        return _err(e)
    hist = d.get("history") or []
    if len(hist) > max_points:
        step = len(hist) / max_points
        hist = [hist[int(i * step)] for i in range(max_points)]
    first, last = (hist[0], hist[-1]) if hist else (None, None)
    return {
        "market_slug": slug, "points": len(hist),
        "first": first, "last": last,
        "change_long": an.r4(an.num(last.get("longPrice")) - an.num(first.get("longPrice")))
        if first and last and an.num(first.get("longPrice")) is not None and an.num(last.get("longPrice")) is not None else None,
        "history": hist,
        "note": None if hist else "Empty history returned by gateway for this query.",
    }


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
@mcp.tool(name="pmus_list_series", annotations=RO)
async def pmus_list_series(
    limit: int = Field(default=50, ge=1, le=200), offset: int = Field(default=0, ge=0),
) -> dict[str, Any]:
    """List series (recurring groupings like 'nfl-2025', 'fed', 'us-midterms-2026')."""
    try:
        d = await client().get("/v1/series", limit=limit, offset=offset)
    except PolymarketUSError as e:
        return _err(e)
    s = d.get("series") or []
    return {"count": len(s), "series": [{"id": x.get("id"), "slug": x.get("slug"), "title": x.get("title")} for x in s]}


@mcp.tool(name="pmus_list_sports", annotations=RO)
async def pmus_list_sports(operational_only: bool = Field(default=True)) -> dict[str, Any]:
    """List sports/leagues supported by Polymarket US with their tag and series ids."""
    try:
        d = await client().get("/v1/sports")
    except PolymarketUSError as e:
        return _err(e)
    sports = d.get("sports") or []
    if operational_only:
        sports = [s for s in sports if s.get("isOperational")]
    return {"count": len(sports), "sports": [
        {"sport": s.get("sport"), "tag_id": s.get("tags"), "series_id": s.get("series"),
         "operational": s.get("isOperational"), "resolution_source": s.get("resolution")} for s in sports]}


@mcp.tool(name="pmus_list_tags", annotations=RO)
async def pmus_list_tags(
    slug: str | None = Field(default=None, description="Get one tag (with subtags) by slug, e.g. 'politics'"),
    limit: int = Field(default=50, ge=1, le=200), offset: int = Field(default=0, ge=0),
) -> dict[str, Any]:
    """List tags, or fetch one tag with its subtags. Tag slugs feed pmus_list_events(tag_slug=...)."""
    try:
        if slug:
            d = await client().get(f"/v1/tags/slug/{slug}")
            t = d.get("tag", d)
            return {"id": t.get("id"), "slug": t.get("slug"), "label": t.get("label"),
                    "subtags": [{"id": s.get("id"), "slug": s.get("slug"), "label": s.get("label")} for s in (t.get("subtags") or [])]}
        d = await client().get("/v1/tags", limit=limit, offset=offset)
    except PolymarketUSError as e:
        return _err(e)
    tags = d.get("tags") or []
    return {"count": len(tags), "tags": [{"id": t.get("id"), "slug": t.get("slug"), "label": t.get("label"),
                                          "subtag_count": len(t.get("subtags") or [])} for t in tags]}


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@mcp.tool(name="pmus_analyze_spread", annotations=RO)
async def pmus_analyze_spread(
    slug: str = Field(description="Market slug"),
    size: float | None = Field(default=None, gt=0, description="Optional order size (contracts) to simulate walking the book"),
    taker_fee_override: float | None = Field(default=None, ge=0, le=0.2, description="Override taker theta (default: market feeCoefficient)"),
) -> dict[str, Any]:
    """Full spread + fee + payoff breakdown for one market: bid/ask/mid, spread in ticks,
    taker vs maker economics for long-YES and short-NO, breakeven probabilities,
    annualized ROI to end date, inside-spread resting-order ladder, depth near the
    touch, and (optionally) execution simulation for a given size."""
    try:
        m, bbo, book = await asyncio.gather(_fetch_market(slug), _fetch_bbo(slug), _fetch_book(slug))
    except PolymarketUSError as e:
        return _err(e)
    theta = taker_fee_override if taker_fee_override is not None else (an.num(m.get("feeCoefficient")) or an.DEFAULT_TAKER_THETA)
    out = an.analyze_spread(
        slug=slug,
        bid=an.num(bbo.get("bestBid")), ask=an.num(bbo.get("bestAsk")),
        tick=an.num(m.get("orderPriceMinTickSize")),
        end_date=m.get("endDate"), taker_theta=theta,
        bids=book.get("bids") or [], offers=book.get("offers") or [],
        size=size, last_trade=an.num(bbo.get("lastTradePx")), open_interest=an.num(bbo.get("openInterest")),
    )
    out["market"] = {"question": m.get("question"), "outcome": m.get("title"), "state": bbo.get("state"), "end_date": m.get("endDate")}
    warn = an.spread_label_check(m.get("question"), m.get("title"), m.get("description"))
    if warn:
        out["market"]["label_warning"] = warn
    return out


@mcp.tool(name="pmus_scan_extreme_markets", annotations=RO)
async def pmus_scan_extreme_markets(
    high_threshold: float = Field(default=0.90, ge=0.5, le=0.999, description="Mid >= this counts as a heavy favorite"),
    low_threshold: float = Field(default=0.10, ge=0.001, le=0.5, description="Mid <= this counts as a long shot"),
    category: str | None = Field(default=None, description="Restrict to a category, e.g. politics, sports, macro, crypto"),
    tag_slug: str | None = Field(default=None),
    max_days_to_end: float | None = Field(default=None, gt=0, description="Only markets ending within this many days"),
    min_days_to_end: float | None = Field(default=None, ge=0),
    pages: int = Field(default=3, ge=1, le=10, description="Event pages of 100 to scan (sorted by volume)"),
    limit: int = Field(default=25, ge=1, le=100, description="Max results"),
    sort_by: Literal["annualized_roi", "roi", "spread_ticks", "days_to_end"] = Field(default="annualized_roi"),
) -> dict[str, Any]:
    """Scan active markets for extreme implied probabilities (heavy favorites >= high_threshold
    and long shots <= low_threshold). For each, computes the fee-adjusted 'yield to settlement'
    of betting WITH the market (buy YES on favorites, short NO on long shots), annualized to the
    end date, plus spread width and whether resting inside the spread beats crossing. Uses event
    snapshot quotes; confirm with pmus_analyze_spread before acting."""
    events: list[dict[str, Any]] = []
    try:
        for p in range(pages):
            d = await client().get("/v1/events", limit=100, offset=p * 100, active=True, closed=False,
                                   categories=category, tagSlug=tag_slug, orderBy="volume", orderDirection="desc")
            batch = d.get("events") or []
            events.extend(batch)
            if len(batch) < 100:
                break
    except PolymarketUSError as e:
        return _err(e)

    rows: list[dict[str, Any]] = []
    scanned = 0
    for e in events:
        for m in e.get("markets") or []:
            if m.get("closed") or not m.get("active"):
                continue
            bid, ask = an.num(m.get("bestBidQuote")), an.num(m.get("bestAskQuote"))
            if bid is None or ask is None or ask <= 0 or bid <= 0:
                continue
            scanned += 1
            mid = (bid + ask) / 2
            if not (mid >= high_threshold or mid <= low_threshold):
                continue
            days = an.days_until(m.get("endDate"))
            if max_days_to_end is not None and (days is None or days > max_days_to_end):
                continue
            if min_days_to_end is not None and (days is None or days < min_days_to_end):
                continue
            tick = an.num(m.get("orderPriceMinTickSize")) or 0.01
            theta = an.num(m.get("feeCoefficient")) or an.DEFAULT_TAKER_THETA
            side = "favorite" if mid >= high_threshold else "longshot"
            if side == "favorite":
                taker = an.long_yes(ask, mid, days, theta)
                maker = an.long_yes(round(bid + tick, 6), mid, days, an.DEFAULT_MAKER_THETA)
                trade = "buy YES"
                improve_px = round(bid + tick, 6)
            else:
                taker = an.short_no(bid, mid, days, theta)
                maker = an.short_no(round(ask - tick, 6), mid, days, an.DEFAULT_MAKER_THETA)
                trade = "short (synthetic NO)"
                improve_px = round(ask - tick, 6)
            spread_ticks = round((ask - bid) / tick, 6)
            rows.append({
                "event": e.get("title"), "event_slug": e.get("slug"), "category": e.get("category"),
                "market_slug": m.get("slug"), "outcome": m.get("title") or m.get("titleShort"),
                "side": side, "trade_with_market": trade,
                "bid": bid, "ask": ask, "mid": an.r4(mid), "implied_prob_pct": round(mid * 100, 1),
                "spread": an.r4(ask - bid), "spread_ticks": round(spread_ticks, 1),
                "days_to_end": days,
                "taker_breakeven_prob": round(taker.breakeven_prob, 4),
                "taker_roi": round(taker.roi_if_win, 4),
                "taker_roi_annualized": an.r4(taker.roi_annualized),
                "maker_inside_spread_px": improve_px if spread_ticks > 1 else None,
                "maker_roi": round(maker.roi_if_win, 4) if spread_ticks > 1 else None,
                "maker_roi_annualized": an.r4(maker.roi_annualized) if spread_ticks > 1 else None,
                "roi_pickup_resting_vs_crossing": round(maker.roi_if_win - taker.roi_if_win, 4) if spread_ticks > 1 else None,
                "contrarian_note": (
                    f"Fading it (short at {bid}) needs P(YES) < {round(bid - an.fee_per_contract(bid, theta), 4)}; pays {an.r4(1 - bid)} risk for {an.r4(bid)} reward."
                    if side == "favorite" else
                    f"Buying it (long at {ask}) needs P(YES) > {round(ask + an.fee_per_contract(ask, theta), 4)}; risks {an.r4(ask)} for {an.r4(1 - ask)} reward."
                ),
            })
    key = {
        "annualized_roi": lambda r: (r["taker_roi_annualized"] is None, -(r["taker_roi_annualized"] or 0)),
        "roi": lambda r: -r["taker_roi"],
        "spread_ticks": lambda r: -r["spread_ticks"],
        "days_to_end": lambda r: (r["days_to_end"] is None, r["days_to_end"] or 0),
    }[sort_by]
    rows.sort(key=key)
    return {
        "events_scanned": len(events), "markets_scanned": scanned, "matches": len(rows),
        "thresholds": {"high": high_threshold, "low": low_threshold},
        "how_to_read": [
            "taker_roi is the return on capital if the favored side settles, after crossing the spread and paying taker fees.",
            "maker_* assumes you rest one tick inside the spread and get filled; you earn the rebate but fills are not guaranteed.",
            "roi_pickup_resting_vs_crossing is the extra return from resting inside the spread instead of hitting the touch.",
            "Annualized figures reward short-dated markets; they ignore the real risk that a 95% market settles NO (~1 in 20).",
            "Quotes are from the event snapshot; run pmus_analyze_spread on a slug for live book depth and execution sim.",
        ],
        "results": rows[:limit],
    }


@mcp.tool(name="pmus_event_basket", annotations=RO)
async def pmus_event_basket(
    event_slug: str = Field(description="Event slug with multiple outcome markets, e.g. 'usse-midterms-2026-11-03'"),
    refresh_quotes: bool = Field(default=True, description="Fetch live BBO per market (slower, more accurate)"),
) -> dict[str, Any]:
    """Price the whole outcome set of an event: sum of asks and bids after fees, the
    overround, and the locked P&L from buying every YES or shorting every market IF
    the outcomes are mutually exclusive. Surfaces mispricing across a spread family."""
    try:
        d = await client().get(f"/v1/events/slug/{event_slug}")
        e = d.get("event", d)
        markets = [m for m in (e.get("markets") or []) if m.get("active") and not m.get("closed")]
        theta = an.DEFAULT_TAKER_THETA
        if markets:
            theta = an.num(markets[0].get("feeCoefficient")) or theta
        if refresh_quotes and markets:
            bbos = await asyncio.gather(*[_fetch_bbo(m["slug"]) for m in markets[:60]], return_exceptions=True)
            for m, b in zip(markets, bbos):
                if isinstance(b, dict):
                    m["bestBidQuote"], m["bestAskQuote"] = b.get("bestBid"), b.get("bestAsk")
    except PolymarketUSError as e:
        return _err(e)
    out = an.event_basket(markets, theta)
    out.update({"event": e.get("title"), "event_slug": e.get("slug"), "end_date": e.get("endDate"),
                "quotes": "live_bbo" if refresh_quotes else "event_snapshot", "taker_theta": theta})
    return out


@mcp.tool(name="pmus_line_ladder_check", annotations=RO)
async def pmus_line_ladder_check(
    event_slug: str = Field(description="Game/event slug with spread or total lines, e.g. 'mlb-nyy-min-2026-09-16'"),
    refresh_quotes: bool = Field(default=True, description="Fetch live BBO for each line market (recommended; event snapshot quotes lag)"),
    include_soft: bool = Field(default=False, description="Also list lines whose mids are inverted but quotes don't cross"),
) -> dict[str, Any]:
    """Check an event's spread and total ladders for impossible pricing. A harder line can never be
    more likely than an easier one (win by 3+ <= win by 2+, over 10.5 <= over 9.5, underdog +1.5 <=
    +2.5). When the harder line's bid is above the easier line's ask, buying the easier line and
    shorting the harder one locks in profit after taker fees. Reports locked P&L per pair, the size
    available at the touch on both legs, and label conflicts (question vs rules text) on spread markets."""
    try:
        d = await client().get(f"/v1/events/slug/{event_slug}")
    except PolymarketUSError as e:
        return _err(e)
    e = d.get("event", d)
    lines = [m for m in (e.get("markets") or [])
             if m.get("active") and not m.get("closed") and an.classify_line_market(m.get("slug") or "")]
    theta = an.num(lines[0].get("feeCoefficient")) if lines else None
    theta = theta or an.DEFAULT_TAKER_THETA
    sem = asyncio.Semaphore(8)  # stay well under the 20 req/s public limit

    async def guarded(coro):
        async with sem:
            return await coro

    if refresh_quotes and lines:
        bbos = await asyncio.gather(*[guarded(_fetch_bbo(m["slug"])) for m in lines[:120]], return_exceptions=True)
        for m, b in zip(lines, bbos):
            if isinstance(b, dict):
                m["bestBidQuote"], m["bestAskQuote"] = b.get("bestBid"), b.get("bestAsk")
    out = an.ladder_violations(lines, theta)

    # size available at the touch for each hard violation
    slugs = {h["buy_yes"]["slug"] for h in out["hard_violations"]} | {h["short"]["slug"] for h in out["hard_violations"]}
    books: dict[str, Any] = {}
    if slugs:
        res = await asyncio.gather(*[guarded(_fetch_book(s)) for s in slugs], return_exceptions=True)
        books = {s: r for s, r in zip(slugs, res) if isinstance(r, dict)}
    for h in out["hard_violations"]:
        ask_lvls = (books.get(h["buy_yes"]["slug"]) or {}).get("offers") or []
        bid_lvls = (books.get(h["short"]["slug"]) or {}).get("bids") or []
        ask_qty = an.num(ask_lvls[0].get("qty")) if ask_lvls else None
        bid_qty = an.num(bid_lvls[0].get("qty")) if bid_lvls else None
        pairs = min(ask_qty, bid_qty) if ask_qty is not None and bid_qty is not None else None
        h["touch_qty"] = {"buy_leg_ask_qty": ask_qty, "short_leg_bid_qty": bid_qty, "max_pairs_at_touch": pairs}
        h["max_locked_pnl_at_touch"] = an.r4(pairs * h["locked_pnl_per_pair"]) if pairs is not None else None

    label_conflicts = []
    for m in lines:
        w = an.spread_label_check(m.get("question"), m.get("title"), m.get("description"))
        if w:
            label_conflicts.append({"slug": m.get("slug"), **w})

    if not include_soft:
        out["soft_violation_count"] = len(out.pop("soft_violations"))
    out.update({
        "event": e.get("title"), "event_slug": e.get("slug"), "taker_theta": theta,
        "quotes": "live_bbo" if refresh_quotes else "event_snapshot",
        "line_markets": len(lines),
        "label_conflicts": label_conflicts,
        "how_to_read": [
            "hard_violations: buy_yes the easier line at its ask, short the harder line at its bid. Worst case you net locked_pnl_per_pair; if only the easier line hits you get +$1 more.",
            "max_locked_pnl_at_touch uses only the size at the best price on each leg; walking deeper usually erases the edge.",
            "Quotes move between calls. In-play games reprice every pitch; re-check with pmus_analyze_spread right before acting.",
            "Ladder logic reads the slug (neg-N = favorite -N, pos-N = underdog +N, tsc = over). If label_conflicts lists a market, confirm its settlement side first.",
        ],
    })
    return out


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
