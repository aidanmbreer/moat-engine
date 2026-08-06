"""
Pull customer concentration disclosures for Vertiv (VRT), Eaton (ETN),
Quanta Services (PWR), and nVent (NVT) from each company's most recent
10-K.

This metric is not in XBRL/companyfacts — it's disclosed as prose in
the 10-K text (business section, MD&A, risk factors, or a revenue/
receivables footnote), as either a named customer's percent of
revenue or a statement that no single customer exceeds some threshold
(commonly 10%). This script does not compute or estimate anything: it
fetches each filer's primary 10-K document from EDGAR, splits it into
sentences, and prints every sentence that mentions both "customer" and
a "%" figure (or an explicit "no customer/no single customer ..."
statement), so the disclosed figure — or its absence — can be read
and verified directly against the filing text.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
9 requests total (one shared ticker lookup + one submissions call and
one document fetch per company), with a short pause between each.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
TICKERS = ["VRT", "ETN", "PWR", "NVT"]
MAX_CALLS = 1 + 2 * len(TICKERS)  # ticker lookup + (submissions + document) per company
calls_made = 0

# Sentences mentioning "customer" are kept if they also contain a "%"
# sign, or one of these phrases indicating an explicit (possibly
# unquantified in the immediate sentence, e.g. "no customer exceeded
# 10%") concentration statement.
CONCENTRATION_PHRASES = [
    "no customer",
    "no single customer",
    "largest customer",
    "principal customer",
    "top customer",
    "top 10 customer",
    "top ten customer",
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


def most_recent_10k(cik):
    subs = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    recent = subs["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            return {
                "accession": recent["accessionNumber"][i],
                "document": recent["primaryDocument"][i],
                "filing_date": recent["filingDate"][i],
                "report_date": recent["reportDate"][i],
            }
    raise RuntimeError(f"No 10-K found for CIK {cik}")


def fetch_10k_text(cik, accession, document):
    accession_nodash = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{document}"
    resp = sec_get(url)
    soup = BeautifulSoup(resp.content, "lxml")
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text), url


def customer_concentration_sentences(text):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    hits = []
    for s in sentences:
        low = s.lower()
        if "customer" not in low:
            continue
        if "%" in s or any(p in low for p in CONCENTRATION_PHRASES):
            hits.append(s.strip())
    # de-duplicate while preserving order (some sentences repeat across
    # MD&A and footnotes almost verbatim)
    seen = set()
    deduped = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            deduped.append(h)
    return deduped


def main():
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    for ticker in TICKERS:
        cik = ciks[ticker]
        filing = most_recent_10k(cik)
        text, url = fetch_10k_text(cik, filing["accession"], filing["document"])
        sentences = customer_concentration_sentences(text)

        print(f"=== {ticker} ===")
        print(f"10-K for FY ended {filing['report_date']}, filed {filing['filing_date']}")
        print(f"Source: {url}")
        if sentences:
            for s in sentences:
                print(f'  "{s}"')
        else:
            print("  NOT DISCLOSED — no sentence in the filing pairs \"customer\" with a "
                  "percent figure or an explicit concentration statement.")
        print()


if __name__ == "__main__":
    main()
