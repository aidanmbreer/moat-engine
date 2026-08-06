"""
Trust test, not a feature: wire a resolved ticker into ONE metric —
gross margin, most recent fiscal year — with mandatory confidence
reporting, and run it on companies this tool has never seen before.

Pipeline: resolve_ticker.resolve_ticker() (CIK + confirmed 10-K,
unchanged from that slice) -> fetch companyfacts for that CIK ->
fetch_margins.compute_margins() (run for gross margin only — operating
margin comes along for free since compute_margins computes both, but
only gross margin is reported here, per this slice's scope). No other
metric is wired in.

This script previously found a real bug on NVDA: compute_margins()'s
"most recent" resolution picked whichever candidate revenue tag merely
existed anywhere in the filer's history, not whichever one actually had
the most recent data, and silently reported FY2022 figures as "clean."
Fixed in fetch_margins.most_recent_fact_among_candidates() (compares
every candidate's own most-recent fact and picks the genuinely most
recent one) and fetch_roic_others.compute_roic() (same helper, applied
to its own "most recent" anchor tag), which fetch_credit_metrics.py
inherits automatically since it delegates its own "most recent"
resolution to compute_margins.

Confidence, redefined after that fix: gross margin is only reported
"clean" if (a) compute_margins() succeeded, AND (b) the fiscal year it
resolved to matches resolve_ticker()'s independently-found most recent
10-K fiscal year end — a cross-check against the filer's submissions
index, not against XBRL tag data, so it can't share the same blind spot
a tag-based bug could have. A mismatch (data found, but for an older
year than the filing's actual most recent one) is reported "STALE," not
"clean," even though a tag DID match. A compute_margins() failure is
"UNRESOLVED." Nothing here guesses a number for either case.

Standalone — does not modify or import fetch_customer_concentration.py,
fetch_credit_metrics.py, fetch_trajectory.py, fetch_verdict.py, or
consolidate.py, and does not touch the existing four-company TICKERS
list in fetch_margins.py or fetch_roic_others.py.
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

    base = {
        "ticker": ticker_norm,
        "company_name": resolution["company_name"],
        "cik": cik,
        "fiscal_year": margins["fiscal_year"],
        "fiscal_year_end": margins["fiscal_year_end"],
        "authoritative_fiscal_year_end": resolution["fiscal_year_end"],
        "revenue": margins["revenue"],
        "revenue_tag": margins["revenue_tag"],
        "cost_of_revenue": margins["cost_of_revenue"],
        "cost_of_revenue_tag": margins["cost_of_revenue_tag"],
        "gross_margin": margins["gross_margin"],
    }

    # Cross-check against resolve_ticker()'s independently-found most
    # recent 10-K fiscal year end (from the filer's submissions index,
    # not from XBRL tag data) — a value only earns "clean" if the tag
    # resolution actually landed on the filing's true most recent year.
    if margins["fiscal_year_end"] != resolution["fiscal_year_end"]:
        base["status"] = "stale"
        base["reason"] = (
            f"compute_margins() resolved to FY ended {margins['fiscal_year_end']} using tag "
            f"[{margins['revenue_tag']}], but resolve_ticker() independently found this company's "
            f"actual most recent 10-K is for FY ended {resolution['fiscal_year_end']} "
            f"(accession {resolution['accession_number']}, filed {resolution['filing_date']}). "
            "The gross margin figure above is real data, just not for the current year — do not "
            "treat it as this company's latest reported gross margin."
        )
        return base

    base["status"] = "clean"
    return base


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

    print(f"Company: {result['company_name']}  (CIK {result['cik']})")
    print(f"Fiscal year: FY{result['fiscal_year']} (ended {result['fiscal_year_end']})")
    print(f"Revenue tag used:          {result['revenue_tag']}  = ${result['revenue']:,}")
    print(f"Cost-of-revenue tag used:  {result['cost_of_revenue_tag']}  = ${result['cost_of_revenue']:,}")
    print(f"Gross margin: {result['gross_margin']:.2%}")

    if result["status"] == "stale":
        print("CONFIDENCE: STALE")
        print(f"  *** {result['reason']} ***")
    else:
        print(
            "CONFIDENCE: CLEAN — both tags matched directly, nothing derived or substituted, "
            f"and the resolved fiscal year (ended {result['fiscal_year_end']}) matches "
            f"resolve_ticker()'s independently-found most recent 10-K."
        )
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
