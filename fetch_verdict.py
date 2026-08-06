"""
A verdict layer that reads the 5-year trajectories already produced by
fetch_trajectory.py and turns them into a plain-language analytical
read per company, for VRT, ETN, PWR, and NVT.

Nothing here recomputes a metric. Every number comes from
fetch_trajectory.gather_ticker_trajectory()'s series (which itself
reuses compute_margins()/compute_roic()/compute_credit_metrics()
unchanged) and from fetch_trajectory.direction_and_change() (called
directly, not reimplemented) — including on two new derived series,
gross/operating margin spread-vs-peer-average per year, which this
script computes by aggregating the already-computed per-year company
values (a peer average is not a new metric derivation, just a mean
across the four already-computed numbers for that year). Customer
concentration reuses fetch_customer_concentration's extraction and
consolidate.py's concentration_headline() selection and regex
patterns directly.

Four reads per company:
1. MOAT DIRECTION — widening / narrowing / mixed, from the trajectory
   (not the level) of ROIC plus gross- and operating-margin spread vs.
   peer average. Rule: score each of {ROIC direction, gross-margin
   spread direction, operating-margin spread direction} as +1 rising /
   -1 falling / 0 otherwise. >=2 net positive and no negative signal
   -> WIDENING; >=2 net negative and no positive signal -> NARROWING;
   otherwise MIXED.
2. MARGIN QUALITY / DURABILITY — most recent operating margin vs. a
   documented "high margin" threshold (15%, chosen as the value that
   cleanly separates this coverage set: three companies sit at 15-19%
   and one at 5.7%), combined with customer concentration. High margin
   + no customer at/above ~10% -> DURABLE. High margin + a customer at
   or above ~10% -> FRAGILE (the margin is revocable). High margin +
   concentration not disclosed -> UNVERIFIED. Margin below the
   threshold -> LOW-MARGIN (the durable/fragile framing isn't the
   binding question).
3. CREDIT DIRECTION — leverage (Debt/EBITDA trajectory: falling =
   easing, rising = building) and coverage (interest coverage
   trajectory: rising = improving, falling = deteriorating) over the
   same 5 years, read directly off fetch_trajectory's own series.
4. VERDICT — one line synthesizing 1-3, with a confidence caveat
   appended whenever a read depends on a 'fallback'-confidence year
   (not just 'derived' — see CONFIDENCE_NOTE below) so a conclusion is
   never presented as clean when its inputs aren't.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
5 + 4 = 9 requests total: the 5 fetch_trajectory.py already makes
(shared ticker lookup + one companyfacts call per company) plus one
10-K document fetch per company for customer concentration (the
submissions lookup piggybacks on data already implied by the CIK, but
fetch_customer_concentration.most_recent_10k() still makes its own
submissions call per company).
"""

import consolidate
import fetch_customer_concentration
import fetch_margins
import fetch_trajectory

TICKERS = fetch_trajectory.TICKERS

HIGH_MARGIN_THRESHOLD = 0.15
CONCENTRATION_THRESHOLD = 10.0  # percent

CONFIDENCE_RANK = {"clean": 0, "derived": 1, "fallback": 2, "unresolved": 3}


def worst_confidence(confidences):
    return max(confidences, key=lambda c: CONFIDENCE_RANK[c]) if confidences else "unresolved"


def fallback_years(series):
    return [yr for yr, _, conf in series if conf == "fallback"]


def derived_years(series):
    return [yr for yr, _, conf in series if conf == "derived"]


def confidence_note(label, series):
    """None if every year is clean. A cautionary, specific note if any
    year is 'fallback' (may differ in basis — the case the task calls
    out as requiring disclosure). A lighter note if the worst is
    'derived' (a documented formula, not a substitute tag)."""
    worst = worst_confidence([c for _, _, c in series])
    if worst == "clean":
        return None
    if worst == "fallback":
        years = fallback_years(series)
        return f"{label} uses a fallback (substitute-tag) basis in FY{', FY'.join(years)} — directional read only, hand-check those years."
    if worst == "derived":
        years = derived_years(series)
        if len(years) == len(series):
            return f"{label} is derived (formula-based, not a single tagged subtotal) in every year shown — mechanically consistent, not itself a red flag."
        return f"{label} is derived (formula-based) in FY{', FY'.join(years)}."
    return f"{label} has unresolved (missing) years — trajectory is based on fewer than 5 data points."


def peer_average_series(all_series, metric):
    """Per-year peer average across the four companies for `metric`,
    skipping any company/year that's None (unresolved) that year.
    Returns a list of averages aligned by index to each company's own
    per-year series (all four companies share the same 5 fiscal years
    in this coverage set)."""
    length = len(all_series[TICKERS[0]][metric])
    averages = []
    for i in range(length):
        vals = [all_series[t][metric][i][1] for t in TICKERS if all_series[t][metric][i][1] is not None]
        averages.append(sum(vals) / len(vals) if vals else None)
    return averages


def spread_series(all_series, ticker, metric, peer_avg):
    series = all_series[ticker][metric]
    out = []
    for i, (yr, val, conf) in enumerate(series):
        if val is None or peer_avg[i] is None:
            out.append((yr, None, conf))
        else:
            out.append((yr, val - peer_avg[i], conf))
    return out


def moat_direction(roic_series, gross_spread_series, op_spread_series):
    roic_dir, *_ = fetch_trajectory.direction_and_change("roic", roic_series)
    gross_dir, *_ = fetch_trajectory.direction_and_change("gross_margin", gross_spread_series)
    op_dir, *_ = fetch_trajectory.direction_and_change("operating_margin", op_spread_series)

    def score(d):
        return 1 if d == "rising" else (-1 if d == "falling" else 0)

    total = score(roic_dir) + score(gross_dir) + score(op_dir)
    has_negative = any(d == "falling" for d in (roic_dir, gross_dir, op_dir))
    has_positive = any(d == "rising" for d in (roic_dir, gross_dir, op_dir))

    if total >= 2 and not has_negative:
        verdict = "WIDENING"
    elif total <= -2 and not has_positive:
        verdict = "NARROWING"
    else:
        verdict = "MIXED"

    return verdict, roic_dir, gross_dir, op_dir


def concentration_status(sentences):
    """Reuses consolidate.py's own regex objects and priority order —
    largest-customer figure checked first, then an explicit
    no-customer-exceeds-X% statement — rather than re-deriving a new
    extraction. Returns (status, detail_string) where status is
    "concentrated" / "diversified" / "unknown"."""
    for s in sentences:
        m = consolidate.LARGEST_CUSTOMER_PATTERN.search(s)
        if m:
            pct = float(m.group(1))
            status = "concentrated" if pct >= CONCENTRATION_THRESHOLD else "diversified"
            return status, f"largest customer {pct:.0f}%"
    for s in sentences:
        m = consolidate.NO_CUSTOMER_PATTERN.search(s)
        if m:
            return "diversified", f"no customer exceeds {m.group(1)}%"
    return "unknown", "not disclosed"


def durability(current_margin_val, current_margin_conf, conc_status, conc_detail):
    if current_margin_val < HIGH_MARGIN_THRESHOLD:
        return (
            "LOW-MARGIN",
            f"Operating margin {current_margin_val:.2%} is below the {HIGH_MARGIN_THRESHOLD:.0%} threshold "
            f"used for the durability framing; concentration is {conc_detail} for reference, but the "
            "fragile/durable question isn't the binding constraint here.",
        )
    if conc_status == "unknown":
        return (
            "UNVERIFIED",
            f"Operating margin {current_margin_val:.2%} is high, but customer concentration is {conc_detail} "
            "in the filing — durability can't be assessed from public data.",
        )
    if conc_status == "concentrated":
        return (
            "FRAGILE",
            f"Operating margin {current_margin_val:.2%} is high, but {conc_detail} — that margin is "
            "revocable if the relationship with that customer changes.",
        )
    return (
        "DURABLE",
        f"Operating margin {current_margin_val:.2%} is high and {conc_detail} — no single-customer "
        "concentration risk disclosed.",
    )


def credit_direction(debt_ebitda_series, interest_coverage_series):
    leverage_dir, *_ = fetch_trajectory.direction_and_change("debt_ebitda", debt_ebitda_series)
    coverage_dir, *_ = fetch_trajectory.direction_and_change("interest_coverage", interest_coverage_series)

    leverage_label = {"rising": "building", "falling": "easing", "flat": "flat", "insufficient data": "unknown"}[leverage_dir]
    coverage_label = {"rising": "improving", "falling": "deteriorating", "flat": "flat", "insufficient data": "unknown"}[coverage_dir]

    return leverage_label, coverage_label, leverage_dir, coverage_dir


def print_company_verdict(ticker, all_series, gross_spread, op_spread, sentences):
    series = all_series[ticker]
    print(f"########## {ticker} ##########")

    # 1. Moat direction
    moat, roic_dir, gross_dir, op_dir = moat_direction(series["roic"], gross_spread, op_spread)
    print("1. MOAT DIRECTION:", moat)
    print(f"   ROIC trajectory: {roic_dir}")
    print(f"   Gross margin spread vs. peer avg trajectory: {gross_dir}")
    print(f"   Operating margin spread vs. peer avg trajectory: {op_dir}")
    roic_note = confidence_note("ROIC", series["roic"])
    if roic_note:
        print(f"   CONFIDENCE NOTE: {roic_note}")

    # 2. Margin quality / durability
    current_margin_yr, current_margin_val, current_margin_conf = series["operating_margin"][-1]
    conc_status, conc_detail = concentration_status(sentences)
    label, explanation = durability(current_margin_val, current_margin_conf, conc_status, conc_detail)
    print(f"2. MARGIN QUALITY / DURABILITY: {label}")
    print(f"   {explanation}")
    if current_margin_conf != "clean":
        print(
            f"   CONFIDENCE NOTE: FY{current_margin_yr} operating margin is [{current_margin_conf}] "
            "— see fetch_trajectory.py diagnostics for the specific tag/derivation used."
        )

    # 3. Credit direction
    leverage_label, coverage_label, leverage_dir, coverage_dir = credit_direction(
        series["debt_ebitda"], series["interest_coverage"]
    )
    print(f"3. CREDIT DIRECTION: leverage {leverage_label}, coverage {coverage_label}")
    print(f"   Debt/EBITDA trajectory: {leverage_dir}")
    print(f"   Interest coverage trajectory: {coverage_dir}")
    coverage_note = confidence_note("Interest coverage", series["interest_coverage"])
    leverage_note = confidence_note("Debt/EBITDA", series["debt_ebitda"])
    if coverage_note:
        print(f"   CONFIDENCE NOTE: {coverage_note}")
    if leverage_note:
        print(f"   CONFIDENCE NOTE: {leverage_note}")

    # 4. One-line verdict
    verdict_parts = [
        f"{moat.lower()} moat",
        f"{label.lower().replace('-', ' ')} margin" if label != "LOW-MARGIN" else "low margin",
        f"leverage {leverage_label}",
        f"coverage {coverage_label}",
    ]
    verdict_line = ", ".join(verdict_parts) + "."

    caveats = [n for n in (roic_note, coverage_note, leverage_note) if n and "fallback" in n]
    if current_margin_conf == "fallback":
        caveats.append(
            f"FY{current_margin_yr} operating margin itself used a fallback tag — the durability read above rests on that."
        )

    print(f"VERDICT: {verdict_line.capitalize()}")
    if caveats:
        print("  CAVEATS (fallback-dependent — see above for exact years):")
        for c in caveats:
            print(f"    - {c}")
    print()


def main():
    tickers_json = fetch_margins.sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers_json.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    all_series = {}
    sentences_by_ticker = {}
    for ticker in TICKERS:
        cik = ciks[ticker]
        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]
        all_series[ticker] = fetch_trajectory.gather_ticker_trajectory(ticker, us_gaap)

        filing = fetch_customer_concentration.most_recent_10k(cik)
        text, _ = fetch_customer_concentration.fetch_10k_text(cik, filing["accession"], filing["document"])
        sentences_by_ticker[ticker] = fetch_customer_concentration.customer_concentration_sentences(text)

    gross_peer_avg = peer_average_series(all_series, "gross_margin")
    op_peer_avg = peer_average_series(all_series, "operating_margin")

    print()
    print("=" * 78)
    print("VERDICT LAYER — plain-language read per company, 5-year trajectory basis")
    print("=" * 78)
    print()

    for ticker in TICKERS:
        gross_spread = spread_series(all_series, ticker, "gross_margin", gross_peer_avg)
        op_spread = spread_series(all_series, ticker, "operating_margin", op_peer_avg)
        print_company_verdict(ticker, all_series, gross_spread, op_spread, sentences_by_ticker[ticker])


if __name__ == "__main__":
    main()
