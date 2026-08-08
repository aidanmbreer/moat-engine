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
  lookup as its own function). CAT (added after VRT/ETN/PWR/NVT) has
  two CAT-specific debt notes carried in "debt_flags": (1) CAT doesn't
  tag a consolidated current-debt figure at all — only a
  segment-dimensioned one invisible to companyfacts — so its current
  debt falls back to the debt-footnote maturities-schedule tag
  (see fetch_roic_others.CURRENT_DEBT_TAG_CANDIDATES), flagged
  MATURITIES_SCHEDULE rather than reported clean; (2) CAT's total debt
  is consolidated (industrial parent + Cat Financial combined), same
  as the other companies' methodology, but ~85-90% of that consolidated
  debt is Cat Financial's — flagged as CAT_FINANCIAL_CONCENTRATION.
  CAT's ShortTermBorrowings (Cat Financial commercial paper funding the
  lending book, not industrial debt) is intentionally excluded from
  total debt for consistency with the other four companies, which have
  no such line either — also flagged where it applies. LMT (added
  after ORCL/CRM) similarly gets a PENSION_EXCLUDED debt_flags note:
  its net pension liability (DefinedBenefitPensionPlanLiabilitiesNoncurrent)
  is deliberately excluded from total debt, same methodology-consistency
  choice as the other companies (none reflects a pension deficit in
  total debt), with the note stating what pension-adjusted leverage
  would look like instead — informational only, doesn't change the
  confidence tier on its own (debt/EBITDA and net debt/EBITDA already
  carry the D&A-derivation "derived" flag independently for LMT).
- Interest expense: preferred tag is InterestExpense; several filers
  don't tag that for FY2025 (it went stale after an earlier year), so
  InterestExpenseNonoperating is tried next (flagged fallback when
  used — it's a real substitute-tag judgment call, since "nonoperating"
  is a narrower/different classification than plain interest expense).
  nVent doesn't tag either for FY2025 — it dropped to reporting only
  InterestIncomeExpenseNet starting FY2024 — so nVent's interest
  expense falls back to |InterestIncomeExpenseNet|, which is interest
  expense NET of interest income, not gross. That's flagged explicitly
  since it overstates nVent's coverage ratio relative to a
  gross-expense basis. CRM (added after VRT/ETN/PWR/NVT) doesn't tag
  any of the above at all — it discloses interest expense only in a
  debt footnote, not on the income statement face — under
  InterestExpenseDebt, a standard us-gaap element ("aggregate expense
  for interest on debt for the period") verified gross (not netted;
  InvestmentIncomeInterest is a separate tag) and used consistently by
  CRM for 15 years. Unlike InterestExpenseNonoperating, this is treated
  as economically equivalent to the primary InterestExpense tag, not a
  substitute with a different scope — no fallback flag when it's used,
  same treatment as the noncurrent-debt/pretax-income "verified-
  equivalent, different name" candidates elsewhere in this pipeline.
- Total debt / EBITDA (leverage), Net debt / EBITDA, and
  EBITDA / Interest expense (coverage) follow directly from the above.
  Each resolves independently against only its own inputs (debt/EBITDA:
  debt + EBITDA; net debt/EBITDA: debt + cash + EBITDA; interest
  coverage: EBITDA + interest expense) — a failure to resolve debt,
  cash, or interest expense only marks the metric(s) that actually
  depend on that input as unresolved, not all three. EBITDA (operating
  income + D&A) is a genuine shared dependency of all three, so a
  failure there still fails the whole function.

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

# (tag, flag_if_used) — flag_if_used=True reproduces the existing "used X
# instead" fallback note (InterestExpenseNonoperating is a narrower/
# different classification, a real substitute-tag judgment call).
# flag_if_used=False means verified economically equivalent to the primary
# InterestExpense tag, not a substitute with a different scope — resolves
# clean, same treatment CAT's pretax-income and ORCL's noncurrent-debt
# fallbacks already get elsewhere in this pipeline.
INTEREST_EXPENSE_TAG_CANDIDATES = [
    ("InterestExpense", False),
    ("InterestExpenseNonoperating", True),
    ("InterestExpenseDebt", False),
]


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
    for tag, flag_if_used in INTEREST_EXPENSE_TAG_CANDIDATES:
        fact = fetch_roic_others.annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            if flag_if_used:
                flags.append(
                    f"InterestExpense tag not reported for FY ended {fiscal_year_end}; used {tag} instead."
                )
            return fact["val"], tag, flags

    fact = fetch_roic_others.annual_duration_fact_at(us_gaap, "InterestIncomeExpenseNet", fiscal_year_end, required=False)
    if fact is not None:
        value = abs(fact["val"])
        flags.append(
            f"No gross interest expense tag ({' / '.join(tag for tag, _ in INTEREST_EXPENSE_TAG_CANDIDATES)}) reported for FY ended "
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


def compute_credit_metrics(ticker, us_gaap, fiscal_year_end=None):
    """If fiscal_year_end is None, targets the most recent 10-K (existing
    behavior, unchanged) — the value simply passes through to
    fetch_margins.compute_margins(), which resolves it. Given an explicit
    period end, runs the same debt/cash/D&A/interest resolution against
    that year instead — used for multi-year pulls.

    Debt/EBITDA, net debt/EBITDA, and interest coverage resolve
    independently, each against only its own inputs:
      - debt/EBITDA needs debt + EBITDA
      - net debt/EBITDA needs debt + cash + EBITDA
      - interest coverage needs EBITDA + interest expense
    EBITDA itself (operating income + D&A) is a genuine shared
    dependency of all three, so a failure to resolve operating income or
    D&A still fails the whole function — that's not collateral damage,
    it's a real shared input. But a failure to resolve debt, cash, or
    interest expense only takes down the metric(s) that actually depend
    on that input; it must not mark the other metrics unresolved too.
    """
    flags = []

    margins = fetch_margins.compute_margins(ticker, us_gaap, fiscal_year_end=fiscal_year_end)
    fiscal_year_end = margins["fiscal_year_end"]
    fiscal_year = margins["fiscal_year"]
    if margins["operating_income"] is None:
        # Operating income is a genuine shared dependency of EBITDA (and thus
        # all three credit metrics) — unlike gross margin, which compute_margins()
        # now resolves independently and can fail without taking operating
        # income down with it.
        raise RuntimeError(f"Operating income unresolved: {margins['operating_income_error']}")
    operating_income = margins["operating_income"]

    da_value, da_tag, da_flags = depreciation_and_amortization(ticker, us_gaap, fiscal_year_end)
    ebitda = operating_income + da_value

    debt = None
    total_debt = None
    debt_error = None
    debt_flags = []
    try:
        debt = fetch_roic_others.total_debt_for_roic(us_gaap, fiscal_year_end)
        total_debt = debt["total"]
        if debt.get("current_debt_note"):
            debt_flags.append(debt["current_debt_note"])
        if ticker == "CAT":
            debt_flags.append(
                "CAT_FINANCIAL_CONCENTRATION: ~85-90% of CAT's consolidated debt sits in Cat Financial (the "
                "captive finance subsidiary), not the industrial Machinery, Energy & Transportation business. "
                "Debt is reported consolidated (parent + Cat Financial combined) for consistency with how the "
                "four known companies are computed; leverage here reflects a captive lender's balance sheet "
                "funding its lending/leasing book and is not directly comparable to a pure industrial's "
                "leverage."
            )
            short_term_borrowings = fetch_roic_others.annual_instant_fact(
                us_gaap, "ShortTermBorrowings", fiscal_year_end, required=False
            )
            stb_str = f"${short_term_borrowings['val']:,}" if short_term_borrowings else "an unresolved amount"
            debt_flags.append(
                f"CAT's ShortTermBorrowings ({stb_str} as of {fiscal_year_end}) is intentionally excluded from "
                "this total debt figure — it is Cat Financial commercial paper funding the lending book, not "
                "industrial debt, and none of the four known companies' methodology includes a short-term-"
                "borrowings line either."
            )
        if ticker == "LMT":
            pension_liability = fetch_roic_others.annual_instant_fact(
                us_gaap, "DefinedBenefitPensionPlanLiabilitiesNoncurrent", fiscal_year_end, required=False
            )
            if pension_liability is not None and total_debt:
                pension_pct = pension_liability["val"] / total_debt * 100
                debt_flags.append(
                    f"PENSION_EXCLUDED: LMT's net pension liability (DefinedBenefitPensionPlanLiabilitiesNoncurrent, "
                    f"${pension_liability['val']:,} as of {fiscal_year_end}) is intentionally excluded from this "
                    "total debt figure, consistent with how the other covered companies are treated (none reflects "
                    f"a pension deficit in total debt either). Pension-adjusted leverage would be modestly higher — "
                    f"this liability is ~{pension_pct:.0f}% of total debt as reported (${total_debt:,})."
                )
    except RuntimeError as e:
        debt_error = str(e)

    cash = cash_tag = cash_note = None
    cash_flags = []
    cash_error = None
    try:
        cash, cash_tag, cash_note = cash_and_equivalents(us_gaap, fiscal_year_end)
        if cash_note:
            cash_flags.append(f"{cash_tag} used instead of CashAndCashEquivalentsAtCarryingValue — {cash_note}")
    except RuntimeError as e:
        cash_error = str(e)

    interest_value = interest_tag = None
    interest_flags = []
    interest_error = None
    try:
        interest_value, interest_tag, interest_flags = interest_expense(ticker, us_gaap, fiscal_year_end)
    except RuntimeError as e:
        interest_error = str(e)

    # Top-level "flags" preserves the original ordering (cash, then D&A,
    # then interest) regardless of the try/except structure above. debt_flags
    # is appended last — it's empty for the four known companies (and any
    # filer that isn't CAT and doesn't need the maturities-schedule
    # fallback), so this is a no-op for their output.
    flags.extend(cash_flags)
    flags.extend(da_flags)
    flags.extend(interest_flags)
    flags.extend(debt_flags)

    if debt_error is None:
        leverage = total_debt / ebitda
        leverage_error = None
    else:
        leverage = None
        leverage_error = debt_error

    if debt_error is None and cash_error is None:
        net_debt = total_debt - cash
        net_leverage = net_debt / ebitda
        net_leverage_error = None
    else:
        net_debt = None
        net_leverage = None
        net_leverage_error = " ; ".join(e for e in (debt_error, cash_error) if e)

    if interest_error is None:
        interest_coverage = ebitda / interest_value
        interest_coverage_error = None
    else:
        interest_coverage = None
        interest_coverage_error = interest_error

    print(f"Fiscal year: FY{fiscal_year} (ended {fiscal_year_end})")
    print(f"Operating income: ${operating_income:,}")
    print(f"D&A [{da_tag}]: ${da_value:,}")
    print(f"EBITDA (operating income + D&A): ${ebitda:,}")
    if debt_error is None:
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
    else:
        print(f"Total debt: UNRESOLVED — {debt_error}")
    if cash_error is None:
        print(f"Cash and equivalents [{cash_tag}]: ${cash:,}")
    else:
        print(f"Cash and equivalents: UNRESOLVED — {cash_error}")
    if net_leverage_error is None:
        print(f"Net debt (total debt - cash): ${net_debt:,}")
    else:
        print(f"Net debt (total debt - cash): UNRESOLVED — {net_leverage_error}")
    if interest_error is None:
        print(f"Interest expense [{interest_tag}]: ${interest_value:,}")
    else:
        print(f"Interest expense: UNRESOLVED — {interest_error}")
    if leverage_error is None:
        print(f"Total debt / EBITDA: {leverage:.2f}x")
    else:
        print(f"Total debt / EBITDA: UNRESOLVED — {leverage_error}")
    if net_leverage_error is None:
        print(f"Net debt / EBITDA: {net_leverage:.2f}x")
    else:
        print(f"Net debt / EBITDA: UNRESOLVED — {net_leverage_error}")
    if interest_coverage_error is None:
        print(f"EBITDA / Interest expense: {interest_coverage:.2f}x")
    else:
        print(f"EBITDA / Interest expense: UNRESOLVED — {interest_coverage_error}")
    for flag in flags:
        print(f"FLAG: {flag}")
    print()

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "fiscal_year_end": fiscal_year_end,
        "operating_income": operating_income,
        "da": da_value,
        "da_tag": da_tag,
        "ebitda": ebitda,
        "total_debt": total_debt,
        "debt_error": debt_error,
        "cash": cash,
        "cash_error": cash_error,
        "net_debt": net_debt,
        "interest_expense": interest_value,
        "interest_expense_tag": interest_tag,
        "interest_error": interest_error,
        "leverage": leverage,
        "leverage_error": leverage_error,
        "net_leverage": net_leverage,
        "net_leverage_error": net_leverage_error,
        "interest_coverage": interest_coverage,
        "interest_coverage_error": interest_coverage_error,
        "flags": flags,
        "margins_flags": margins["flags"],
        "da_flags": da_flags,
        "interest_flags": interest_flags,
        "cash_flags": cash_flags,
        "debt_flags": debt_flags,
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
