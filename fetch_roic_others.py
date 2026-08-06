"""
Compute ROIC for Eaton (ETN), Quanta Services (PWR), and nVent (NVT) for
the most recent fiscal year using SEC EDGAR's companyfacts API.

Same definitions as fetch_vrt_roic.py, applied unchanged:
- NOPAT = operating income * (1 - effective tax rate), where the
  effective tax rate is pulled from the filing (income tax expense /
  pre-tax income), not assumed.
- Total debt = current portion of long-term debt + long-term debt
  (net) + long-term lease liabilities (finance and operating). See
  total_debt_for_roic() below, copied verbatim from fetch_vrt_roic.py.
- Invested capital = total debt + total shareholders' equity - cash
  and equivalents. Goodwill is included (not netted out) since it
  remains inside shareholders' equity.
- ROIC = NOPAT / invested capital.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
4 requests total (one shared ticker lookup + one companyfacts call per
company), with a short pause between each.
"""

import time

import requests

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
TICKERS = ["ETN", "PWR", "NVT"]
MAX_CALLS = 1 + len(TICKERS)  # ticker lookup + one companyfacts call per company
calls_made = 0


def sec_get(url):
    global calls_made
    if calls_made >= MAX_CALLS:
        raise RuntimeError("Refusing to exceed hard cap of API calls")
    calls_made += 1
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.2)  # stay well under the 10 req/sec limit
    return resp


def most_recent_annual_duration_fact(us_gaap, tag):
    """Latest form=10-K, fp=FY fact for a duration (income statement) concept."""
    facts = us_gaap[tag]["units"]["USD"]
    annual_facts = [f for f in facts if f["form"] == "10-K" and f["fp"] == "FY"]
    return max(annual_facts, key=lambda f: f["end"])


def annual_instant_fact(us_gaap, tag, fiscal_year_end, required=True):
    """form=10-K, fp=FY fact for an instant (balance sheet) concept as of a date.

    Returns None if the tag isn't reported and required=False, since some
    filers omit a line item entirely (e.g. no separately tagged finance
    lease liability) rather than reporting it as zero.
    """
    if tag not in us_gaap:
        if required:
            raise RuntimeError(f"Tag {tag} not present in company facts")
        return None
    facts = us_gaap[tag]["units"]["USD"]
    candidates = [
        f
        for f in facts
        if f["form"] == "10-K" and f["fp"] == "FY" and f["end"] == fiscal_year_end
    ]
    if not candidates:
        if required:
            raise RuntimeError(f"No {tag} fact found as of {fiscal_year_end}")
        return None
    return candidates[0]


def total_debt_for_roic(us_gaap, fiscal_year_end):
    """Total debt for the ROIC calculation: current portion of long-term
    debt + long-term debt (net) + long-term lease liabilities (finance
    and operating). Returns the component facts plus the summed total so
    each input can be checked against the balance sheet.

    Finance lease liabilities are only added if the filer reports them
    under a separately tagged noncurrent finance lease line; some filers
    fold an immaterial finance lease balance into long-term debt or other
    liabilities instead of tagging it separately.
    """
    current_lt_debt = annual_instant_fact(us_gaap, "LongTermDebtCurrent", fiscal_year_end)
    noncurrent_lt_debt = annual_instant_fact(us_gaap, "LongTermDebtNoncurrent", fiscal_year_end)
    operating_lease_liability = annual_instant_fact(
        us_gaap, "OperatingLeaseLiabilityNoncurrent", fiscal_year_end
    )
    finance_lease_liability = annual_instant_fact(
        us_gaap, "FinanceLeaseLiabilityNoncurrent", fiscal_year_end, required=False
    )

    current_lt_debt_val = current_lt_debt["val"]
    noncurrent_lt_debt_val = noncurrent_lt_debt["val"]
    operating_lease_liability_val = operating_lease_liability["val"]
    finance_lease_liability_val = finance_lease_liability["val"] if finance_lease_liability else 0

    return {
        "current_long_term_debt": current_lt_debt_val,
        "long_term_debt_noncurrent": noncurrent_lt_debt_val,
        "operating_lease_liability_noncurrent": operating_lease_liability_val,
        "finance_lease_liability_noncurrent": finance_lease_liability_val,
        "total": (
            current_lt_debt_val
            + noncurrent_lt_debt_val
            + operating_lease_liability_val
            + finance_lease_liability_val
        ),
    }


def tags_at_date(us_gaap, fiscal_year_end, keyword):
    """Diagnostic helper: every us-gaap tag containing `keyword` that has a
    form=10-K, fp=FY value as of fiscal_year_end. Used to surface candidate
    tags for hand-checking when an expected tag is missing, without guessing
    which one is the right substitute.
    """
    matches = {}
    for tag, obj in us_gaap.items():
        if keyword.lower() not in tag.lower():
            continue
        for facts in obj["units"].values():
            for f in facts:
                if f.get("form") == "10-K" and f.get("fp") == "FY" and f.get("end") == fiscal_year_end:
                    matches[tag] = f["val"]
                    break
    return matches


def compute_roic(ticker, us_gaap):
    flags = []

    # IncomeTaxExpenseBenefit is reported every year a 10-K is filed, so its
    # fiscal year is a reliable "most recent FY" reference point.
    tax_expense_fact = most_recent_annual_duration_fact(us_gaap, "IncomeTaxExpenseBenefit")
    reference_fiscal_year_end = tax_expense_fact["end"]
    reference_fy = tax_expense_fact["fy"]

    operating_income_fact = most_recent_annual_duration_fact(us_gaap, "OperatingIncomeLoss")
    if operating_income_fact["fy"] != reference_fy:
        candidates = tags_at_date(us_gaap, reference_fiscal_year_end, "Revenue")
        candidates.update(tags_at_date(us_gaap, reference_fiscal_year_end, "Cost"))
        candidates.update(tags_at_date(us_gaap, reference_fiscal_year_end, "Expense"))
        candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
        raise RuntimeError(
            f"OperatingIncomeLoss is stale: latest tagged value is FY{operating_income_fact['fy']} "
            f"(ended {operating_income_fact['end']}), but the filer's most recent 10-K is FY{reference_fy} "
            f"(ended {reference_fiscal_year_end}). This filer has likely stopped reporting a distinct "
            "operating income subtotal in XBRL. Revenue/cost/expense tags available as of "
            f"{reference_fiscal_year_end} for hand-reconstruction:\n{candidate_lines}"
        )

    fiscal_year_end = operating_income_fact["end"]
    fiscal_year = operating_income_fact["fy"]

    pretax_income_fact = most_recent_annual_duration_fact(
        us_gaap, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    )

    try:
        debt = total_debt_for_roic(us_gaap, fiscal_year_end)
    except RuntimeError as e:
        candidates = tags_at_date(us_gaap, fiscal_year_end, "Debt")
        candidates.update(tags_at_date(us_gaap, fiscal_year_end, "Lease"))
        candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
        raise RuntimeError(
            f"total_debt_for_roic failed ({e}). Debt/lease tags available as of {fiscal_year_end} "
            f"for hand-checking (note: some filers report a combined debt+finance-lease figure, which "
            f"would double-count against a separately tagged finance lease liability):\n{candidate_lines}"
        )
    for component in ("current_long_term_debt", "long_term_debt_noncurrent", "operating_lease_liability_noncurrent", "finance_lease_liability_noncurrent"):
        if debt[component] == 0:
            flags.append(f"{component} is $0 as of {fiscal_year_end} — hand-check the balance sheet.")

    equity_fact = annual_instant_fact(us_gaap, "StockholdersEquity", fiscal_year_end, required=False)
    if equity_fact is None:
        equity_fact = annual_instant_fact(
            us_gaap, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", fiscal_year_end
        )
        flags.append(
            "StockholdersEquity tag not reported; used StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest instead — hand-check against the balance sheet."
        )

    cash_fact = annual_instant_fact(us_gaap, "CashAndCashEquivalentsAtCarryingValue", fiscal_year_end, required=False)
    if cash_fact is None:
        cash_fact = annual_instant_fact(
            us_gaap, "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", fiscal_year_end
        )
        flags.append(
            "CashAndCashEquivalentsAtCarryingValue tag not reported; used CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents instead, which may include a small restricted-cash component — hand-check against the balance sheet."
        )

    operating_income = operating_income_fact["val"]
    pretax_income = pretax_income_fact["val"]
    tax_expense = tax_expense_fact["val"]
    effective_tax_rate = tax_expense / pretax_income

    nopat = operating_income * (1 - effective_tax_rate)

    total_debt = debt["total"]
    total_equity = equity_fact["val"]
    cash = cash_fact["val"]
    invested_capital = total_debt + total_equity - cash

    roic = nopat / invested_capital

    print(f"=== {ticker} ===")
    print(f"Fiscal year: FY{fiscal_year} (ended {fiscal_year_end})")
    print(f"Operating income: ${operating_income:,}")
    print(f"Effective tax rate: {effective_tax_rate:.2%} (tax expense ${tax_expense:,} / pre-tax income ${pretax_income:,})")
    print(f"NOPAT: ${nopat:,.0f}")
    print(
        "Total debt: "
        f"${total_debt:,} "
        f"(current LT debt ${debt['current_long_term_debt']:,} "
        f"+ noncurrent LT debt ${debt['long_term_debt_noncurrent']:,} "
        f"+ operating lease liability (noncurrent) ${debt['operating_lease_liability_noncurrent']:,} "
        f"+ finance lease liability (noncurrent) ${debt['finance_lease_liability_noncurrent']:,})"
    )
    print(f"Total equity: ${total_equity:,}")
    print(f"Cash and equivalents: ${cash:,}")
    print(f"Invested capital: ${invested_capital:,}")
    print(f"ROIC: {roic:.2%}")
    for flag in flags:
        print(f"FLAG: {flag}")
    print()


def main():
    # 1. Look up each company's CIK from SEC's ticker directory (one shared call)
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    for ticker in TICKERS:
        cik = ciks[ticker]
        facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]
        try:
            compute_roic(ticker, us_gaap)
        except RuntimeError as e:
            print(f"=== {ticker} ===")
            print(f"BLOCKED: {e}")
            print()


if __name__ == "__main__":
    main()
