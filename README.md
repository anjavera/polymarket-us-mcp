# polymarket-us-mcp

Read-only [MCP](https://modelcontextprotocol.io) server for **Polymarket US** (the CFTC-regulated
exchange at polymarket.us — *not* the global Polygon-based Polymarket). It wraps the public
gateway (`https://gateway.polymarket.us`) and adds spread / fee / payoff analytics.

**No API key is loaded anywhere.** Every tool is a GET against public endpoints and is
annotated `readOnlyHint=true`. The server cannot place, modify, or cancel orders.

## Install

```powershell
cd C:\Users\15309\PolyMarket\polymarket-us-mcp
uv venv .venv
uv pip install -p .venv\Scripts\python.exe -e ".[dev]"
```

## Configure Claude Desktop / Claude Code

`claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "polymarket-us": {
      "command": "C:\\Users\\15309\\PolyMarket\\polymarket-us-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "polymarket_us_mcp.server"],
      "cwd": "C:\\Users\\15309\\PolyMarket\\polymarket-us-mcp"
    }
  }
}
```

Claude Code: `claude mcp add polymarket-us -- C:\Users\15309\PolyMarket\polymarket-us-mcp\.venv\Scripts\python.exe -m polymarket_us_mcp.server`

Optional env vars: `POLYMARKET_US_TAKER_FEE` (default 0.06; the market's own `feeCoefficient`
is used when present), `POLYMARKET_US_MAKER_FEE` (default -0.0125), `POLYMARKET_US_TIMEOUT` (s).

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

First found live on NYY @ MIN 2026-09-16: −2.5 bid 0.30 vs −1.5 ask 0.21 ≈ 6.7¢ locked per pair,
but only ~3 contracts at the touch.

Settlement check on that game (Twins won 5–4 in 13): `pos-1pt5` settled **YES**, so on `pos` spread
markets the question / long side (+N) is what YES means — the title and rules text are mirrored.

## Verify

```powershell
.venv\Scripts\python.exe -m pytest -q          # unit tests for the math (offline)
.venv\Scripts\python.exe tests\live_e2e.py     # spawns the server over stdio, calls every tool live
```

## Caveats

- Annualized ROI assumes settlement at the market `endDate`, which is the exchange deadline; many
  markets resolve earlier (higher real annualized) or the deadline is far past the event.
- A 95% market still loses about 1 in 20. The scanner shows *yield*, not edge.
- Scanner quotes come from the event snapshot; confirm with `pmus_analyze_spread` (live book) before acting.
- `eventSlug` filtering on `/v1/markets` is unreliable on the gateway; use `pmus_get_event` instead.
- Public gateway rate limit: 20 req/s per IP. `pmus_event_basket(refresh_quotes=True)` makes one BBO call per market.
