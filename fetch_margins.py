"""
Compute gross margin and operating margin for Vertiv (VRT), Eaton (ETN),
Quanta Services (PWR), and nVent (NVT) for the most recent fiscal year
using SEC EDGAR's companyfacts API.

Definitions:
- Gross margin = (Revenue - Cost of revenue) / Revenue. Cost of revenue
  is read from whichever of CostOfRevenue / CostOfGoodsAndServicesSold
  the filer actually tags — confirmed by inspecting each company's
  companyfacts response, they're mutually exclusive (Eaton tags
  CostOfRevenue; Vertiv, Quanta, and nVent tag
  CostOfGoodsAndServicesSold), so trying both in order is unambiguous
  rather than a guess.
- Operating margin = Operating income / Revenue. Operating income
  normally comes straight from the OperatingIncomeLoss tag, matched to
  the same fiscal year as revenue. Eaton has not tagged that subtotal
  since FY2019, so its operating income is derived as
  Revenue - COGS - SG&A - R&D instead — the same proxy validated for
  Eaton's ROIC in fetch_roic_others.py — and every other
  revenue/cost/expense tag found for that period is printed so the
  substitution can be checked by eye.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
5 requests total (one shared ticker lookup + one companyfacts call per
company), with a short pause between each.
"""

import time

import requests

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
TICKERS = ["VRT", "ETN", "PWR", "NVT"]
MAX_CALLS = 1 + len(TICKERS)  # ticker lookup + one companyfacts call per company
calls_made = 0

REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
COST_OF_REVENUE_TAG_CANDIDATES = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]


def recent_fiscal_year_ends(us_gaap, count=5):
    """The `count` most recent distinct fiscal year ends (form=10-K,
    fp=FY) found across ALL revenue tag candidates (unioned, not just
    the first one present) — a company can switch which revenue tag it
    uses partway through the window, and period discovery shouldn't
    silently lose the years reported under the other tag. Pure period
    discovery, doesn't touch any calculation; used to find which years to
    run compute_margins()/compute_roic()/compute_credit_metrics() against
    for a multi-year pull."""
    ends = set()
    for tag in REVENUE_TAGS:
        if tag in us_gaap:
            facts = us_gaap[tag]["units"]["USD"]
            ends.update(f["end"] for f in facts if f["form"] == "10-K" and f["fp"] == "FY")
    if not ends:
        raise RuntimeError(f"No revenue tag found (tried: {', '.join(REVENUE_TAGS)}) to discover fiscal year ends")
    return sorted(ends, reverse=True)[:count]


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


def most_recent_fact_among_candidates(us_gaap, candidate_tags):
    """Among `candidate_tags` (in preference order), find the one whose
    own most-recent form=10-K, fp=FY fact is genuinely the most recent
    across ALL candidates — not just the first candidate that happens to
    exist anywhere in the filer's history.

    This is the fix for a real bug: a filer can switch which tag it uses
    for a concept partway through its history (NVDA moved off
    RevenueFromContractWithCustomerExcludingAssessedTax, which stops
    having data after FY2022, onto a plain Revenues tag that carries
    data through the current filing). The old logic picked whichever
    candidate merely existed in us_gaap, found *that* tag's own most
    recent fact, and stopped — for NVDA that silently returned FY2022
    data under a technically-matched but stale tag. This function
    instead resolves every candidate's own most-recent fact first, then
    picks whichever one is genuinely most recent.

    Ties (the same most-recent end date under multiple candidates) keep
    the existing preference order — the first candidate to reach that
    end date wins, later ones don't displace it.

    Works identically to most_recent_annual_duration_fact() for a
    single-tag, single-candidate list, so this is a safe drop-in even
    where there's currently no alternate tag name to fall back to.

    Returns (fact, tag) for the winning candidate, or (None, None) if no
    candidate has any form=10-K, fp=FY data at all.
    """
    best_fact, best_tag = None, None
    for tag in candidate_tags:
        if tag not in us_gaap:
            continue
        try:
            fact = most_recent_annual_duration_fact(us_gaap, tag)
        except ValueError:
            continue  # tag exists but has no form=10-K, fp=FY facts
        if best_fact is None or fact["end"] > best_fact["end"]:
            best_fact, best_tag = fact, tag
    return best_fact, best_tag


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


def tags_at_date(us_gaap, fiscal_year_end, keyword):
    """Diagnostic helper: every us-gaap tag containing `keyword` that has a
    form=10-K, fp=FY value for a period ending fiscal_year_end. Used to
    surface candidate tags for hand-checking when an expected tag is
    missing, without guessing which one is the right substitute.
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


# Tags used to derive an operating income proxy when OperatingIncomeLoss
# isn't reported for the current fiscal year. Same proxy validated for
# Eaton's ROIC in fetch_roic_others.py.
OPERATING_INCOME_PROXY_TAGS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "cost_of_revenue": "CostOfRevenue",
    "sga": "SellingGeneralAndAdministrativeExpense",
    "rd": "ResearchAndDevelopmentExpense",
}

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

    print(f"  OperatingIncomeLoss not tagged for FY ended {fiscal_year_end}; deriving a proxy:")
    print(
        f"    Operating income = Revenue - COGS - SG&A - R&D "
        f"= ${facts['revenue']['val']:,} - ${facts['cost_of_revenue']['val']:,} "
        f"- ${facts['sga']['val']:,} - ${facts['rd']['val']:,} = ${value:,}"
    )
    print(f"    Other revenue/cost/expense/income tags found as of {fiscal_year_end}, NOT included above:")
    for tag, val in sorted(other_candidates.items()):
        print(f"      {tag}: ${val:,}  ({note_for_tag(tag)})")

    return value


def compute_margins(ticker, us_gaap, fiscal_year_end=None):
    """If fiscal_year_end is None, targets the most recent 10-K (existing
    behavior, unchanged). If given an explicit period end, runs the exact
    same tag-resolution and derivation logic against that year instead —
    used for multi-year pulls. Every existing caller passes no
    fiscal_year_end and is unaffected.

    Gross margin and operating margin resolve independently. Revenue is a
    genuine shared dependency of both (gross margin's numerator and
    operating margin's denominator both need it), so a failure to resolve
    revenue still fails the whole function. But cost-of-revenue (gross
    margin's other input) and operating income (operating margin's other
    input) don't depend on each other — a filer that doesn't tag a single
    cost-of-revenue figure at all (e.g. ORCL, which presents no
    gross-profit subtotal on its income statement) can still have a
    natively-tagged OperatingIncomeLoss, and operating margin must resolve
    on that basis rather than being dragged down as collateral damage from
    the missing cost-of-revenue tag.
    """
    print(f"=== {ticker} ===")
    flags = []

    revenue_fact, revenue_tag = None, None
    if fiscal_year_end is None:
        revenue_fact, revenue_tag = most_recent_fact_among_candidates(us_gaap, REVENUE_TAGS)
        if revenue_fact is None:
            raise RuntimeError(f"No revenue tag found (tried: {', '.join(REVENUE_TAGS)})")
    else:
        for tag in REVENUE_TAGS:
            fact = annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
            if fact is not None:
                revenue_fact, revenue_tag = fact, tag
                break
        if revenue_fact is None:
            raise RuntimeError(
                f"No revenue tag found ending {fiscal_year_end} (tried: {', '.join(REVENUE_TAGS)})"
            )

    fiscal_year_end = revenue_fact["end"]
    fiscal_year = revenue_fact["fy"]
    revenue = revenue_fact["val"]

    # --- Cost of revenue / gross margin (independent of operating margin) ---
    cost_of_revenue_tag, cost_of_revenue_val = None, None
    cost_of_revenue_error = None
    for tag in COST_OF_REVENUE_TAG_CANDIDATES:
        fact = annual_duration_fact_at(us_gaap, tag, fiscal_year_end, required=False)
        if fact is not None:
            cost_of_revenue_tag, cost_of_revenue_val = tag, fact["val"]
            break
    if cost_of_revenue_tag is None:
        candidates = tags_at_date(us_gaap, fiscal_year_end, "Cost")
        candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
        cost_of_revenue_error = (
            f"No cost-of-revenue tag found (tried: {', '.join(COST_OF_REVENUE_TAG_CANDIDATES)}). "
            f"'Cost'-containing tags available as of {fiscal_year_end}:\n{candidate_lines}"
        )

    if cost_of_revenue_error is None:
        gross_profit = revenue - cost_of_revenue_val
        gross_margin = gross_profit / revenue
    else:
        gross_profit = None
        gross_margin = None

    # --- Operating income / operating margin (independent of gross margin) ---
    operating_income_fact = annual_duration_fact_at(us_gaap, "OperatingIncomeLoss", fiscal_year_end, required=False)
    operating_income_error = None
    if operating_income_fact is not None:
        operating_income = operating_income_fact["val"]
    else:
        proxy_value = operating_income_via_proxy(ticker, us_gaap, fiscal_year_end)
        if proxy_value is None:
            candidates = tags_at_date(us_gaap, fiscal_year_end, "Revenue")
            candidates.update(tags_at_date(us_gaap, fiscal_year_end, "Cost"))
            candidates.update(tags_at_date(us_gaap, fiscal_year_end, "Expense"))
            candidate_lines = "\n".join(f"    {tag}: ${val:,}" for tag, val in sorted(candidates.items()))
            operating_income = None
            operating_income_error = (
                f"OperatingIncomeLoss not tagged for FY ended {fiscal_year_end}, and the "
                f"Revenue - COGS - SG&A - R&D proxy tags are also incomplete. "
                f"Revenue/cost/expense tags available as of {fiscal_year_end} for hand-reconstruction:\n{candidate_lines}"
            )
        else:
            operating_income = proxy_value
            flags.append("Operating income derived via Revenue - COGS - SG&A - R&D proxy, not a tagged OperatingIncomeLoss — see derivation above.")

    if operating_income_error is None:
        operating_margin = operating_income / revenue
    else:
        operating_margin = None

    print(f"Fiscal year: FY{fiscal_year} (ended {fiscal_year_end})")
    print(f"Revenue [{revenue_tag}]: ${revenue:,}")
    if cost_of_revenue_error is None:
        print(f"Cost of revenue [{cost_of_revenue_tag}]: ${cost_of_revenue_val:,}")
        print(f"Gross profit: ${gross_profit:,}")
        print(f"Gross margin: {gross_margin:.2%}")
    else:
        print(f"Cost of revenue: UNRESOLVED — {cost_of_revenue_error}")
        print("Gross profit: UNRESOLVED")
        print("Gross margin: UNRESOLVED")
    if operating_income_error is None:
        print(f"Operating income: ${operating_income:,}")
        print(f"Operating margin: {operating_margin:.2%}")
    else:
        print(f"Operating income: UNRESOLVED — {operating_income_error}")
        print("Operating margin: UNRESOLVED")
    for flag in flags:
        print(f"FLAG: {flag}")
    print()

    return {
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "fiscal_year_end": fiscal_year_end,
        "revenue": revenue,
        "revenue_tag": revenue_tag,
        "cost_of_revenue": cost_of_revenue_val,
        "cost_of_revenue_tag": cost_of_revenue_tag,
        "cost_of_revenue_error": cost_of_revenue_error,
        "gross_profit": gross_profit,
        "gross_margin": gross_margin,
        "operating_income": operating_income,
        "operating_income_error": operating_income_error,
        "operating_margin": operating_margin,
        "flags": flags,
    }


def print_summary_table(results):
    if not results:
        print("No companies computed successfully.")
        return

    headers = ["Ticker", "FY", "Revenue", "Gross Margin", "Operating Margin"]
    rows = []
    for r in results:
        rows.append([
            r["ticker"],
            str(r["fiscal_year"]),
            f"${r['revenue']:,}",
            f"{r['gross_margin']:.2%}",
            f"{r['operating_margin']:.2%}",
        ])

    widths = [max(len(h), max(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    def fmt_row(cells):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("=== Summary: Gross margin and operating margin across all four companies ===")
    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def main():
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
            results.append(compute_margins(ticker, us_gaap))
        except RuntimeError as e:
            print(f"BLOCKED: {e}")
            print()

    print_summary_table(results)


if __name__ == "__main__":
    main()
