"""
Compute ROIC for Vertiv (VRT), Eaton (ETN), Quanta Services (PWR), and
nVent (NVT) for the most recent fiscal year using SEC EDGAR's
companyfacts API.

Definitions:
- NOPAT = operating income * (1 - effective tax rate), where the
  effective tax rate is pulled from the filing (income tax expense /
  pre-tax income), not assumed.
- Total debt = current portion of debt + noncurrent long-term debt +
  noncurrent operating lease liabilities + noncurrent finance lease
  liabilities, each counted exactly once. See total_debt_for_roic():
  it targets this economic concept rather than assuming every filer
  uses the same tag names. VRT tags current/noncurrent debt and
  finance leases separately. Eaton and Quanta report a single
  "long-term debt and capital lease obligations" figure that already
  includes noncurrent finance leases, so the function detects that
  and skips adding the separately tagged finance lease line to avoid
  double-counting.
- Operating income normally comes straight from the OperatingIncomeLoss
  tag. If a filer has stopped reporting that subtotal (Eaton, since
  FY2019), it's derived as Revenue - COGS - SG&A - R&D instead, and
  every other revenue/cost/expense tag found for that period is
  printed so the substitution can be checked by eye.
- Invested capital = total debt + total shareholders' equity - cash
  and equivalents. Goodwill is included (not netted out) since it
  remains inside shareholders' equity.
- ROIC = NOPAT / invested capital.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
5 requests total (one shared ticker lookup + one companyfacts call per
company), with a short pause between each.
"""

import time

import requests

import fetch_margins

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
TICKERS = ["VRT", "ETN", "PWR", "NVT"]
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


def annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=True):
    """form=10-K, fp=FY fact for a duration (income statement) concept whose
    period ends exactly on fiscal_year_end."""
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
            raise RuntimeError(f"No {tag} fact found ending {fiscal_year_end}")
        return None
    return candidates[0]


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


def tags_at_date(us_gaap, fiscal_year_end, keyword, period="instant"):
    """Diagnostic helper: every us-gaap tag containing `keyword` that has a
    form=10-K, fp=FY value as of/for fiscal_year_end. Used to surface
    candidate tags for hand-checking when an expected tag is missing,
    without guessing which one is the right substitute. period="duration"
    also matches income-statement facts with a start date, not just
    balance-sheet instants.
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


# Current-portion-of-debt tags, tried in order. None of these bundle
# noncurrent finance leases, so no double-counting risk on this side.
# The last candidate is a maturities-schedule disclosure tag, not a
# balance-sheet current-liabilities tag — added for filers (CAT) that
# don't tag a consolidated current-debt figure at all, only a
# segment-dimensioned one invisible to companyfacts. Its note is
# carried alongside the tag so callers can flag the substitution
# rather than reporting it as a plain clean match.
CURRENT_DEBT_TAG_CANDIDATES = [
    ("LongTermDebtCurrent", None),
    ("DebtCurrent", None),
    ("LongTermDebtAndCapitalLeaseObligationsCurrent", None),
    (
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
        "MATURITIES_SCHEDULE: current debt uses LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths "
        "instead of a balance-sheet current-liabilities tag — this filer doesn't tag a consolidated current-debt "
        "figure at all, only a segment-dimensioned one invisible to this pipeline. Sourced from the debt-footnote "
        "12-month maturities schedule, not the balance sheet's current-liabilities classification; the two can "
        "diverge under covenant-triggered reclassification of otherwise long-term debt to current.",
    ),
]

# Noncurrent long-term debt tags, tried in order. The second candidate is
# a combined "debt and capital(finance) lease obligations" figure used by
# filers (e.g. Eaton, Quanta) that no longer break out a pure long-term
# debt subtotal — when it's the one that matches, noncurrent finance
# leases are already inside it. The third candidate (ORCL) is a
# "notes payable" tag family instead of "long-term debt" — verified
# economically equivalent via DebtCurrent + LongTermNotesPayable matching
# DebtLongtermAndShorttermCombinedAmount exactly (same concept, different
# tag name, not bundled with finance leases), so no confidence flag on
# this substitution, same character as CAT's pretax fallback below.
NONCURRENT_DEBT_TAG_CANDIDATES = [
    ("LongTermDebtNoncurrent", False),
    ("LongTermDebtAndCapitalLeaseObligations", True),
    ("LongTermNotesPayable", False),
]

# Pre-tax income ("income before taxes") tags, tried in order. The second
# candidate is a legacy XBRL element name some filers (CAT) still use for
# the same concept — verified economically equivalent via the accounting
# identity pretax - tax + equity-method income = net income (ProfitLoss),
# not just a similarly-named tag, so no confidence flag on this
# substitution, exactly like the noncurrent-debt candidates above.
PRETAX_INCOME_TAG_CANDIDATES = [
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]


def total_debt_for_roic(us_gaap, fiscal_year_end):
    """Total debt for the ROIC calculation, targeting the economic concept
    (all interest-bearing debt + lease obligations, each counted exactly
    once) rather than assuming a fixed set of tag names. Returns the
    component facts plus the summed total so each input can be checked
    against the balance sheet.
    """
    current_debt_tag, current_debt_val, current_debt_note = None, None, None
    for tag, note in CURRENT_DEBT_TAG_CANDIDATES:
        fact = annual_instant_fact(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            current_debt_tag, current_debt_val, current_debt_note = tag, fact["val"], note
            break
    if current_debt_tag is None:
        raise RuntimeError(
            "No current-portion-of-debt tag found (tried: "
            + ", ".join(tag for tag, _ in CURRENT_DEBT_TAG_CANDIDATES)
            + ")"
        )

    noncurrent_debt_tag, noncurrent_debt_val, bundles_finance_lease = None, None, False
    for tag, bundles in NONCURRENT_DEBT_TAG_CANDIDATES:
        fact = annual_instant_fact(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            noncurrent_debt_tag, noncurrent_debt_val, bundles_finance_lease = tag, fact["val"], bundles
            break
    if noncurrent_debt_tag is None:
        raise RuntimeError(
            "No noncurrent long-term debt tag found (tried: "
            + ", ".join(tag for tag, _ in NONCURRENT_DEBT_TAG_CANDIDATES)
            + ")"
        )

    operating_lease_liability = annual_instant_fact(us_gaap, "OperatingLeaseLiabilityNoncurrent", fiscal_year_end)
    operating_lease_liability_val = operating_lease_liability["val"]

    if bundles_finance_lease:
        finance_lease_liability_val = 0
        finance_lease_note = f"already included in {noncurrent_debt_tag} — not added separately"
    else:
        finance_lease_liability = annual_instant_fact(
            us_gaap, "FinanceLeaseLiabilityNoncurrent", fiscal_year_end, required=False
        )
        finance_lease_liability_val = finance_lease_liability["val"] if finance_lease_liability else 0
        finance_lease_note = None

    return {
        "current_debt_tag": current_debt_tag,
        "current_debt": current_debt_val,
        "current_debt_note": current_debt_note,
        "noncurrent_debt_tag": noncurrent_debt_tag,
        "noncurrent_debt": noncurrent_debt_val,
        "operating_lease_liability_noncurrent": operating_lease_liability_val,
        "finance_lease_liability_noncurrent": finance_lease_liability_val,
        "finance_lease_note": finance_lease_note,
        "total": (
            current_debt_val
            + noncurrent_debt_val
            + operating_lease_liability_val
            + finance_lease_liability_val
        ),
    }


# Tags used to derive an operating income proxy when OperatingIncomeLoss
# isn't reported for the current fiscal year. A brief note on why each
# nearby line item is excluded is printed alongside the derivation.
OPERATING_INCOME_PROXY_TAGS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "cost_of_revenue": "CostOfRevenue",
    "sga": "SellingGeneralAndAdministrativeExpense",
    "rd": "ResearchAndDevelopmentExpense",
}

# (keyword to match in tag name, note to print) — first match wins.
EXCLUSION_NOTES = [
    ("Tax", "tax — below operating income"),
    ("Interest", "interest/financing — below operating income"),
    ("DeferredFinanceCosts", "debt issuance cost — financing, not operating"),
    ("DebtInstrument", "debt instrument disclosure — not a P&L operating line"),
    ("Nonoperating", "explicitly tagged non-operating"),
    ("ShortTermLeaseCost", "lease expense already embedded in COGS/SG&A — would double-count if added"),
    ("VariableLeaseCost", "lease expense already embedded in COGS/SG&A — would double-count if added"),
    ("OperatingLeaseCost", "lease expense already embedded in COGS/SG&A — would double-count if added"),
    ("LeaseCost", "lease expense already embedded in COGS/SG&A — would double-count if added"),
    ("Restructuring", "restructuring — judgment call; some analysts treat this as operating, others as one-time. Not included here."),
    ("Pension", "pension/OPEB — typically has both an operating service-cost piece and a non-operating piece; can't split from this tag alone. Not included here."),
    ("Amortization", "forward-looking amortization schedule disclosure, not FY actual expense"),
    ("PrepaidExpense", "balance sheet item, not a P&L line"),
    ("ContractWithCustomerLiability", "balance sheet contract liability, not a P&L line"),
    ("ShareBasedCompensation", "disclosure of unrecognized future cost, not FY actual expense"),
]


def note_for_tag(tag):
    for keyword, note in EXCLUSION_NOTES:
        if keyword.lower() in tag.lower():
            return note
    return "not part of Revenue - COGS - SG&A - R&D; review if it belongs in operating income"


def operating_income_via_proxy(ticker, us_gaap, fiscal_year_end):
    facts = {}
    for name, tag in OPERATING_INCOME_PROXY_TAGS.items():
        fact = annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
        if fact is None:
            return None
        facts[name] = fact

    value = facts["revenue"]["val"] - facts["cost_of_revenue"]["val"] - facts["sga"]["val"] - facts["rd"]["val"]

    other_candidates = {}
    for keyword in ("Revenue", "Cost", "Expense", "Income"):
        other_candidates.update(tags_at_date(us_gaap, fiscal_year_end, keyword))
    for tag in OPERATING_INCOME_PROXY_TAGS.values():
        other_candidates.pop(tag, None)

    print(f"--- {ticker}: OperatingIncomeLoss not tagged for FY ended {fiscal_year_end}; deriving a proxy ---")
    print(
        f"Operating income = Revenue - COGS - SG&A - R&D "
        f"= ${facts['revenue']['val']:,} - ${facts['cost_of_revenue']['val']:,} "
        f"- ${facts['sga']['val']:,} - ${facts['rd']['val']:,} = ${value:,}"
    )
    print(f"Other revenue/cost/expense/income tags found as of {fiscal_year_end}, NOT included above:")
    for tag, val in sorted(other_candidates.items()):
        print(f"    {tag}: ${val:,}  ({note_for_tag(tag)})")
    print()

    return value


def compute_roic(ticker, us_gaap, fiscal_year_end=None):
    """If fiscal_year_end is None, targets the most recent 10-K (existing
    behavior, unchanged). If given an explicit period end, runs the exact
    same debt/equity/cash/tax-rate resolution and derivation logic against
    that year instead — used for multi-year pulls. Every existing caller
    passes no fiscal_year_end and is unaffected.

    The operating-income check is restated (not behaviorally changed) to
    work for either case: does OperatingIncomeLoss have a fact for exactly
    this period end? That's equivalent to the original "most recent" logic
    (comparing the latest tagged OperatingIncomeLoss fact's fiscal year to
    the reference fiscal year) but also works when the reference year isn't
    the most recent one.
    """
    flags = []

    # IncomeTaxExpenseBenefit is reported every year a 10-K is filed, so its
    # fiscal year is a reliable reference point. Uses
    # fetch_margins.most_recent_fact_among_candidates() (not the plain
    # most_recent_annual_duration_fact()) even though there's currently
    # only one candidate tag here — same fix applied uniformly across the
    # shared "most recent" resolution path, so this anchor can't repeat
    # the NVDA-style stale-tag bug if a second candidate tag is ever
    # added here.
    if fiscal_year_end is None:
        tax_expense_fact, _ = fetch_margins.most_recent_fact_among_candidates(us_gaap, ["IncomeTaxExpenseBenefit"])
        if tax_expense_fact is None:
            raise RuntimeError("No IncomeTaxExpenseBenefit tag found")
    else:
        tax_expense_fact = annual_duration_fact_at(us_gaap, "IncomeTaxExpenseBenefit", fiscal_year_end)
    reference_fiscal_year_end = tax_expense_fact["end"]
    reference_fy = tax_expense_fact["fy"]

    operating_income_fact = annual_duration_fact_at(
        us_gaap, "OperatingIncomeLoss", reference_fiscal_year_end, required=False
    )
    if operating_income_fact is None:
        proxy_value = operating_income_via_proxy(ticker, us_gaap, reference_fiscal_year_end)
        if proxy_value is None:
            candidates = tags_at_date(us_gaap, reference_fiscal_year_end, "Revenue")
            candidates.update(tags_at_date(us_gaap, reference_fiscal_year_end, "Cost"))
            candidates.update(tags_at_date(us_gaap, reference_fiscal_year_end, "Expense"))
            candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
            raise RuntimeError(
                f"OperatingIncomeLoss not tagged for FY ended {reference_fiscal_year_end}, and the "
                f"Revenue - COGS - SG&A - R&D proxy tags are also incomplete. Revenue/cost/expense tags "
                f"available as of {reference_fiscal_year_end} for hand-reconstruction:\n{candidate_lines}"
            )
        operating_income = proxy_value
        fiscal_year_end = reference_fiscal_year_end
        fiscal_year = reference_fy
        flags.append("Operating income derived via Revenue - COGS - SG&A - R&D proxy, not a tagged OperatingIncomeLoss — see derivation above.")
    else:
        operating_income = operating_income_fact["val"]
        fiscal_year_end = operating_income_fact["end"]
        fiscal_year = operating_income_fact["fy"]

    # fiscal_year_end is a concrete date by this point regardless of what the
    # function's fiscal_year_end argument was, so this always targets the
    # exact period already settled on above.
    pretax_income, pretax_income_tag = None, None
    for tag in PRETAX_INCOME_TAG_CANDIDATES:
        fact = annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            pretax_income, pretax_income_tag = fact["val"], tag
            break

    if pretax_income is None:
        # ORCL doesn't tag a consolidated pre-tax income figure at all —
        # only a domestic/foreign geographic split. Verified equivalent via
        # the accounting identity (domestic + foreign - tax expense = net
        # income), but it's a derived sum of two tags, not a substitute
        # tag, so it's flagged "derived" rather than reported clean.
        domestic_fact = annual_duration_fact_at(
            us_gaap, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic", fiscal_year_end, required=False
        )
        foreign_fact = annual_duration_fact_at(
            us_gaap, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign", fiscal_year_end, required=False
        )
        if domestic_fact is not None and foreign_fact is not None:
            pretax_income = domestic_fact["val"] + foreign_fact["val"]
            pretax_income_tag = (
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic + "
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign (derived)"
            )
            flags.append(
                "No consolidated pre-tax income tag found; pre-tax income derived as "
                f"IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic (${domestic_fact['val']:,}) + "
                f"IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign (${foreign_fact['val']:,}) "
                f"= ${pretax_income:,} — see derivation above."
            )

    if pretax_income is None:
        raise RuntimeError(
            f"No pre-tax income tag found for FY ended {fiscal_year_end} (tried: "
            + ", ".join(PRETAX_INCOME_TAG_CANDIDATES)
            + ", and the Domestic+Foreign derivation)"
        )

    try:
        debt = total_debt_for_roic(us_gaap, fiscal_year_end)
    except RuntimeError as e:
        candidates = tags_at_date(us_gaap, fiscal_year_end, "Debt")
        candidates.update(tags_at_date(us_gaap, fiscal_year_end, "Lease"))
        candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
        raise RuntimeError(
            f"total_debt_for_roic failed ({e}). Debt/lease tags available as of {fiscal_year_end} "
            f"for hand-checking:\n{candidate_lines}"
        )
    for component in ("current_debt", "noncurrent_debt", "operating_lease_liability_noncurrent", "finance_lease_liability_noncurrent"):
        if debt[component] == 0:
            flags.append(f"{component} is $0 as of {fiscal_year_end} — hand-check the balance sheet.")
    if debt.get("current_debt_note"):
        flags.append(debt["current_debt_note"])

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
        f"(current debt [{debt['current_debt_tag']}] ${debt['current_debt']:,} "
        f"+ noncurrent debt [{debt['noncurrent_debt_tag']}] ${debt['noncurrent_debt']:,} "
        f"+ operating lease liability (noncurrent) ${debt['operating_lease_liability_noncurrent']:,} "
        f"+ finance lease liability (noncurrent) ${debt['finance_lease_liability_noncurrent']:,}"
        + (f" [{debt['finance_lease_note']}]" if debt["finance_lease_note"] else "")
        + ")"
    )
    print(f"Total equity: ${total_equity:,}")
    print(f"Cash and equivalents: ${cash:,}")
    print(f"Invested capital: ${invested_capital:,}")
    print(f"ROIC: {roic:.2%}")
    for flag in flags:
        print(f"FLAG: {flag}")
    print()

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "fiscal_year_end": fiscal_year_end,
        "operating_income": operating_income,
        "effective_tax_rate": effective_tax_rate,
        "pretax_income_tag": pretax_income_tag,
        "nopat": nopat,
        "total_debt": total_debt,
        "total_equity": total_equity,
        "cash": cash,
        "invested_capital": invested_capital,
        "roic": roic,
        "flags": flags,
    }


def print_summary_table(results):
    if not results:
        print("No companies computed successfully.")
        return

    headers = ["Ticker", "FY", "Op. Income", "Tax Rate", "NOPAT", "Total Debt", "Total Equity", "Cash", "Invested Capital", "ROIC"]
    rows = []
    for r in results:
        rows.append([
            r["ticker"],
            str(r["fiscal_year"]),
            f"${r['operating_income']:,}",
            f"{r['effective_tax_rate']:.2%}",
            f"${r['nopat']:,.0f}",
            f"${r['total_debt']:,}",
            f"${r['total_equity']:,}",
            f"${r['cash']:,}",
            f"${r['invested_capital']:,}",
            f"{r['roic']:.2%}",
        ])

    widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    def fmt_row(cells):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("=== Summary: ROIC across all four companies ===")
    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def main():
    # 1. Look up each company's CIK from SEC's ticker directory (one shared call)
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    results = []
    for ticker in TICKERS:
        cik = ciks[ticker]
        facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]
        try:
            results.append(compute_roic(ticker, us_gaap))
        except RuntimeError as e:
            print(f"=== {ticker} ===")
            print(f"BLOCKED: {e}")
            print()

    print_summary_table(results)


if __name__ == "__main__":
    main()
