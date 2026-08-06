"""
Compute Vertiv's (VRT) operating margin for the most recent fiscal year
using SEC EDGAR's companyfacts API.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
2 requests total, with a short pause between each, so no additional
loop cap is needed.
"""

import time

import requests

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
MAX_CALLS = 2  # hard cap: ticker lookup, companyfacts lookup
calls_made = 0

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
]


def sec_get(url):
    global calls_made
    if calls_made >= MAX_CALLS:
        raise RuntimeError("Refusing to exceed hard cap of API calls")
    calls_made += 1
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.2)  # stay well under the 10 req/sec limit
    return resp


def most_recent_annual_fact(units_usd):
    """Return the USD fact with form=10-K, fp=FY, and the latest fiscal year end."""
    annual_facts = [
        fact for fact in units_usd if fact["form"] == "10-K" and fact["fp"] == "FY"
    ]
    return max(annual_facts, key=lambda fact: fact["end"])


def main():
    # 1. Look up VRT's CIK number from SEC's ticker directory
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    match = next(v for v in tickers.values() if v["ticker"] == "VRT")
    cik = str(match["cik_str"]).zfill(10)

    # 2. Pull VRT's XBRL company facts
    facts = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
    us_gaap = facts["facts"]["us-gaap"]

    operating_income_fact = most_recent_annual_fact(
        us_gaap["OperatingIncomeLoss"]["units"]["USD"]
    )
    fiscal_year_end = operating_income_fact["end"]

    # Match revenue to the same fiscal period, trying tags in order of preference
    revenue_fact = None
    for tag in REVENUE_TAGS:
        if tag not in us_gaap:
            continue
        candidates = [
            fact
            for fact in us_gaap[tag]["units"]["USD"]
            if fact["form"] == "10-K" and fact["fp"] == "FY" and fact["end"] == fiscal_year_end
        ]
        if candidates:
            revenue_fact = candidates[0]
            break

    if revenue_fact is None:
        raise RuntimeError(f"No matching revenue fact found for fiscal year end {fiscal_year_end}")

    operating_income = operating_income_fact["val"]
    revenue = revenue_fact["val"]
    operating_margin = operating_income / revenue

    print(f"Fiscal year: FY{operating_income_fact['fy']} (ended {fiscal_year_end})")
    print(f"Operating income: ${operating_income:,}")
    print(f"Revenue: ${revenue:,}")
    print(f"Operating margin: {operating_margin:.2%}")


if __name__ == "__main__":
    main()
