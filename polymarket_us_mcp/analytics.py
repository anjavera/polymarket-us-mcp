"""Pure spread / fee / payoff math for Polymarket US binary contracts.

Everything here is deterministic and network-free so it can be unit tested.

Price model (Polymarket US):
- One instrument per market, price in USD between 0 and 1 = implied P(YES).
- Long (buy) = YES.  Short (sell) = synthetic NO: you receive the price now and
  pay $1 if YES settles.  Collateral for a short is (1 - price).
- Fee per contract = theta * p * (1 - p).  Taker theta = market feeCoefficient
  (0.06 at time of writing), maker theta = -0.0125 (a rebate).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_TAKER_THETA = float(os.environ.get("POLYMARKET_US_TAKER_FEE", "0.06"))
DEFAULT_MAKER_THETA = float(os.environ.get("POLYMARKET_US_MAKER_FEE", "-0.0125"))


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def num(x: Any) -> float | None:
    """Coerce API values ('0.58', {'value': '0.58'}, 0.58, None) to float."""
    if x is None or x == "":
        return None
    if isinstance(x, dict):
        return num(x.get("value"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def r4(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_until(s: str | None, now: datetime | None = None) -> float | None:
    dt = parse_iso(s)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round((dt - now).total_seconds() / 86400, 2)


# --------------------------------------------------------------------------- #
# Fees and payoffs
# --------------------------------------------------------------------------- #
def fee_per_contract(price: float, theta: float) -> float:
    """theta * p * (1-p); negative theta => rebate (negative fee)."""
    return theta * price * (1.0 - price)


def annualize(roi: float, days: float | None) -> float | None:
    if days is None or days <= 0 or roi <= -1:
        return None
    try:
        return (1.0 + roi) ** (365.0 / days) - 1.0
    except (OverflowError, ZeroDivisionError):
        return None


@dataclass
class SidePayoff:
    side: str                 # "long_yes" or "short_no"
    role: str                 # "taker" or "maker"
    price: float              # execution price of the instrument
    fee: float                # fee per contract (negative = rebate)
    cost_basis: float         # capital at risk per contract (incl. fee)
    profit_if_win: float      # per contract
    loss_if_lose: float       # per contract (positive number)
    breakeven_prob: float     # P(this side wins) needed to break even
    roi_if_win: float         # profit_if_win / cost_basis
    roi_annualized: float | None
    ev_at_mid: float          # expected profit per contract if P(YES) == mid

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 5)
        return d


def long_yes(price: float, mid: float, days: float | None, theta: float) -> SidePayoff:
    fee = fee_per_contract(price, theta)
    cost = price + fee
    profit = 1.0 - cost
    roi = profit / cost if cost > 0 else 0.0
    return SidePayoff(
        side="long_yes", role="taker" if theta > 0 else "maker", price=price, fee=fee,
        cost_basis=cost, profit_if_win=profit, loss_if_lose=cost, breakeven_prob=cost,
        roi_if_win=roi, roi_annualized=annualize(roi, days), ev_at_mid=mid - cost,
    )


def short_no(price: float, mid: float, days: float | None, theta: float) -> SidePayoff:
    """Sell (short) the instrument at `price`: receive price - fee, pay 1 if YES."""
    fee = fee_per_contract(price, theta)
    received = price - fee
    collateral = 1.0 - price           # margin locked = max loss before fee
    loss_if_yes = 1.0 - received       # pay 1, keep what you received
    roi = received / collateral if collateral > 0 else 0.0
    p_no = 1.0 - mid
    return SidePayoff(
        side="short_no", role="taker" if theta > 0 else "maker", price=price, fee=fee,
        cost_basis=collateral, profit_if_win=received, loss_if_lose=loss_if_yes,
        breakeven_prob=1.0 - received,   # need P(NO) >= 1 - received  <=> P(YES) <= received
        roi_if_win=roi, roi_annualized=annualize(roi, days),
        ev_at_mid=p_no * received - mid * loss_if_yes,
    )


# --------------------------------------------------------------------------- #
# Spread analysis
# --------------------------------------------------------------------------- #
def classify_spread(spread_ticks: float | None) -> str:
    if spread_ticks is None:
        return "unknown"
    if spread_ticks <= 1:
        return "tight (1 tick — no room to improve; maker capture limited to rebate)"
    if spread_ticks <= 5:
        return "normal"
    if spread_ticks <= 20:
        return "wide (room for inside-spread limit orders)"
    return "very wide / illiquid (quote carefully; price may not reflect consensus)"


def inside_spread_ladder(
    bid: float, ask: float, tick: float, mid: float, days: float | None,
    maker_theta: float, max_rungs: int = 6,
) -> list[dict[str, Any]]:
    """Resting-order candidates strictly inside the spread.

    Each rung shows what a maker gets by improving the best bid (buy YES) or the
    best ask (sell / short) by n ticks. EV is computed at the mid as the
    reference probability — this is *not* a forecast, only "what is priced".
    """
    rungs: list[dict[str, Any]] = []
    n_ticks = int(round((ask - bid) / tick))
    if n_ticks <= 1:
        return rungs
    steps = list(range(1, n_ticks))
    if len(steps) > max_rungs:
        # sample evenly, always keep first and last
        idx = {0, len(steps) - 1}
        stride = max(1, len(steps) // (max_rungs - 1))
        idx.update(range(0, len(steps), stride))
        steps = [steps[i] for i in sorted(idx)][:max_rungs]
    for n in steps:
        buy_px = round(bid + n * tick, 6)
        sell_px = round(ask - n * tick, 6)
        ly = long_yes(buy_px, mid, days, maker_theta)
        sn = short_no(sell_px, mid, days, maker_theta)
        rungs.append({
            "ticks_inside": n,
            "rest_buy_yes_at": buy_px,
            "buy_cost_after_rebate": round(ly.cost_basis, 5),
            "buy_ev_at_mid": round(ly.ev_at_mid, 5),
            "rest_sell_no_at": sell_px,
            "sell_received_after_rebate": round(sn.profit_if_win, 5),
            "sell_ev_at_mid": round(sn.ev_at_mid, 5),
            "you_become_best_quote": n >= 1,
        })
    return rungs


def walk_book(levels: list[dict[str, Any]], size: float) -> dict[str, Any]:
    """Simulate a market order of `size` contracts against one side of the book."""
    filled = 0.0
    notional = 0.0
    used = 0
    for lvl in levels:
        px = num(lvl.get("px"))
        qty = num(lvl.get("qty"))
        if px is None or qty is None or qty <= 0:
            continue
        take = min(qty, size - filled)
        filled += take
        notional += take * px
        used += 1
        if filled >= size - 1e-9:
            break
    avg = notional / filled if filled else None
    best = num(levels[0].get("px")) if levels else None
    return {
        "requested": size,
        "filled": round(filled, 4),
        "fully_filled": filled >= size - 1e-9,
        "avg_price": r4(avg),
        "best_price": best,
        "slippage_vs_best": r4(abs(avg - best)) if avg is not None and best is not None else None,
        "levels_consumed": used,
    }


def depth_within(levels: list[dict[str, Any]], best: float | None, band: float) -> float:
    if best is None:
        return 0.0
    total = 0.0
    for lvl in levels:
        px, qty = num(lvl.get("px")), num(lvl.get("qty"))
        if px is None or qty is None:
            continue
        if abs(px - best) <= band + 1e-12:
            total += qty
    return round(total, 2)


def analyze_spread(
    *,
    slug: str,
    bid: float | None,
    ask: float | None,
    tick: float | None,
    end_date: str | None,
    taker_theta: float,
    maker_theta: float = DEFAULT_MAKER_THETA,
    bids: list[dict[str, Any]] | None = None,
    offers: list[dict[str, Any]] | None = None,
    size: float | None = None,
    last_trade: float | None = None,
    open_interest: float | None = None,
) -> dict[str, Any]:
    tick = tick or 0.01
    days = days_until(end_date)
    out: dict[str, Any] = {
        "market_slug": slug,
        "best_bid": bid,
        "best_ask": ask,
        "tick_size": tick,
        "last_trade": last_trade,
        "open_interest": open_interest,
        "days_to_end_date": days,
        "fee_model": {
            "taker_theta": taker_theta,
            "maker_theta": maker_theta,
            "formula": "fee_per_contract = theta * p * (1-p); negative = rebate",
        },
    }
    if bid is None or ask is None:
        out["note"] = "One or both sides of the book are empty; spread metrics unavailable."
        return out

    mid = (bid + ask) / 2.0
    spread = ask - bid
    spread_ticks = round(spread / tick, 6) if tick else None
    out.update({
        "mid": r4(mid),
        "implied_prob_yes_pct": round(mid * 100, 2),
        "implied_prob_no_pct": round((1 - mid) * 100, 2),
        "spread": r4(spread),
        "spread_ticks": round(spread_ticks, 1) if spread_ticks is not None else None,
        "spread_pct_of_mid": round(spread / mid * 100, 3) if mid else None,
        "spread_class": classify_spread(spread_ticks),
        "cost_to_cross_round_trip": r4(spread + fee_per_contract(ask, taker_theta) + fee_per_contract(bid, taker_theta)),
    })

    out["taker"] = {
        "buy_yes_at_ask": long_yes(ask, mid, days, taker_theta).to_dict(),
        "short_no_at_bid": short_no(bid, mid, days, taker_theta).to_dict(),
    }
    out["maker_join_best"] = {
        "rest_buy_yes_at_bid": long_yes(bid, mid, days, maker_theta).to_dict(),
        "rest_sell_no_at_ask": short_no(ask, mid, days, maker_theta).to_dict(),
    }
    out["inside_spread_ladder"] = inside_spread_ladder(bid, ask, tick, mid, days, maker_theta)

    if bids is not None and offers is not None:
        band = max(tick * 5, 0.01)
        bid_depth = depth_within(bids, bid, band)
        ask_depth = depth_within(offers, ask, band)
        tot = bid_depth + ask_depth
        out["liquidity"] = {
            "band": band,
            "bid_depth_in_band": bid_depth,
            "ask_depth_in_band": ask_depth,
            "bid_share_pct": round(bid_depth / tot * 100, 1) if tot else None,
            "bid_levels": len(bids),
            "ask_levels": len(offers),
            "imbalance_note": (
                "more resting size on the bid — buyers outnumber sellers near the touch"
                if tot and bid_depth > ask_depth * 1.5 else
                "more resting size on the ask — sellers outnumber buyers near the touch"
                if tot and ask_depth > bid_depth * 1.5 else "roughly balanced"
            ),
        }
        if size:
            out["execution_sim"] = {
                "buy_yes": walk_book(offers, size),
                "short_no": walk_book(bids, size),
            }

    out["reading_guide"] = [
        "breakeven_prob is the probability the side must have to break even after fees; compare it to your own estimate, not to mid.",
        "ev_at_mid uses the mid as the reference probability, so it mostly reflects fee/spread drag (taker) or capture (maker), not edge.",
        "inside_spread_ladder rungs are resting orders that improve the touch; they earn the maker rebate but only fill if someone crosses.",
        "roi_annualized assumes settlement at end_date, which is the exchange deadline and may be later than actual resolution.",
    ]
    return out


# --------------------------------------------------------------------------- #
# Event-level (multi-outcome) checks
# --------------------------------------------------------------------------- #
def event_basket(markets: list[dict[str, Any]], taker_theta: float) -> dict[str, Any]:
    """Sum of asks / bids across an event's markets.

    If the markets are mutually exclusive and exhaustive (exactly one settles
    YES), then buying every YES at the ask locks in 1 - sum(cost), and
    shorting every one at the bid locks in sum(received) - 1.
    """
    rows = []
    sum_buy_cost = 0.0
    sum_short_recv = 0.0
    complete = True
    for m in markets:
        bid, ask = num(m.get("bestBidQuote")), num(m.get("bestAskQuote"))
        if bid is None or ask is None:
            complete = False
        buy_cost = (ask + fee_per_contract(ask, taker_theta)) if ask is not None else None
        short_recv = (bid - fee_per_contract(bid, taker_theta)) if bid is not None else None
        if buy_cost is not None:
            sum_buy_cost += buy_cost
        if short_recv is not None:
            sum_short_recv += short_recv
        rows.append({
            "slug": m.get("slug"),
            "outcome": m.get("title") or m.get("titleShort") or m.get("question"),
            "bid": bid, "ask": ask,
            "buy_cost_incl_fee": r4(buy_cost),
            "short_received_after_fee": r4(short_recv),
        })
    n = len(rows)
    return {
        "n_markets": n,
        "all_quotes_present": complete,
        "sum_of_asks_incl_fees": r4(sum_buy_cost),
        "sum_of_bids_after_fees": r4(sum_short_recv),
        "buy_all_yes_locked_pnl_if_exclusive": r4(1.0 - sum_buy_cost) if complete and n > 1 else None,
        "short_all_locked_pnl_if_exclusive": r4(sum_short_recv - 1.0) if complete and n > 1 else None,
        "overround_pct": r4((sum_buy_cost - 1.0) * 100) if complete and n > 1 else None,
        "markets": rows,
        "caveat": (
            "Locked-PnL figures assume exactly one market in this event settles YES. "
            "Verify exclusivity/exhaustiveness in the rules before relying on them; many events "
            "(ranges, 'over X' ladders, independent props) are NOT mutually exclusive."
        ),
    }


# --------------------------------------------------------------------------- #
# Label consistency (question vs outcome title vs rules text)
# --------------------------------------------------------------------------- #
_Q_COVER_RE = re.compile(
    r"will (?:the )?(?P<team>.+?) cover (?P<sign>[+-]?)(?P<n>\d+(?:\.\d+)?) vs\.? (?:the )?(?P<opp>.+?) in\b",
    re.IGNORECASE,
)
_WINS_BY = r"(?:the )?(?P<team>[A-Z0-9][\w .'&-]*?) wins? by (?:over|more than) (?P<n>\d+(?:\.\d+)?)"
_WINS_BY_RES = (re.compile(r"\bif " + _WINS_BY, re.IGNORECASE), re.compile(r"^\s*" + _WINS_BY, re.IGNORECASE))


def _norm_team(s: str | None) -> str:
    s = (s or "").strip().lower()
    return s[4:] if s.startswith("the ") else s


def spread_label_check(question: str | None, title: str | None, description: str | None) -> dict[str, Any] | None:
    """Flag spread markets whose question and title / rules text describe opposite sides.

    Question form: "Will the A cover -N vs the B ..."  -> YES = A wins by more than N.
                   "Will the A cover +N (or N) vs B" -> YES = B does NOT win by more than N.
    Title / rules form: "T wins by over N" -> YES = T wins by more than N.
    The two agree only when the question sign is '-' and T == A. Returns None when the
    labels agree or cannot be parsed; otherwise a dict describing the conflict.
    """
    q = _Q_COVER_RE.search(question or "")
    if not q:
        return None
    team, opp, sign, n = _norm_team(q["team"]), _norm_team(q["opp"]), q["sign"] or "+", q["n"]
    q_yes = f"{q['team']} wins by more than {n}" if sign == "-" else f"{q['team']} covers +{n} ({q['opp']} does not win by more than {n})"
    found: dict[str, str] = {}
    for label, text in (("title", title), ("rules", description)):
        mt = next((r.search(text or "") for r in _WINS_BY_RES if r.search(text or "")), None)
        if not mt:
            continue
        t = _norm_team(mt["team"])
        if t not in (team, opp):
            continue
        agrees = sign == "-" and t == team and float(mt["n"]) == float(n)
        if not agrees:
            found[label] = f"{mt['team'].strip()} wins by more than {mt['n']}"
    if not found:
        return None
    return {
        "conflict": True,
        "question_says_yes_means": q_yes,
        **{f"{k}_says_yes_means": v for k, v in found.items()},
        "note": ("Question and title/rules describe opposite outcomes. Observed settlement on "
                 "mlb-nyy-min-2026-09-16 (Twins won 5-4): asc-...-pos-1pt5 settled YES, so the QUESTION "
                 "and long_side (+N) were right and the title/rules text was wrong. Treat YES as the "
                 "question's meaning, but check new market types before trading."),
    }


# --------------------------------------------------------------------------- #
# Line ladders (spread / total lines must be monotonic in the line)
# --------------------------------------------------------------------------- #
_SPREAD_SLUG_RE = re.compile(r"^asc-(?P<base>.+?)-(?P<period>f\d+-)?(?P<sign>pos|neg)-(?P<n>\d+)pt(?P<d>\d+)$")
_TOTAL_SLUG_RE = re.compile(r"^tsc-(?P<base>.+?)-(?P<period>f\d+-)?(?:tt-(?P<team>[a-z0-9]+)-)?(?P<n>\d+)pt(?P<d>\d+)$")


def classify_line_market(slug: str) -> dict[str, Any] | None:
    """Parse a spread/total slug into its ladder family, line, and monotonic direction.

    direction 'decreasing': P(YES) must not rise as the line rises (over N, favorite -N).
    direction 'increasing': P(YES) must not fall as the line rises (underdog +N).
    """
    m = _SPREAD_SLUG_RE.match(slug or "")
    if m:
        period = (m["period"] or "full").rstrip("-")
        return {
            "family": f"{m['base']}|{period}|spread-{m['sign']}",
            "kind": "spread", "period": period,
            "line": float(f"{m['n']}.{m['d']}"),
            "direction": "decreasing" if m["sign"] == "neg" else "increasing",
        }
    m = _TOTAL_SLUG_RE.match(slug or "")
    if m:
        period = (m["period"] or "full").rstrip("-")
        subject = f"team-total-{m['team']}" if m["team"] else "game-total"
        return {
            "family": f"{m['base']}|{period}|{subject}",
            "kind": subject, "period": period,
            "line": float(f"{m['n']}.{m['d']}"),
            "direction": "decreasing",
        }
    return None


def ladder_violations(markets: list[dict[str, Any]], taker_theta: float) -> dict[str, Any]:
    """Check every spread/total ladder in an event for non-monotonic pricing.

    For two lines where YES(narrow) implies YES(wide) (e.g. win by 3+ implies win by 2+),
    P(narrow) <= P(wide). If bid(narrow) > ask(wide), buying wide at the ask and shorting
    narrow at the bid locks in bid_narrow - ask_wide - fees, plus $1 more if only the wide
    line hits. 'soft' = mids inverted but the quotes don't cross (no locked profit).
    """
    families: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        info = classify_line_market(m.get("slug") or "")
        if not info:
            continue
        bid, ask = num(m.get("bestBidQuote")), num(m.get("bestAskQuote"))
        families.setdefault(info["family"], []).append({
            **info, "slug": m.get("slug"), "question": m.get("question"), "bid": bid, "ask": ask,
            "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
        })

    hard: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    fam_out = []
    for fam, rows in families.items():
        rows.sort(key=lambda r: r["line"])
        fam_out.append({"family": fam, "direction": rows[0]["direction"],
                        "lines": [{"line": r["line"], "slug": r["slug"], "bid": r["bid"], "ask": r["ask"]} for r in rows]})
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                lo, hi = rows[i], rows[j]
                # wide = the more likely line, narrow = the less likely one
                wide, narrow = (lo, hi) if lo["direction"] == "decreasing" else (hi, lo)
                if wide["ask"] is not None and narrow["bid"] is not None and narrow["bid"] > wide["ask"]:
                    fee_w = fee_per_contract(wide["ask"], taker_theta)
                    fee_n = fee_per_contract(narrow["bid"], taker_theta)
                    locked = narrow["bid"] - wide["ask"] - fee_w - fee_n
                    capital = (wide["ask"] + fee_w) + (1.0 - narrow["bid"])
                    hard.append({
                        "family": fam,
                        "buy_yes": {"slug": wide["slug"], "line": wide["line"], "ask": wide["ask"]},
                        "short": {"slug": narrow["slug"], "line": narrow["line"], "bid": narrow["bid"]},
                        "locked_pnl_per_pair": r4(locked),
                        "bonus_if_only_wide_hits": 1.0,
                        "capital_per_pair": r4(capital),
                        "locked_roi": r4(locked / capital) if capital > 0 else None,
                        "profitable_after_fees": locked > 0,
                    })
                elif wide["mid"] is not None and narrow["mid"] is not None and narrow["mid"] > wide["mid"]:
                    soft.append({"family": fam, "wide": wide["slug"], "wide_mid": r4(wide["mid"]),
                                 "narrow": narrow["slug"], "narrow_mid": r4(narrow["mid"])})
    hard.sort(key=lambda h: -(h["locked_pnl_per_pair"] or 0))
    return {
        "families_checked": len(families),
        "hard_violations": hard,
        "soft_violations": soft,
        "families": fam_out,
    }
