# polymarket-us-mcp

Read-only [MCP](https://modelcontextprotocol.io) server for **Polymarket US** (the CFTC-regulated
exchange at polymarket.us — *not* the global Polygon-based Polymarket). It wraps the public
gateway (`https://gateway.polymarket.us`) and adds spread / fee / payoff analytics.

**No API key is loaded anywhere.** Every tool is a GET against public endpoints and is
annotated `readOnlyHint=true`. The server cannot place, modify, or cancel orders.

> **Disclaimer.** This is an independent, unofficial project. It is not affiliated with, endorsed
> by, or supported by Polymarket or QCEX. Nothing it outputs is financial, investment, or trading
> advice. "Locked P&L", ROI, and mispricing figures are arithmetic on quoted prices at one moment;
> quotes move, fills are not guaranteed, fee schedules change, and markets can settle in ways the
> labels don't suggest. Check the market rules and your jurisdiction's eligibility before trading.
> Use at your own risk.

## Requirements

- Python 3.10+
- Network access to `https://gateway.polymarket.us`

## Quick start (no clone)

With [uv](https://docs.astral.sh/uv/) installed, run straight from GitHub:

```bash
uvx --from git+https://github.com/anjavera/polymarket-us-mcp polymarket-us-mcp
```

Add it to Claude Code:

```bash
claude mcp add polymarket-us -- uvx --from git+https://github.com/anjavera/polymarket-us-mcp polymarket-us-mcp
```

Or to Claude Desktop's `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "polymarket-us": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/anjavera/polymarket-us-mcp", "polymarket-us-mcp"]
    }
  }
}
```

## Install from source

```bash
git clone https://github.com/anjavera/polymarket-us-mcp
cd polymarket-us-mcp
python -m venv .venv
# macOS/Linux: source .venv/bin/activate    Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

This puts a `polymarket-us-mcp` command on the venv's path. Point your MCP client at it using the
absolute path, e.g. `/path/to/polymarket-us-mcp/.venv/bin/polymarket-us-mcp`
(Windows: `C:\path\to\polymarket-us-mcp\.venv\Scripts\polymarket-us-mcp.exe`).

## Configuration

Optional env vars: `POLYMARKET_US_TAKER_FEE` (default 0.06; the market's own `feeCoefficient`
is used when present), `POLYMARKET_US_MAKER_FEE` (default -0.0125), `POLYMARKET_US_TIMEOUT` (seconds,
default 20), `POLYMARKET_US_GATEWAY` (default `https://gateway.polymarket.us`),
`POLYMARKET_US_LOG_LEVEL` (default `WARNING`; set `INFO` to log every gateway request to stderr).

## Tools

| Tool | What it does |
|---|---|
| `pmus_search` | Text search over events; returns markets with bid/ask/implied prob |
| `pmus_list_events` | Filtered, paginated events (category, tag, series, dates, sort) |
| `pmus_get_event` | One event with all markets, rules excerpt, market groups |
| `pmus_list_markets` | Flat market list |
| `pmus_get_market` | One market: rules, tick size, fee coefficient, sides, quotes |
| `pmus_get_bbo` | Best bid/offer, last trade, depth counts, open interest, state |
| `pmus_get_order_book` | Bids/offers with cumulative size |
| `pmus_get_settlement` | Settlement value for closed markets (1 = YES, 0 = NO, 0.5 = void) |
| `pmus_get_price_history` | Long/short price history (gateway often returns empty) |
| `pmus_list_series`, `pmus_list_sports`, `pmus_list_tags` | Reference data |
| `pmus_analyze_spread` | **Spread insight for one market** (see below) |
| `pmus_scan_extreme_markets` | **Find heavy favorites / long shots and price the bet within their spread** |
| `pmus_event_basket` | Sum of asks/bids across an event's outcomes; locked P&L if mutually exclusive |
| `pmus_line_ladder_check` | **Impossible pricing across spread/total lines** (e.g. -2.5 priced above -1.5) with locked P&L and touch size |

### Price model used by the analytics

- Price ∈ [0, 1] USD = implied P(YES). One instrument per market: **long = YES**, **short = synthetic NO**
  (receive the price now, pay $1 if YES; collateral = 1 − price).
- Fee per contract = `θ · p · (1 − p)`. Taker θ = market `feeCoefficient` (0.06); maker θ = −0.0125 (rebate).
  Peak fee is at p = 0.50; near 0.05 / 0.95 it is tiny — which is why extreme markets are where spread
  matters more than fees.

### `pmus_analyze_spread`

For a slug it returns bid/ask/mid, spread in ticks and % of mid, a liquidity class, and:

- **taker** — buy YES at the ask / short at the bid: cost basis incl. fee, profit and loss per contract,
  **breakeven probability**, ROI if it wins, ROI annualized to the end date, EV at mid.
- **maker_join_best** — same economics if you rest at the touch and earn the rebate.
- **inside_spread_ladder** — rungs 1..n ticks inside the spread: what a resting buy / resting short at
  that price costs or receives after rebate, and its EV at mid. This is the "other bets inside the
  spread": when a market is 0.90 / 0.96, crossing at 0.96 gives ~3.6% to settlement, but resting at
  0.91–0.93 (if filled) gives 7–9% and collects the rebate.
- **liquidity** — depth within a band of the touch on each side and an imbalance note.
- **execution_sim** (with `size`) — walk the book, average fill price, slippage, fully-filled flag.

### `pmus_scan_extreme_markets`

Pulls active events by volume, flattens their markets, and keeps those with mid ≥ `high_threshold`
(default 0.90) or ≤ `low_threshold` (default 0.10). For each: the with-the-market trade (buy YES on
favorites, short on long shots), taker breakeven probability, ROI and annualized ROI, spread width,
the resting price one tick inside the spread and the ROI pickup vs crossing, plus a contrarian note
giving the probability you'd need to believe to fade it. Filter by category / tag / days to end.

### `pmus_event_basket`

For multi-outcome events (e.g. Senate control: Dem / Rep) sums asks (incl. fees) and bids (after fees)
across the markets. If outcomes are mutually exclusive and exhaustive, `1 − Σask` and `Σbid − 1` are
locked P&L. Almost always negative (that's the overround), but a positive number is a genuine
cross-market mispricing. Exclusivity is *not* checked automatically — read the rules.

### `pmus_line_ladder_check`

Groups an event's spread and total markets into ladders by slug (`asc-…-neg-N` favorite −N,
`asc-…-pos-N` underdog +N, `tsc-…-N` game over N, `tsc-…-tt-TEAM-N` team over N; `f5-` first-5
lines are their own ladder) and checks they are monotonic. A harder line can't be more likely than an
easier one. When the harder line's **bid** is above the easier line's **ask**, buying the easier line and
shorting the harder one locks in `bid − ask − fees` per pair (plus $1 if only the easier line hits).
Reports size at the touch on both legs and the max locked P&L at that size. Also lists
`label_conflicts`: spread markets whose question ("Yankees cover +1.5") and title/rules text ("Twins
win by more than 1.5") describe opposite outcomes. `get_market`, `get_event`, `search` and
`analyze_spread` now attach the same `label_warning` and the market's `long_side`.

*Historical example (September 2026, MLB NYY @ MIN):* the −2.5 line was bid 0.30 while the −1.5 line
was offered at 0.21, about 6.7¢ locked per pair after fees, but only ~3 contracts were available at
the touch. Violations like this are rare and short-lived.

On the label question, that game's `pos-1pt5` market settled **YES** when the Twins won by one, so
for `pos` spread markets observed so far the question / long side (+N) is what YES means and the
title and rules text were mirrored. The exchange could change this; verify on new market types.

## Development

```bash
pytest -q                  # offline unit tests for the math
python tests/live_e2e.py   # spawns the server over stdio and calls every tool against the live gateway
```

The live check hits the real gateway and depends on what markets are open, so it isn't run in CI.

## Caveats

- Annualized ROI assumes settlement at the market `endDate`, which is the exchange deadline; many
  markets resolve earlier (higher real annualized) or the deadline is far past the event.
- A 95% market still loses about 1 in 20. The scanner shows *yield*, not edge.
- Scanner quotes come from the event snapshot; confirm with `pmus_analyze_spread` (live book) before acting.
- `eventSlug` filtering on `/v1/markets` is unreliable on the gateway; use `pmus_get_event` instead.
- Public gateway rate limit: 20 req/s per IP. `pmus_event_basket(refresh_quotes=True)` makes one BBO call per market.
- The gateway is undocumented in places and may change without notice; field names and endpoints here
  reflect its behavior as of September 2026.

## License

[MIT](LICENSE)
