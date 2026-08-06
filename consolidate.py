"""
Consolidate all five validated metrics (revenue, gross margin,
operating margin, ROIC, customer concentration) for Vertiv (VRT),
Eaton (ETN), Quanta Services (PWR), and nVent (NVT) into a single
comparison table.

Every number in this table comes from calling the already-validated
functions in fetch_margins.py, fetch_roic_others.py, and
fetch_customer_concentration.py directly:
- Revenue, gross margin, operating margin: fetch_margins.compute_margins()
- ROIC: fetch_roic_others.compute_roic(), run against the exact same
  companyfacts response already fetched for margins — not re-fetched,
  not re-derived.
- Customer concentration: fetch_customer_concentration's own
  most_recent_10k() / fetch_10k_text() / customer_concentration_sentences()
  pull the filing text and extract candidate sentences exactly as that
  script does standalone. The only new code here is
  concentration_headline(), a thin selection/formatting layer that
  picks which already-extracted sentence is the table's headline
  figure (it does not scan the filing itself or invent a number).

Each underlying compute_*() function still prints its own diagnostic
output (tag matches, proxy derivations, flags) as it runs, exactly as
when those scripts are run standalone, so every number in the table
can be traced back to its source.

"Peer average" for the spread columns is the simple mean across all
four companies for that metric (including the company itself).

SEC EDGAR requires a descriptive User-Agent on every request and asks
that callers stay under 10 requests/second; the underlying scripts
already enforce this via their own sec_get()/sleep().

Output:
- output/consolidated_comparison.xlsx
- output/consolidated_comparison.html
"""

import os
import re

import fetch_customer_concentration
import fetch_margins
import fetch_roic_others
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TICKERS = ["VRT", "ETN", "PWR", "NVT"]
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# --- Customer concentration headline selection -----------------------------
# Operates only on sentences already extracted by
# fetch_customer_concentration.customer_concentration_sentences(); does not
# re-scan the filing or invent a figure.

LARGEST_CUSTOMER_PATTERN = re.compile(
    r"largest customers?\s+(?:accounted for|represented)\s+(?:approximately\s+)?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
TOP_N_CUSTOMERS_PATTERN = re.compile(
    r"(?:ten|top\s*10|top\s*ten)\s+largest customers\s+(?:accounted for|represented)\s+(?:approximately\s+)?(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
NO_CUSTOMER_PATTERN = re.compile(
    r"no (?:single )?customer[s]?\b.{0,80}?(\d+(?:\.\d+)?)\s*%", re.IGNORECASE
)


def concentration_headline(sentences):
    """Pick the headline customer-concentration figure and its source
    sentence from the list of sentences fetch_customer_concentration.py
    already flagged as relevant. Priority:
      1. A "largest customer(s) accounted for/represented X%" statement
         (plus a companion top-10/top-ten figure if the same sentence
         discloses one).
      2. An explicit "no customer(s) ... X%" statement.
      3. Nothing matched -> "Not disclosed".
    """
    for s in sentences:
        m = LARGEST_CUSTOMER_PATTERN.search(s)
        if m:
            headline = f"Largest customer: {m.group(1)}%"
            m2 = TOP_N_CUSTOMERS_PATTERN.search(s)
            if m2:
                headline += f"; top 10: {m2.group(1)}%"
            return headline, s

    for s in sentences:
        m = NO_CUSTOMER_PATTERN.search(s)
        if m:
            return f"No customer exceeds {m.group(1)}%", s

    return "Not disclosed", None


def fmt_pct(x):
    return f"{x:.2%}"


def fmt_spread(x):
    sign = "+" if x >= 0 else ""
    return f"{sign}{x * 100:.1f}pp"


def gather_rows():
    tickers_json = fetch_margins.sec_get("https://www.sec.gov/files/company_tickers.json").json()
    ciks = {}
    for ticker in TICKERS:
        match = next(v for v in tickers_json.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    rows = []
    for ticker in TICKERS:
        cik = ciks[ticker]

        facts = fetch_margins.sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json()
        us_gaap = facts["facts"]["us-gaap"]

        margins = fetch_margins.compute_margins(ticker, us_gaap)
        roic = fetch_roic_others.compute_roic(ticker, us_gaap)

        filing = fetch_customer_concentration.most_recent_10k(cik)
        text, source_url = fetch_customer_concentration.fetch_10k_text(cik, filing["accession"], filing["document"])
        sentences = fetch_customer_concentration.customer_concentration_sentences(text)
        headline, source_sentence = concentration_headline(sentences)

        rows.append({
            "ticker": ticker,
            "fiscal_year": margins["fiscal_year"],
            "revenue": margins["revenue"],
            "gross_margin": margins["gross_margin"],
            "operating_margin": margins["operating_margin"],
            "roic": roic["roic"],
            "concentration_headline": headline,
            "concentration_source": source_sentence,
            "concentration_filing_url": source_url,
        })

    return rows


def add_spreads(rows):
    for metric in ("gross_margin", "operating_margin", "roic"):
        avg = sum(r[metric] for r in rows) / len(rows)
        for r in rows:
            r[f"{metric}_peer_avg"] = avg
            r[f"{metric}_spread"] = r[metric] - avg
    return rows


COLUMNS = [
    ("ticker", "Ticker"),
    ("fiscal_year", "FY"),
    ("revenue", "Revenue"),
    ("gross_margin", "Gross Margin"),
    ("gross_margin_spread", "Gross Margin Spread vs. Peer Avg"),
    ("operating_margin", "Operating Margin"),
    ("operating_margin_spread", "Operating Margin Spread vs. Peer Avg"),
    ("roic", "ROIC"),
    ("roic_spread", "ROIC Spread vs. Peer Avg"),
    ("concentration_headline", "Customer Concentration"),
]


def formatted_cell(key, row):
    val = row[key]
    if key == "revenue":
        return f"${val:,.0f}"
    if key in ("gross_margin", "operating_margin", "roic"):
        return fmt_pct(val)
    if key.endswith("_spread"):
        return fmt_spread(val)
    return val


def print_table(rows):
    headers = [label for _, label in COLUMNS]
    table_rows = [[str(formatted_cell(key, r)) for key, _ in COLUMNS] for r in rows]
    widths = [max(len(h), max(len(row[i]) for row in table_rows)) for i, h in enumerate(headers)]

    def fmt_row(cells):
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    print("=== Consolidated comparison: VRT / ETN / PWR / NVT (FY2025) ===")
    print(fmt_row(headers))
    print("-+-".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt_row(row))


def write_xlsx(rows, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Comparison"

    headers = [label for _, label in COLUMNS]
    ws.append(headers)
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for r in rows:
        row_values = []
        for key, _ in COLUMNS:
            val = r[key]
            if key in ("gross_margin", "operating_margin", "roic") or key.endswith("_spread"):
                row_values.append(val)  # write raw float; format via number_format below
            else:
                row_values.append(val)
        ws.append(row_values)

    pct_cols = [i for i, (key, _) in enumerate(COLUMNS, start=1) if key in ("gross_margin", "operating_margin", "roic")]
    spread_cols = [i for i, (key, _) in enumerate(COLUMNS, start=1) if key.endswith("_spread")]
    revenue_col = next(i for i, (key, _) in enumerate(COLUMNS, start=1) if key == "revenue")

    for row_idx in range(2, 2 + len(rows)):
        ws.cell(row=row_idx, column=revenue_col).number_format = "$#,##0"
        for col in pct_cols:
            ws.cell(row=row_idx, column=col).number_format = "0.00%"
        for col in spread_cols:
            cell = ws.cell(row=row_idx, column=col)
            cell.number_format = "+0.0%;-0.0%"
            if cell.value is not None:
                if cell.value > 0:
                    cell.font = Font(color="006100")
                elif cell.value < 0:
                    cell.font = Font(color="9C0006")

    for col_idx, (key, _) in enumerate(COLUMNS, start=1):
        max_len = max(
            [len(headers[col_idx - 1])] + [len(str(formatted_cell(key, r))) for r in rows]
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, max_len + 2)

    ws.freeze_panes = "A2"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb.save(path)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Consolidated Metrics Comparison — VRT / ETN / PWR / NVT</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 0.2rem; }}
  .subtitle {{ color: #555; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: right; }}
  th {{ background: #1f4e78; color: #fff; text-align: center; position: sticky; top: 0; }}
  td:first-child, th:first-child {{ text-align: left; font-weight: 600; }}
  td.concentration {{ text-align: left; }}
  tr:nth-child(even) {{ background: #f7f9fb; }}
  .pos {{ color: #0a6b0a; }}
  .neg {{ color: #b3261e; }}
  .footnote {{ margin-top: 1.5rem; font-size: 0.8rem; color: #666; max-width: 800px; }}
</style>
</head>
<body>
<h1>Consolidated Metrics Comparison</h1>
<div class="subtitle">VRT, ETN, PWR, NVT — FY2025 — spreads are vs. the peer average (mean of all four companies)</div>
<table>
<thead><tr>{header_row}</tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
<div class="footnote">
Revenue, gross margin, and operating margin from fetch_margins.py; ROIC from fetch_roic_others.py;
customer concentration extracted from each company's most recent 10-K by fetch_customer_concentration.py.
ETN operating margin uses the derived proxy (Revenue - COGS - SG&amp;A - R&amp;D); no tagged OperatingIncomeLoss for FY2025.
</div>
</body>
</html>
"""


def write_html(rows, path):
    header_row = "".join(f"<th>{label}</th>" for _, label in COLUMNS)
    body_rows = []
    for r in rows:
        cells = []
        for key, _ in COLUMNS:
            val = formatted_cell(key, r)
            css_class = ""
            if key.endswith("_spread"):
                css_class = " class=\"pos\"" if r[key] >= 0 else " class=\"neg\""
            if key == "concentration_headline":
                css_class = " class=\"concentration\""
            cells.append(f"<td{css_class}>{val}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    html = HTML_TEMPLATE.format(header_row=header_row, body_rows="\n".join(body_rows))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(html)


def main():
    rows = gather_rows()
    rows = add_spreads(rows)

    print()
    print_table(rows)

    xlsx_path = os.path.join(OUTPUT_DIR, "consolidated_comparison.xlsx")
    html_path = os.path.join(OUTPUT_DIR, "consolidated_comparison.html")
    write_xlsx(rows, xlsx_path)
    write_html(rows, html_path)

    print()
    print(f"Wrote {xlsx_path}")
    print(f"Wrote {html_path}")


if __name__ == "__main__":
    main()
