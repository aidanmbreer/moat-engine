"""
Compute credit/leverage metrics for Vertiv (VRT), Eaton (ETN), Quanta
Services (PWR), and nVent (NVT) for the most recent fiscal year using
SEC EDGAR's companyfacts API.

Definitions:
- EBITDA = Operating income + D&A. Operating income comes from
  fetch_margins.compute_margins() (same value already validated there,
  including the Eaton Revenue - COGS - SG&A - R&D proxy — not
  recomputed here). D&A is pulled from the cash flow statement's
  DepreciationDepletionAndAmortization tag where a filer reports that
  single combined figure (Vertiv, Eaton). Quanta and nVent don't tag a
  combined D&A line at all (confirmed by inspecting their companyfacts
  responses), so D&A there is derived as
  Depreciation + AmortizationOfIntangibleAssets — flagged, and
  excludes any separately tagged finance-lease right-of-use asset
  amortization, which is a much smaller line.
- Total debt / net debt: total debt reuses
  fetch_roic_others.total_debt_for_roic() directly, unmodified. Net
  debt = total debt - cash. Cash uses the same tag preference already
  validated in fetch_roic_others.compute_roic() (not a new fallback
  order, just applied here since compute_roic() doesn't expose cash
  lookup as its own function).
- Interest expense: preferred tag is InterestExpense; several filers
  don't tag that for FY2025 (it went stale after an earlier year), so
  InterestExpenseNonoperating is tried next. nVent doesn't tag either
  for FY2025 — it dropped to reporting only InterestIncomeExpenseNet
  starting FY2024 — so nVent's interest expense falls back to
  |InterestIncomeExpenseNet|, which is interest expense NET of
  interest income, not gross. That's flagged explicitly since it
  overstates nVent's coverage ratio relative to a gross-expense basis.
- Total debt / EBITDA (leverage), Net debt / EBITDA, and
  EBITDA / Interest expense (coverage) follow directly from the above.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
5 requests total (one shared ticker lookup + one companyfacts call per
company), with a short pause between each — it does not re-fetch
companyfacts separately for margins vs. debt vs. cash, since all of
those are derived from the single companyfacts response already
fetched per company.
"""

import fetch_margins
import fetch_roic_others

TICKERS = ["VRT", "ETN", "PWR", "NVT"]

INTEREST_EXPENSE_TAG_CANDIDATES = ["InterestExpense", "InterestExpenseNonoperating"]


def cash_and_equivalents(us_gaap, fiscal_year_end):
    """Same tag preference already validated in
    fetch_roic_others.compute_roic(): CashAndCashEquivalentsAtCarryingValue,
    falling back to CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents
    (which may include a small restricted-cash component) when the primary
    tag isn't reported.
    """
    fact = fetch_roic_others.annual_instant_fact(
        us_gaap, "CashAndCashEquivalentsAtCarryingValue", fiscal_year_end, required=False
    )
    if fact is not None:
        return fact["val"], "CashAndCashEquivalentsAtCarryingValue", None

    fact = fetch_roic_others.annual_instant_fact(
        us_gaap, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", fiscal_year_end, required=True
    )
    return (
        fact["val"],
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "may include a small restricted-cash component — hand-check against the balance sheet",
    )


def depreciation_and_amortization(ticker, us_gaap, fiscal_year_end):
    flags = []
    combined = fetch_roic_others.annual_duration_fact_at(
        us_gaap, "DepreciationDepletionAndAmortization", fiscal_year_end, required=False
    )
    if combined is not None:
        return combined["val"], "DepreciationDepletionAndAmortization", flags

    dep = fetch_roic_others.annual_duration_fact_at(us_gaap, "Depreciation", fiscal_year_end, required=False)
    amort = fetch_roic_others.annual_duration_fact_at(
        us_gaap, "AmortizationOfIntangibleAssets", fiscal_year_end, required=False
    )
    if dep is None or amort is None:
        candidates = fetch_roic_others.tags_at_date(us_gaap, fiscal_year_end, "Depreciation")
        candidates.update(fetch_roic_others.tags_at_date(us_gaap, fiscal_year_end, "Amortization"))
        candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
        raise RuntimeError(
            f"No combined DepreciationDepletionAndAmortization tag for {ticker}, and the "
            f"Depreciation + AmortizationOfIntangibleAssets fallback is incomplete. "
            f"Depreciation/Amortization tags available as of {fiscal_year_end}:\n{candidate_lines}"
        )

    value = dep["val"] + amort["val"]
    flags.append(
        f"No combined DepreciationDepletionAndAmortization tag; D&A derived as "
        f"Depreciation (${dep['val']:,}) + AmortizationOfIntangibleAssets (${amort['val']:,}) = ${value:,}. "
        "Excludes finance-lease right-of-use asset amortization if separately tagged — hand-check against the cash flow statement."
    )
    return value, "Depreciation + AmortizationOfIntangibleAssets (derived)", flags


def interest_expense(ticker, us_gaap, fiscal_year_end):
    flags = []
    for tag in INTEREST_EXPENSE_TAG_CANDIDATES:
        fact = fetch_roic_others.annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            if tag != INTEREST_EXPENSE_TAG_CANDIDATES[0]:
                flags.append(
                    f"InterestExpense tag not reported for FY ended {fiscal_year_end}; used {tag} instead."
                )
            return fact["val"], tag, flags

    fact = fetch_roic_others.annual_duration_fact_at(us_gaap, "InterestIncomeExpenseNet", fiscal_year_end, required=False)
    if fact is not None:
        value = abs(fact["val"])
        flags.append(
            f"No gross interest expense tag ({' / '.join(INTEREST_EXPENSE_TAG_CANDIDATES)}) reported for FY ended "
            f"{fiscal_year_end}; used |InterestIncomeExpenseNet| = ${value:,} instead. This is interest expense "
            "NET of interest income, not gross interest expense — EBITDA/interest coverage will read stronger than "
            "on a gross-expense basis. Hand-check against the filing's interest expense footnote."
        )
        return value, "InterestIncomeExpenseNet (net, absolute value)", flags

    candidates = fetch_roic_others.tags_at_date(us_gaap, fiscal_year_end, "Interest")
    candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
    raise RuntimeError(
        f"No interest expense tag found for {ticker} as of {fiscal_year_end}. "
        f"'Interest'-containing tags available:\n{candidate_lines}"
    )


def compute_credit_metrics(ticker, us_gaap):
    flags = []

    margins = fetch_margins.compute_margins(ticker, us_gaap)
    fiscal_year_end = margins["fiscal_year_end"]
    fiscal_year = margins["fiscal_year"]
    operating_income = margins["operating_income"]

    debt = fetch_roic_others.total_debt_for_roic(us_gaap, fiscal_year_end)
    total_debt = debt["total"]

    cash, cash_tag, cash_note = cash_and_equivalents(us_gaap, fiscal_year_end)
    if cash_note:
        flags.append(f"{cash_tag} used instead of CashAndCashEquivalentsAtCarryingValue — {cash_note}")

    da_value, da_tag, da_flags = depreciation_and_amortization(ticker, us_gaap, fiscal_year_end)
    flags.extend(da_flags)

    interest_value, interest_tag, interest_flags = interest_expense(ticker, us_gaap, fiscal_year_end)
    flags.extend(interest_flags)

    ebitda = operating_income + da_value
    net_debt = total_debt - cash

    leverage = total_debt / ebitda
    net_leverage = net_debt / ebitda
    interest_coverage = ebitda / interest_value

    print(f"Fiscal year: FY{fiscal_year} (ended {fiscal_year_end})")
    print(f"Operating income: ${operating_income:,}")
    print(f"D&A [{da_tag}]: ${da_value:,}")
    print(f"EBITDA (operating income + D&A): ${ebitda:,}")
    print(
        "Total debt: "
        f"${total_debt:,} "
        f"(current debt [{debt['current_debt_tag']}] ${debt['current_debt']:,} "
        f"+ noncurrent debt [{debt['noncurrent_debt_tag']}] ${debt['noncurrent_debt']:,} "
        f"+ operating lease liability (noncurrent) ${debt['operating_lease_liability_noncurrent']:,} "
        f"+ finance lease liability (noncurrent) ${debt['finance_lease_liability_noncurrent']:,}"
        + (f" [{debt['finance_lease_note']}]" if debt["finance_lease_note"] else "")
        + ")"
    )
    print(f"Cash and equivalents [{cash_tag}]: ${cash:,}")
    print(f"Net debt (total debt - cash): ${net_debt:,}")
    print(f"Interest expense [{interest_tag}]: ${interest_value:,}")
    print(f"Total debt / EBITDA: {leverage:.2f}x")
    print(f"Net debt / EBITDA: {net_leverage:.2f}x")
    print(f"EBITDA / Interest expense: {interest_coverage:.2f}x")
    for flag in flags:
        print(f"FLAG: {flag}")
    print()

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "operating_income": operating_income,
        "da": da_value,
        "da_tag": da_tag,
        "ebitda": ebitda,
        "total_debt": total_debt,
        "cash": cash,
        "net_debt": net_debt,
        "interest_expense": interest_value,
        "interest_expense_tag": interest_tag,
        "leverage": leverage,
        "net_leverage": net_leverage,
        "interest_coverage": interest_coverage,
        "flags": flags,
    }


def print_summary_table(results):
    if not results:
        print("No companies computed successfully.")
        return

    headers = ["Ticker", "FY", "EBITDA", "Total Debt", "Net Debt", "Interest Exp.", "Debt/EBITDA", "Net Debt/EBITDA", "EBITDA/Interest"]
    rows = []
    for r in results:
        rows.append([
            r["ticker"],
            str(r["fiscal_year"]),
            f"${r['ebitda']:,}",
            f"${r['total_debt']:,}",
            f"${r['net_debt']:,}",
            f"${r['interest_expense']:,}",
            f"{r['leverage']:.2f}x",
            f"{r['net_leverage']:.2f}x",
            f"{r['interest_coverage']:.2f}x",
        ])

    widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    def fmt_row(cells):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("=== Summary: Credit/leverage metrics across all four companies ===")
    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def main():
    tickers = fetch_margins.sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    results = []
    for ticker in TICKERS:
        cik = ciks[ticker]
        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]
        try:
            results.append(compute_credit_metrics(ticker, us_gaap))
        except RuntimeError as e:
            print(f"BLOCKED: {e}")
            print()

    print_summary_table(results)


if __name__ == "__main__":
    main()
