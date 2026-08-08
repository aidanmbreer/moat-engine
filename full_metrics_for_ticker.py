"""
Full metric set — gross margin, operating margin, ROIC, debt/EBITDA,
net debt/EBITDA, interest coverage — for an arbitrary resolved ticker,
most recent fiscal year, with the confidence framework applied
uniformly to every metric.

Pipeline, all reused unchanged: resolve_ticker.resolve_ticker() (CIK +
confirmed 10-K + the authoritative "actual most recent fiscal year
end," from the filer's submissions index) -> companyfacts fetch ->
fetch_margins.compute_margins() [gross margin, operating margin] ->
fetch_roic_others.compute_roic() [ROIC] ->
fetch_credit_metrics.compute_credit_metrics() [debt/EBITDA, net
debt/EBITDA, interest coverage]. All three compute functions already
carry the most_recent_fact_among_candidates() fix, so none of them
should silently resolve to a stale tag anymore — but this script still
verifies that per metric rather than assuming it, per-metric, exactly
as the task requires.

Confidence, per metric, in priority order:
  1. compute_*() raised -> UNRESOLVED. No number reported.
  2. The metric's own resolved fiscal year end doesn't match
     resolve_ticker()'s independently-found authoritative one -> STALE
     (both years shown). Checked before anything else, since landing on
     the wrong year is a more fundamental problem than which tag or
     derivation was used within that (wrong) year.
  3. Otherwise, the metric's baseline confidence — CLEAN / DERIVED /
     FALLBACK — comes from fetch_trajectory.classify_confidence(),
     reused directly (not reimplemented) against that metric's own
     flags, exactly as fetch_trajectory.py and fetch_verdict.py already
     do for the four known companies.

Standalone — does not modify fetch_margins.py, fetch_roic_others.py,
fetch_trajectory.py, fetch_verdict.py, consolidate.py, or
resolve_ticker.py, and does not touch any existing TICKERS list. Only
reads their already-fixed, already-verified logic. It does read
fetch_credit_metrics.compute_credit_metrics()'s per-metric resolution
(debt/EBITDA, net debt/EBITDA, and interest coverage each resolve off
only their own inputs — see that function's docstring) so that a
failure on one input (e.g. HON's interest expense, tagged as
InterestAndDebtExpense, not currently a candidate) reports UNRESOLVED
only for the metric(s) that actually need it, not all three.
"""

import fetch_credit_metrics
import fetch_margins
import fetch_roic_others
import fetch_trajectory
import resolve_ticker

METRIC_ORDER = ["gross_margin", "operating_margin", "roic", "debt_ebitda", "net_debt_ebitda", "interest_coverage"]
METRIC_LABELS = {
    "gross_margin": "Gross Margin",
    "operating_margin": "Operating Margin",
    "roic": "ROIC",
    "debt_ebitda": "Debt / EBITDA",
    "net_debt_ebitda": "Net Debt / EBITDA",
    "interest_coverage": "Interest Coverage (EBITDA / Interest)",
}
MULTIPLE_METRICS = {"debt_ebitda", "net_debt_ebitda", "interest_coverage"}


def confidence_for_metric(computed_fiscal_year_end, authoritative_fiscal_year_end, baseline_confidence):
    """Staleness (wrong year) always overrides the baseline tag-quality
    confidence (clean/derived/fallback) — a value can only be "clean" if
    it's both well-tagged AND for the right year."""
    if computed_fiscal_year_end != authoritative_fiscal_year_end:
        return "stale"
    return baseline_confidence


def fmt_value(metric, value):
    if metric in MULTIPLE_METRICS:
        return f"{value:.2f}x"
    return f"{value:.2%}"


def full_metrics_for_ticker(ticker):
    resolution = resolve_ticker.resolve_ticker(ticker)
    if resolution["status"] == "error":
        return {"status": "error", "ticker": resolution["ticker"], "reason": resolution["reason"]}

    ticker_norm = resolution["ticker"]
    cik = resolution["cik"]
    authoritative_fy_end = resolution["fiscal_year_end"]

    try:
        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
    except Exception as e:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": f"Resolved to CIK {cik} ({resolution['company_name']}) but could not fetch companyfacts: {e}",
        }

    us_gaap = facts["facts"]["us-gaap"]
    metrics = {}

    # --- gross margin + operating margin ---
    # These resolve independently (see compute_margins docstring) — a
    # filer that doesn't tag a single cost-of-revenue figure (e.g. ORCL)
    # can still have operating margin resolve on its own merits.
    try:
        margins = fetch_margins.compute_margins(ticker_norm, us_gaap)
    except Exception as e:
        metrics["gross_margin"] = {"status": "unresolved", "reason": str(e)}
        metrics["operating_margin"] = {"status": "unresolved", "reason": str(e)}
    else:
        if margins["gross_margin"] is None:
            metrics["gross_margin"] = {"status": "unresolved", "reason": margins["cost_of_revenue_error"]}
        else:
            gm_baseline = fetch_trajectory.classify_confidence([margins["gross_margin_flags"]])
            metrics["gross_margin"] = {
                "status": confidence_for_metric(margins["fiscal_year_end"], authoritative_fy_end, gm_baseline),
                "value": margins["gross_margin"],
                "tag": f"{margins['revenue_tag']} / {margins['cost_of_revenue_tag']}",
                "fiscal_year_end": margins["fiscal_year_end"],
                "flags": margins["gross_margin_flags"],
            }

        if margins["operating_margin"] is None:
            metrics["operating_margin"] = {"status": "unresolved", "reason": margins["operating_income_error"]}
        else:
            om_baseline = fetch_trajectory.classify_confidence([margins["flags"]])
            metrics["operating_margin"] = {
                "status": confidence_for_metric(margins["fiscal_year_end"], authoritative_fy_end, om_baseline),
                "value": margins["operating_margin"],
                "tag": "OperatingIncomeLoss" if om_baseline == "clean" else "derived (Revenue - COGS - SG&A - R&D)",
                "fiscal_year_end": margins["fiscal_year_end"],
            }

    # --- ROIC ---
    try:
        roic = fetch_roic_others.compute_roic(ticker_norm, us_gaap)
    except Exception as e:
        metrics["roic"] = {"status": "unresolved", "reason": str(e)}
    else:
        roic_baseline = fetch_trajectory.classify_confidence([roic["flags"]])
        metrics["roic"] = {
            "status": confidence_for_metric(roic["fiscal_year_end"], authoritative_fy_end, roic_baseline),
            "value": roic["roic"],
            "tag": "NOPAT / invested capital (operating income, tax, debt, equity, cash tags — see flags/trace above)",
            "fiscal_year_end": roic["fiscal_year_end"],
            "flags": roic["flags"],
        }

    # --- credit metrics ---
    # debt/EBITDA, net debt/EBITDA, and interest coverage each resolve
    # off only their own inputs (see compute_credit_metrics docstring),
    # so a failure on one input (e.g. HON's interest expense) is checked
    # and reported per metric, not for all three at once.
    try:
        credit = fetch_credit_metrics.compute_credit_metrics(ticker_norm, us_gaap)
    except Exception as e:
        for m in ("debt_ebitda", "net_debt_ebitda", "interest_coverage"):
            metrics[m] = {"status": "unresolved", "reason": str(e)}
    else:
        if credit["leverage"] is None:
            metrics["debt_ebitda"] = {"status": "unresolved", "reason": credit["leverage_error"]}
        else:
            de_baseline = fetch_trajectory.classify_confidence(
                [credit["margins_flags"], credit["da_flags"], credit["debt_flags"]]
            )
            metrics["debt_ebitda"] = {
                "status": confidence_for_metric(credit["fiscal_year_end"], authoritative_fy_end, de_baseline),
                "value": credit["leverage"],
                "tag": f"total debt (see trace) / D&A[{credit['da_tag']}]",
                "fiscal_year_end": credit["fiscal_year_end"],
                "flags": credit["margins_flags"] + credit["da_flags"] + credit["debt_flags"],
            }

        if credit["net_leverage"] is None:
            metrics["net_debt_ebitda"] = {"status": "unresolved", "reason": credit["net_leverage_error"]}
        else:
            nde_baseline = fetch_trajectory.classify_confidence(
                [credit["margins_flags"], credit["da_flags"], credit["cash_flags"], credit["debt_flags"]]
            )
            metrics["net_debt_ebitda"] = {
                "status": confidence_for_metric(credit["fiscal_year_end"], authoritative_fy_end, nde_baseline),
                "value": credit["net_leverage"],
                "tag": "total debt / cash (see trace) / D&A[{}]".format(credit["da_tag"]),
                "fiscal_year_end": credit["fiscal_year_end"],
                "flags": credit["margins_flags"] + credit["da_flags"] + credit["cash_flags"] + credit["debt_flags"],
            }

        if credit["interest_coverage"] is None:
            metrics["interest_coverage"] = {"status": "unresolved", "reason": credit["interest_coverage_error"]}
        else:
            ic_baseline = fetch_trajectory.classify_confidence(
                [credit["margins_flags"], credit["da_flags"], credit["interest_flags"]]
            )
            metrics["interest_coverage"] = {
                "status": confidence_for_metric(credit["fiscal_year_end"], authoritative_fy_end, ic_baseline),
                "value": credit["interest_coverage"],
                "tag": f"Interest[{credit['interest_expense_tag']}] / D&A[{credit['da_tag']}]",
                "fiscal_year_end": credit["fiscal_year_end"],
                "flags": credit["margins_flags"] + credit["da_flags"] + credit["interest_flags"],
            }

    return {
        "status": "ok",
        "ticker": ticker_norm,
        "company_name": resolution["company_name"],
        "cik": cik,
        "authoritative_fiscal_year_end": authoritative_fy_end,
        "metrics": metrics,
        # Exposed so callers (analyze.py) can reuse this same already-fetched
        # companyfacts response for further computation (e.g. the 5-year
        # trajectory) without a second network fetch. Purely additive — no
        # existing key changed, no existing caller reads this.
        "us_gaap": us_gaap,
        # Also additive — propagated from resolve_ticker() for analyze.py's
        # scope declaration (validated-sector check).
        "sic": resolution["sic"],
        "sic_description": resolution["sic_description"],
    }


def print_result(result):
    print(f"########## {result['ticker']} ##########")
    if result["status"] == "error":
        print(f"CANNOT PROCESS: {result['reason']}")
        print()
        return

    print(f"Company: {result['company_name']}  (CIK {result['cik']})")
    print(f"Authoritative most recent fiscal year end (from resolve_ticker): {result['authoritative_fiscal_year_end']}")
    print()

    for key in METRIC_ORDER:
        m = result["metrics"][key]
        label = METRIC_LABELS[key]
        status = m["status"]

        if status == "unresolved":
            print(f"{label}: UNRESOLVED — {m['reason']}")
            continue

        value_str = fmt_value(key, m["value"])
        print(f"{label}: {value_str}  [{status.upper()}]")
        print(f"  Tag/method: {m['tag']}")
        print(f"  Fiscal year used: {m['fiscal_year_end']}")
        if status == "stale":
            print(
                f"  *** STALE: resolved to FY ended {m['fiscal_year_end']}, but the filing's actual "
                f"most recent fiscal year ends {result['authoritative_fiscal_year_end']}. "
                "Do not treat this as the current figure. ***"
            )
        elif status in ("derived", "fallback") and m.get("flags"):
            for flag in m["flags"]:
                print(f"  FLAG: {flag}")
    print()


def main():
    test_tickers = ["CAT", "HON", "NVDA"]
    for ticker in test_tickers:
        result = full_metrics_for_ticker(ticker)
        print_result(result)


if __name__ == "__main__":
    main()
