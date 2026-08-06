"""
Pull a 5-fiscal-year trajectory (instead of a single most-recent year)
for gross margin, operating margin, ROIC, debt/EBITDA, net debt/EBITDA,
and interest coverage, for Vertiv (VRT), Eaton (ETN), Quanta Services
(PWR), and nVent (NVT).

This is the foundation for trajectory analysis. It reuses the exact
compute_margins() / compute_roic() / compute_credit_metrics() logic
already validated for the most recent fiscal year in fetch_margins.py,
fetch_roic_others.py, and fetch_credit_metrics.py — those functions
were extended with an optional fiscal_year_end argument for this
feature (every existing caller still passes none, targets "most
recent" as before, and is unaffected) and are run here once per
discovered fiscal year instead of once for "most recent." No
calculation is reimplemented; only which period each is pointed at.

The real point of this script: older fiscal years often use different
XBRL tags than the most recent one — a tag can go stale, get renamed,
or disappear entirely between filings (this is the single most common
way an automated multi-year pull silently goes wrong). Every value
pulled here is tagged with a confidence level so that's never smoothed
over:
  - "clean"      = the standard/expected tag was found directly.
  - "derived"    = the value was built from components via a documented
                    formula fallback (e.g. Eaton-style operating income
                    from Revenue - COGS - SG&A - R&D).
  - "fallback"   = a substitute tag was used that may carry a different
                    economic basis than the preferred one (e.g. NVT-style
                    net interest expense used in place of gross).
  - "unresolved" = no usable tag was found for that year at all. The
                    year is reported as missing — never guessed.

Confidence is read directly off the flag text the underlying compute
functions already produce (see classify_confidence()); it is not a new
judgment layer independent of what those functions already flag.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
5 requests total (one shared ticker lookup + one companyfacts call per
company) — all five years, across all three metric families, come out
of that single companyfacts response per company.
"""

import fetch_credit_metrics
import fetch_margins
import fetch_roic_others

TICKERS = ["VRT", "ETN", "PWR", "NVT"]
YEARS_BACK = 5

# "Flat" bands per metric type — small moves shouldn't read as a trend.
FLAT_THRESHOLDS = {
    "gross_margin": 0.005,       # 0.5 percentage points
    "operating_margin": 0.005,
    "roic": 0.005,
    "debt_ebitda": 0.1,          # 0.1x
    "net_debt_ebitda": 0.1,
    "interest_coverage": 0.5,    # coverage ratios swing more; wider band
}

METRIC_LABELS = {
    "gross_margin": "Gross Margin",
    "operating_margin": "Operating Margin",
    "roic": "ROIC",
    "debt_ebitda": "Debt / EBITDA",
    "net_debt_ebitda": "Net Debt / EBITDA",
    "interest_coverage": "Interest Coverage (EBITDA / Interest)",
}

PERCENT_METRICS = {"gross_margin", "operating_margin", "roic"}


def classify_confidence(flag_lists):
    """Worst-of confidence across one or more flag-string lists, reading
    the same language the underlying compute_*() functions already use —
    this does not re-judge tag quality, it reads what those functions
    already flagged.
      - any flag mentioning "instead" (a substitute tag was used) -> "fallback"
      - any flag mentioning "derived"/"proxy" (built from components) -> "derived"
      - otherwise -> "clean"
    "fallback" ranks worse than "derived": a component-sum proxy is a
    documented GAAP identity and fully checkable; a substitute tag may
    carry a different economic meaning altogether.
    """
    joined = " ".join(f for flags in flag_lists for f in flags).lower()
    if "instead" in joined:
        return "fallback"
    if "derived" in joined or "proxy" in joined:
        return "derived"
    return "clean"


def fmt_value(metric, value):
    if value is None:
        return "—"
    if metric in PERCENT_METRICS:
        return f"{value:.2%}"
    return f"{value:.2f}x"


def direction_and_change(metric, series):
    """series: list of (year_label, value, confidence), oldest -> newest.
    Direction/change compares the earliest and latest AVAILABLE
    (non-None) values — unresolved years are skipped, never treated as
    zero or interpolated."""
    available = [(yr, val) for yr, val, _ in series if val is not None]
    if len(available) < 2:
        return "insufficient data", None, None, None

    earliest_yr, earliest_val = available[0]
    latest_yr, latest_val = available[-1]
    change = latest_val - earliest_val

    threshold = FLAT_THRESHOLDS[metric]
    if abs(change) < threshold:
        direction = "flat"
    elif change > 0:
        direction = "rising"
    else:
        direction = "falling"

    return direction, change, (earliest_yr, earliest_val), (latest_yr, latest_val)


def gather_ticker_trajectory(ticker, us_gaap):
    fiscal_year_ends = fetch_margins.recent_fiscal_year_ends(us_gaap, count=YEARS_BACK)
    fiscal_year_ends = list(reversed(fiscal_year_ends))  # oldest -> newest for a "trajectory"

    series = {metric: [] for metric in METRIC_LABELS}

    for fy_end in fiscal_year_ends:
        year_label = fy_end[:4]

        try:
            margins = fetch_margins.compute_margins(ticker, us_gaap, fiscal_year_end=fy_end)
        except Exception as e:
            series["gross_margin"].append((year_label, None, "unresolved"))
            series["operating_margin"].append((year_label, None, "unresolved"))
            print(f"[{ticker} FY{year_label}] margins UNRESOLVED: {e}\n")
        else:
            series["gross_margin"].append((year_label, margins["gross_margin"], "clean"))
            series["operating_margin"].append(
                (year_label, margins["operating_margin"], classify_confidence([margins["flags"]]))
            )

        try:
            roic = fetch_roic_others.compute_roic(ticker, us_gaap, fiscal_year_end=fy_end)
        except Exception as e:
            series["roic"].append((year_label, None, "unresolved"))
            print(f"[{ticker} FY{year_label}] ROIC UNRESOLVED: {e}\n")
        else:
            series["roic"].append((year_label, roic["roic"], classify_confidence([roic["flags"]])))

        try:
            credit = fetch_credit_metrics.compute_credit_metrics(ticker, us_gaap, fiscal_year_end=fy_end)
        except Exception as e:
            series["debt_ebitda"].append((year_label, None, "unresolved"))
            series["net_debt_ebitda"].append((year_label, None, "unresolved"))
            series["interest_coverage"].append((year_label, None, "unresolved"))
            print(f"[{ticker} FY{year_label}] credit metrics UNRESOLVED: {e}\n")
        else:
            debt_ebitda_conf = classify_confidence([credit["margins_flags"], credit["da_flags"]])
            net_debt_ebitda_conf = classify_confidence(
                [credit["margins_flags"], credit["da_flags"], credit["cash_flags"]]
            )
            interest_coverage_conf = classify_confidence(
                [credit["margins_flags"], credit["da_flags"], credit["interest_flags"]]
            )
            series["debt_ebitda"].append((year_label, credit["leverage"], debt_ebitda_conf))
            series["net_debt_ebitda"].append((year_label, credit["net_leverage"], net_debt_ebitda_conf))
            series["interest_coverage"].append((year_label, credit["interest_coverage"], interest_coverage_conf))

    return series


def print_trajectory_summary(ticker, series):
    print(f"########## {ticker} — 5-year trajectory summary ##########")
    for metric, label in METRIC_LABELS.items():
        s = series[metric]
        series_str = "  ".join(f"FY{yr}: {fmt_value(metric, val)} [{conf}]" for yr, val, conf in s)
        direction, change, earliest, latest = direction_and_change(metric, s)

        print(f"{label}:")
        print(f"  {series_str}")
        if direction == "insufficient data":
            print("  Direction: insufficient data (fewer than 2 resolved years)")
        else:
            change_str = fmt_value(metric, abs(change))
            sign = "+" if change >= 0 else "-"
            print(
                f"  Direction: {direction}  |  Change (FY{earliest[0]} -> FY{latest[0]}): "
                f"{sign}{change_str} ({fmt_value(metric, earliest[1])} -> {fmt_value(metric, latest[1])})"
            )
    print()


def print_handcheck_list(all_series):
    print("########## Years to hand-check (fallback or unresolved) ##########")
    any_flagged = False
    for ticker in TICKERS:
        series = all_series[ticker]
        for metric, label in METRIC_LABELS.items():
            for yr, val, conf in series[metric]:
                if conf in ("fallback", "unresolved"):
                    any_flagged = True
                    val_str = fmt_value(metric, val) if val is not None else "MISSING"
                    print(f"  [{ticker}] {label} FY{yr}: {conf.upper()} ({val_str})")
    if not any_flagged:
        print("  None — every year of every metric resolved cleanly or via a documented derivation.")
    print()


def main():
    tickers_json = fetch_margins.sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers_json.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    all_series = {}
    for ticker in TICKERS:
        cik = ciks[ticker]
        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]
        all_series[ticker] = gather_ticker_trajectory(ticker, us_gaap)

    print()
    print("=" * 78)
    print("TRAJECTORY SUMMARY (5 fiscal years, oldest -> newest)")
    print("=" * 78)
    print()
    for ticker in TICKERS:
        print_trajectory_summary(ticker, all_series[ticker])

    print_handcheck_list(all_series)


if __name__ == "__main__":
    main()
