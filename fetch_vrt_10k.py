"""
Fetch the most recent Vertiv (VRT) 10-K filing from SEC EDGAR.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second. This script makes at most
3 requests total, with a short pause between each, so no additional
loop cap is needed.
"""

import re
import time

import requests

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
MAX_CALLS = 3  # hard cap: ticker lookup, submissions lookup, document fetch
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


def main():
    # 1. Look up VRT's CIK number from SEC's ticker directory
    tickers = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    match = next(v for v in tickers.values() if v["ticker"] == "VRT")
    cik = str(match["cik_str"]).zfill(10)

    # 2. Pull VRT's filing history and find the most recent 10-K
    submissions = sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    recent = submissions["filings"]["recent"]
    idx = recent["form"].index("10-K")

    filing_date = recent["filingDate"][idx]
    doc_type = recent["form"][idx]
    accession_number = recent["accessionNumber"][idx]
    primary_doc = recent["primaryDocument"][idx]

    # 3. Fetch the filing document itself
    accession_no_dashes = accession_number.replace("-", "")
    doc_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{match['cik_str']}/{accession_no_dashes}/{primary_doc}"
    )
    doc_resp = sec_get(doc_url)

    # Inline XBRL filings embed a hidden metadata block (<ix:header>) before
    # the visible document text; drop it so the excerpt is readable text.
    html = re.sub(r"<ix:header>.*?</ix:header>", " ", doc_resp.text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    print(f"Filing date: {filing_date}")
    print(f"Document type: {doc_type}")
    print(f"Accession number: {accession_number}")
    print("First 2000 characters of document text:")
    print(text[:2000])


if __name__ == "__main__":
    main()
