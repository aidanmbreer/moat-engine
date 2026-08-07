"""
Resolve an arbitrary ticker to its SEC EDGAR filings — no metrics
computed. First slice of ticker-agnostic support: prove the tool can
take a ticker it has never seen before and reliably find the right
10-K (or fail clearly, not silently or by guessing) before any
calculation logic gets layered on top of it.

Does not touch fetch_margins.py, fetch_roic_others.py,
fetch_customer_concentration.py, fetch_credit_metrics.py,
fetch_trajectory.py, fetch_verdict.py, or consolidate.py — none of
the existing four-company pipeline is modified or called here. This
is a standalone, ticker-agnostic resolver.

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second.
"""

import time

import requests

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}

_TICKER_DIRECTORY = None  # fetched once, reused across resolve_ticker() calls


def _get(url):
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.2)  # stay well under the 10 req/sec limit
    return resp


def _ticker_directory():
    global _TICKER_DIRECTORY
    if _TICKER_DIRECTORY is None:
        _TICKER_DIRECTORY = _get("https://www.sec.gov/files/company_tickers.json").json()
    return _TICKER_DIRECTORY


def resolve_ticker(ticker):
    """Resolve `ticker` to its CIK and most recent 10-K filing, and
    confirm the filing document actually loads.

    Returns a dict. On success:
      {"status": "ok", "ticker", "company_name", "cik", "filing_date",
       "accession_number", "fiscal_year_end", "document_url",
       "document_size_bytes"}
    On failure — unknown ticker, no 10-K on file, or the document
    failing to load — returns instead:
      {"status": "error", "ticker", "reason"}
    Never raises for a bad ticker and never guesses a substitute; a
    failure is always reported, not swallowed.
    """
    ticker_norm = ticker.strip().upper()

    try:
        directory = _ticker_directory()
    except requests.RequestException as e:
        return {"status": "error", "ticker": ticker_norm, "reason": f"Could not reach SEC's ticker directory: {e}"}

    match = next((v for v in directory.values() if v["ticker"] == ticker_norm), None)
    if match is None:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": f"'{ticker_norm}' was not found in SEC's ticker-to-CIK mapping.",
        }

    cik = str(match["cik_str"]).zfill(10)
    company_name = match.get("title", "(name not in ticker directory)")

    try:
        submissions = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    except requests.RequestException as e:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": f"Resolved to CIK {cik} ({company_name}) but could not fetch its filing history: {e}",
        }

    recent = submissions["filings"]["recent"]
    idx = next((i for i, form in enumerate(recent["form"]) if form == "10-K"), None)
    if idx is None:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": (
                f"'{ticker_norm}' (CIK {cik}, {company_name}) has a filing history on EDGAR, "
                "but no 10-K was found among its recent filings."
            ),
        }

    filing_date = recent["filingDate"][idx]
    fiscal_year_end = recent["reportDate"][idx]
    accession_number = recent["accessionNumber"][idx]
    primary_document = recent["primaryDocument"][idx]

    accession_nodash = accession_number.replace("-", "")
    document_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}"

    try:
        doc_resp = _get(document_url)
    except requests.RequestException as e:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": f"Found the 10-K (accession {accession_number}) but the document failed to load: {e}",
        }

    if not doc_resp.content:
        return {
            "status": "error",
            "ticker": ticker_norm,
            "reason": f"10-K document at {document_url} loaded but returned an empty response.",
        }

    return {
        "status": "ok",
        "ticker": ticker_norm,
        "company_name": company_name,
        "cik": cik,
        "filing_date": filing_date,
        "accession_number": accession_number,
        "fiscal_year_end": fiscal_year_end,
        "document_url": document_url,
        "document_size_bytes": len(doc_resp.content),
        # SIC (Standard Industrial Classification) is self-reported by the
        # filer, straight from the same submissions payload already fetched
        # above — no extra request. Purely additive: existing callers/keys
        # unaffected. Used by analyze.py's scope declaration.
        "sic": submissions.get("sic"),
        "sic_description": submissions.get("sicDescription"),
    }


def print_resolution(result):
    print(f"=== {result['ticker']} ===")
    if result["status"] == "ok":
        print(f"Company name: {result['company_name']}")
        print(f"CIK: {result['cik']}")
        print(f"Filing date: {result['filing_date']}")
        print(f"Accession number: {result['accession_number']}")
        print(f"Fiscal year end: {result['fiscal_year_end']}")
        print(f"Document URL: {result['document_url']}")
        print(f"Document loaded: {result['document_size_bytes']:,} bytes")
    else:
        print(f"CANNOT PROCESS: {result['reason']}")
    print()


def main():
    # Three large-cap industrials/tech names NOT in the existing
    # four-company coverage set (VRT, ETN, PWR, NVT), plus one
    # deliberately invalid ticker to confirm clean failure.
    test_tickers = ["CAT", "HON", "NVDA", "ZZZZ"]
    for ticker in test_tickers:
        result = resolve_ticker(ticker)
        print_resolution(result)


if __name__ == "__main__":
    main()
