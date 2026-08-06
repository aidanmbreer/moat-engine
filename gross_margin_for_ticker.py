"""
Trust test, not a feature: wire a resolved ticker into ONE metric —
gross margin, most recent fiscal year — with mandatory confidence
reporting, and run it on companies this tool has never seen before.

Pipeline: resolve_ticker.resolve_ticker() (CIK + confirmed 10-K,
unchanged from that slice) -> fetch companyfacts for that CIK ->
fetch_margins.compute_margins() (unchanged calculation, run for gross
margin only — operating margin comes along for free since
compute_margins computes both, but only gross margin is reported here,
per this slice's scope). No other metric is wired in.

Confidence for gross margin specifically: compute_margins() currently
has no derived/fallback path for revenue or cost-of-revenue resolution
(each is either found directly under one of a short list of candidate
tags, or the call raises and gross margin is unresolved) — so, exactly
as already established in fetch_trajectory.py's own per-metric
confidence assignment, gross margin's confidence is "clean" whenever
the call succeeds and "unresolved" whenever it doesn't. This is not
guessed here; it's read off whether the existing tag-resolution logic
found a match, using the same tag candidate lists already validated on
the four known companies (VRT, ETN, PWR, NVT) — unchanged, not
expanded for these new companies, since the entire point of this test
is to see whether that logic generalizes as-is.

Standalone — does not modify or import fetch_roic_others.py,
fetch_customer_concentration.py, fetch_credit_metrics.py,
fetch_trajectory.py, fetch_verdict.py, or consolidate.py, and does not
touch the existing four-company TICKERS list in fetch_margins.py.
"""

import fetch_margins
import resolve_ticker


def gross_margin_for_ticker(ticker):
    """Resolve `ticker` and run gross margin (only) through
    fetch_margins.compute_margins(), most recent fiscal year. Returns a
    dict describing the outcome; never raises for a bad ticker or a
    resolution/computation failure."""
    resolution = resolve_ticker.resolve_ticker(ticker)
    if resolution["status"] == "error":
        return {"status": "error", "ticker": resolution["ticker"], "stage": "resolve", "reason": resolution["reason"]}

    cik = resolution["cik"]
    ticker_norm = resolution["ticker"]

    try:
        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
    except Exception as e:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "stage": "companyfacts",
            "reason": f"Resolved to CIK {cik} ({resolution['company_name']}) but could not fetch companyfacts: {e}",
        }

    us_gaap = facts["facts"]["us-gaap"]

    try:
        margins = fetch_margins.compute_margins(ticker_norm, us_gaap)
    except Exception as e:
        return {
            "status": "unresolved",
            "ticker": ticker_norm,
            "company_name": resolution["company_name"],
            "cik": cik,
            "reason": str(e),
        }

    return {
        "status": "clean",
        "ticker": ticker_norm,
        "company_name": resolution["company_name"],
        "cik": cik,
        "fiscal_year": margins["fiscal_year"],
        "fiscal_year_end": margins["fiscal_year_end"],
        "revenue": margins["revenue"],
        "revenue_tag": margins["revenue_tag"],
        "cost_of_revenue": margins["cost_of_revenue"],
        "cost_of_revenue_tag": margins["cost_of_revenue_tag"],
        "gross_margin": margins["gross_margin"],
    }


def print_result(result):
    print(f"########## {result['ticker']} ##########")

    if result["status"] == "error":
        print(f"CANNOT PROCESS ({result['stage']} failed): {result['reason']}")
        print()
        return

    if result["status"] == "unresolved":
        print(f"Company: {result['company_name']}  (CIK {result['cik']})")
        print("CONFIDENCE: UNRESOLVED")
        print(f"  *** Gross margin could NOT be computed for this company. No number is being reported. ***")
        print(f"  Reason: {result['reason']}")
        print()
        return

    # status == "clean"
    print(f"Company: {result['company_name']}  (CIK {result['cik']})")
    print(f"Fiscal year: FY{result['fiscal_year']} (ended {result['fiscal_year_end']})")
    print(f"Revenue tag used:          {result['revenue_tag']}  = ${result['revenue']:,}")
    print(f"Cost-of-revenue tag used:  {result['cost_of_revenue_tag']}  = ${result['cost_of_revenue']:,}")
    print(f"Gross margin: {result['gross_margin']:.2%}")
    print("CONFIDENCE: CLEAN — both tags matched directly, nothing derived or substituted.")
    print()


def main():
    # Three large-cap names outside the existing four-company coverage
    # set, hand-unchecked on this exact metric.
    test_tickers = ["CAT", "HON", "NVDA"]
    for ticker in test_tickers:
        result = gross_margin_for_ticker(ticker)
        print_result(result)


if __name__ == "__main__":
    main()
