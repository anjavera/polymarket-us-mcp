"""Unit tests for the network-free analytics layer."""

import math

from polymarket_us_mcp import analytics as an


def test_fee_matches_docs_examples():
    # Docs: 1,000 contracts at 0.50, taker 0.06 => $15.00 ; maker -0.0125 => -$3.125
    assert math.isclose(an.fee_per_contract(0.50, 0.06) * 1000, 15.0)
    assert math.isclose(an.fee_per_contract(0.50, -0.0125) * 1000, -3.125)
    # symmetric around 0.5
    assert math.isclose(an.fee_per_contract(0.10, 0.06), an.fee_per_contract(0.90, 0.06))


def test_long_yes_payoff():
    p = an.long_yes(0.90, mid=0.895, days=365, theta=0.06)
    fee = 0.06 * 0.9 * 0.1
    assert math.isclose(p.cost_basis, 0.90 + fee)
    assert math.isclose(p.profit_if_win, 1 - 0.90 - fee)
    assert math.isclose(p.breakeven_prob, p.cost_basis)
    assert math.isclose(p.roi_annualized, p.roi_if_win)  # 365 days => same
    assert p.role == "taker"


def test_short_no_payoff():
    p = an.short_no(0.10, mid=0.105, days=None, theta=0.06)
    fee = 0.06 * 0.1 * 0.9
    assert math.isclose(p.profit_if_win, 0.10 - fee)
    assert math.isclose(p.cost_basis, 0.90)
    assert math.isclose(p.loss_if_lose, 1 - (0.10 - fee))
    assert p.roi_annualized is None
    # shorting a longshot needs P(YES) below received amount
    assert math.isclose(p.breakeven_prob, 1 - (0.10 - fee))


def test_maker_rebate_lowers_cost():
    taker = an.long_yes(0.60, 0.6, None, 0.06)
    maker = an.long_yes(0.60, 0.6, None, -0.0125)
    assert maker.cost_basis < 0.60 < taker.cost_basis
    assert maker.role == "maker"


def test_inside_spread_ladder():
    rungs = an.inside_spread_ladder(bid=0.90, ask=0.95, tick=0.01, mid=0.925, days=30, maker_theta=-0.0125)
    assert [r["ticks_inside"] for r in rungs] == [1, 2, 3, 4]
    assert math.isclose(rungs[0]["rest_buy_yes_at"], 0.91)
    assert math.isclose(rungs[0]["rest_sell_no_at"], 0.94)
    # one-tick spread => nothing inside
    assert an.inside_spread_ladder(0.90, 0.91, 0.01, 0.905, 30, -0.0125) == []
    # very wide spread is sampled down
    assert len(an.inside_spread_ladder(0.10, 0.90, 0.001, 0.5, 30, -0.0125)) <= 6


def test_walk_book():
    offers = [{"px": {"value": "0.50"}, "qty": "100"}, {"px": {"value": "0.52"}, "qty": "100"}]
    r = an.walk_book(offers, 150)
    assert r["fully_filled"] and r["levels_consumed"] == 2
    assert math.isclose(r["avg_price"], (100 * 0.50 + 50 * 0.52) / 150, abs_tol=1e-4)
    assert math.isclose(r["slippage_vs_best"], r["avg_price"] - 0.50, abs_tol=1e-4)
    r2 = an.walk_book(offers, 500)
    assert not r2["fully_filled"] and r2["filled"] == 200


def test_analyze_spread_full():
    out = an.analyze_spread(
        slug="x", bid=0.58, ask=0.581, tick=0.001, end_date="2099-01-01T00:00:00Z", taker_theta=0.06,
        bids=[{"px": {"value": "0.58"}, "qty": "10"}], offers=[{"px": {"value": "0.581"}, "qty": "5"}], size=3,
    )
    assert out["spread_ticks"] == 1.0 and out["spread_class"].startswith("tight")
    assert out["inside_spread_ladder"] == []
    assert out["liquidity"]["bid_depth_in_band"] == 10
    assert out["execution_sim"]["buy_yes"]["fully_filled"]
    assert out["taker"]["buy_yes_at_ask"]["breakeven_prob"] > 0.581


def test_analyze_spread_empty_side():
    out = an.analyze_spread(slug="x", bid=None, ask=0.5, tick=0.01, end_date=None, taker_theta=0.06)
    assert "note" in out and "spread" not in out


def test_event_basket_exclusive_arb():
    mk = [
        {"slug": "a", "title": "A", "bestBidQuote": {"value": "0.58"}, "bestAskQuote": {"value": "0.581"}},
        {"slug": "b", "title": "B", "bestBidQuote": {"value": "0.44"}, "bestAskQuote": {"value": "0.441"}},
    ]
    out = an.event_basket(mk, 0.06)
    assert out["all_quotes_present"] and out["n_markets"] == 2
    assert out["sum_of_asks_incl_fees"] > 1.0          # overround, no free lunch
    assert out["buy_all_yes_locked_pnl_if_exclusive"] < 0
    assert out["short_all_locked_pnl_if_exclusive"] < 0


def test_num_and_days():
    assert an.num({"value": "0.5", "currency": "USD"}) == 0.5
    assert an.num("abc") is None and an.num(None) is None
    assert an.days_until("2099-01-01T00:00:00Z") > 0
    assert an.days_until(None) is None


from polymarket_us_mcp import analytics as an

def mk(slug, bid, ask, q=""):
    return {"slug": slug, "bestBidQuote": bid, "bestAskQuote": ask, "question": q}

def test_classify():
    c = an.classify_line_market("asc-mlb-nyy-min-2026-09-16-neg-2pt5")
    assert c["line"] == 2.5 and c["direction"] == "decreasing" and c["period"] == "full"
    c = an.classify_line_market("asc-mlb-nyy-min-2026-09-16-f5-pos-1pt5")
    assert c["period"] == "f5" and c["direction"] == "increasing"
    c = an.classify_line_market("tsc-mlb-nyy-min-2026-09-16-tt-nyy-6pt5")
    assert c["kind"] == "team-total-nyy" and c["line"] == 6.5
    c = an.classify_line_market("tsc-mlb-nyy-min-2026-09-16-9pt5")
    assert c["kind"] == "game-total" and c["line"] == 9.5
    assert an.classify_line_market("aec-mlb-nyy-min-2026-09-16") is None

def test_ladder_hard_violation_live_example():
    ms = [mk("asc-mlb-nyy-min-2026-09-16-neg-1pt5", 0.195, 0.21),
          mk("asc-mlb-nyy-min-2026-09-16-neg-2pt5", 0.30, 0.315),
          mk("asc-mlb-nyy-min-2026-09-16-pos-1pt5", 0.95, 0.97),
          mk("asc-mlb-nyy-min-2026-09-16-pos-2pt5", 0.97, 0.98),
          mk("tsc-mlb-nyy-min-2026-09-16-tt-nyy-4pt5", 0.46, 0.80),
          mk("tsc-mlb-nyy-min-2026-09-16-tt-nyy-5pt5", 0.43, 0.45)]
    out = an.ladder_violations(ms, 0.06)
    assert len(out["hard_violations"]) == 1
    h = out["hard_violations"][0]
    assert h["buy_yes"]["line"] == 1.5 and h["short"]["line"] == 2.5
    exp = 0.30 - 0.21 - 0.06*0.21*0.79 - 0.06*0.30*0.70
    assert math.isclose(h["locked_pnl_per_pair"], round(exp, 4))
    assert h["profitable_after_fees"]
    # pos ladder is increasing and consistent; tt-nyy bid 0.43 < ask 0.80 so no hard violation
    assert not any("pos" in s["wide"] for s in out["soft_violations"])

def test_ladder_increasing_violation():
    ms = [mk("asc-x-pos-1pt5", 0.90, 0.92), mk("asc-x-pos-2pt5", 0.80, 0.85)]
    h = an.ladder_violations(ms, 0.06)["hard_violations"]
    assert len(h) == 1 and h[0]["buy_yes"]["line"] == 2.5 and h[0]["short"]["line"] == 1.5

def test_label_check_conflict_and_agree():
    q = "Will the New York Yankees cover 1.5 vs the Minnesota Twins in New York Yankees vs Minnesota Twins?"
    r = an.spread_label_check(q, "Minnesota Twins wins by over 1.5 runs",
        "This market will settle to Yes if the Minnesota Twins win by more than 1.5 runs in the ...")
    assert r and r["conflict"] and "title_says_yes_means" in r and "rules_says_yes_means" in r
    q2 = "Will the New York Yankees cover -2.5 vs the Minnesota Twins in New York Yankees vs Minnesota Twins?"
    assert an.spread_label_check(q2, "New York Yankees wins by over 2.5 runs",
        "This market will settle to Yes if the New York Yankees win by more than 2.5 runs in the") is None
    assert an.spread_label_check("Who will win?", "x", "y") is None
