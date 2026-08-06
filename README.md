# moat-engine

Computes a moat and credit/leverage comparison across a set of public
companies directly from SEC EDGAR filings, and outputs the result as
an Excel workbook and a standalone HTML table.

## Coverage set

Vertiv (VRT), Eaton (ETN), Quanta Services (PWR), and nVent (NVT),
most recent fiscal year (FY2025 10-Ks as of the current data pull).

Extensible: add a ticker to the `TICKERS` list in each `fetch_*.py`
script (and in `consolidate.py`) to add a company. New filers should
be spot-checked against the tag fallbacks described in Limitations
below — EDGAR tag usage isn't uniform across companies, and a new
filer may hit a fallback path (or a missing tag) that hasn't been
seen yet.

## Metrics

**Moat**
- **Gross margin** = (Revenue − Cost of revenue) / Revenue
- **Operating margin** = Operating income / Revenue
- **ROIC** = NOPAT / Invested capital, where NOPAT = Operating income
  × (1 − effective tax rate), and Invested capital = Total debt +
  Total equity − Cash. Total debt = current debt + noncurrent debt +
  noncurrent operating lease liability + noncurrent finance lease
  liability, each counted exactly once.
- **Customer concentration** — not an XBRL figure. Extracted from the
  10-K's prose (business section, MD&A, or a revenue/receivables
  footnote): either a named percentage ("largest customer accounted
  for X% of revenue") or a statement that no customer exceeds a given
  threshold. Reported as "Not disclosed" when the filing states
  neither — never estimated.

**Credit / leverage**
- **Debt / EBITDA** = Total debt / EBITDA
- **Net debt / EBITDA** = (Total debt − Cash) / EBITDA
- **Interest coverage** = EBITDA / Interest expense

  EBITDA = Operating income + D&A. D&A is pulled from the cash flow
  statement's combined depreciation-and-amortization tag where a
  filer reports one; otherwise derived from its components (see
  Limitations).

## Methodology

The thing this isn't: a data terminal that silently normalizes
everything into one schema. Two design choices instead:

1. **Adaptive tag resolution.** Filers tag the same accounting concept
   under different XBRL tag names — e.g. cost of revenue shows up as
   either `CostOfRevenue` or `CostOfGoodsAndServicesSold` depending on
   the company; noncurrent debt as either `LongTermDebtNoncurrent` or
   a combined `LongTermDebtAndCapitalLeaseObligations`. Each script
   tries an ordered list of candidate tags per concept, and records
   *which* tag actually matched — that tag name is printed alongside
   every value, not just the number.

2. **No unverifiable numbers.** Whenever a script has to deviate from
   a straightforward tagged value — deriving a subtotal from
   components because the filer doesn't tag it directly, substituting
   a fallback tag, using a net figure in place of a gross one — it
   prints an explicit `FLAG` line naming exactly what happened and
   why. These flags are not swallowed in the consolidated table
   either: `consolidate.py` carries them through into a dedicated
   flags section in both the xlsx and the html output. If a number in
   this project's output can't be traced to a specific XBRL tag (or,
   for customer concentration, a specific sentence in the filing),
   that's a bug, not an intentional simplification.

Concretely, examples of both in the current data: Eaton's operating
income isn't tagged for FY2025 (Eaton stopped reporting that subtotal
after FY2019), so it's derived as Revenue − COGS − SG&A − R&D, with
every other candidate P&L tag printed and annotated with why it was
excluded. nVent's interest coverage falls back to a *net*
interest-income/expense figure because no gross interest expense tag
exists for FY2025 — flagged as likely overstating coverage relative
to the other three companies' gross-expense basis, not silently
computed as if it were equivalent.

## Repository layout

- `fetch_margins.py` — gross margin and operating margin, all four
  companies.
- `fetch_roic_others.py` — ROIC, all four companies. Also exposes
  `total_debt_for_roic()`, which `fetch_credit_metrics.py` reuses
  directly rather than re-deriving total debt.
- `fetch_customer_concentration.py` — fetches each company's most
  recent 10-K text and extracts customer-concentration disclosure
  sentences.
- `fetch_credit_metrics.py` — debt/EBITDA, net debt/EBITDA, interest
  coverage. Reuses `total_debt_for_roic()` and
  `fetch_margins.compute_margins()` rather than recomputing operating
  income or total debt.
- `consolidate.py` — imports the compute functions from all of the
  above and assembles one comparison table (moat metrics + credit
  metrics, with peer-average spread columns for the moat numerics).
  Writes the xlsx and html outputs.
- `fetch_vrt_10k.py`, `fetch_vrt_margin.py`, `fetch_vrt_roic.py` —
  early single-company (VRT-only) prototypes that predate the
  four-company scripts above. Kept for history; not part of the
  current pipeline.

## Running it

```bash
python3 -m venv venv
source venv/bin/activate
pip install requests beautifulsoup4 lxml openpyxl

python3 consolidate.py
```

`consolidate.py` runs the full pipeline end to end: it prints each
company's per-metric diagnostics (tag matches, proxy derivations,
flags) to stdout, then the consolidated table, then writes:

- `output/consolidated_comparison.xlsx`
- `output/consolidated_comparison.html`

Any individual `fetch_*.py` script can also be run standalone (e.g.
`python3 fetch_margins.py`) to see just that metric's diagnostics
without the others.

Requires network access to SEC EDGAR (`www.sec.gov`,
`data.sec.gov`). Every script sets a descriptive `User-Agent` header,
as SEC's fair-use policy requires, and rate-limits its own requests.

## Limitations / caveats

Known tag fallbacks currently in effect, as of the FY2025 data pull:

- **ETN** — `OperatingIncomeLoss` not tagged for FY2025 (Eaton stopped
  reporting that subtotal after FY2019); operating income is derived
  as Revenue − COGS − SG&A − R&D. This flows into operating margin,
  EBITDA, and ROIC.
- **ETN, PWR** — no `InterestExpense` tag for FY2025 (the tag went
  stale after an earlier fiscal year); interest expense uses
  `InterestExpenseNonoperating` instead. Both are still gross
  interest-expense figures, just filed under a different tag name.
- **NVT** — no gross interest expense tag at all for FY2025 (nVent
  stopped tagging `InterestExpense` after FY2023); interest coverage
  falls back to `|InterestIncomeExpenseNet|`, which nets against
  interest income. NVT's interest coverage is likely overstated
  relative to the other three companies' gross-expense basis.
- **PWR, NVT** — no combined `DepreciationDepletionAndAmortization`
  tag; D&A is derived as `Depreciation + AmortizationOfIntangibleAssets`,
  which excludes finance-lease right-of-use asset amortization where
  it's separately tagged (a comparatively small line).
- **NVT** — cash falls back to
  `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents`
  (may include a small restricted-cash component) since
  `CashAndCashEquivalentsAtCarryingValue` isn't tagged for FY2025.
- **VRT** — the finance-lease-liability component of total debt is
  $0 for FY2025 per the tagged data; worth a hand-check against the
  balance sheet to confirm that's genuinely zero rather than untagged.
- **Customer concentration** is read from filing prose, not XBRL —
  there's no structured tag for it. Extraction is keyword/pattern
  based; a filing that discloses concentration in phrasing the
  patterns don't anticipate could be missed rather than flagged.
  VRT's FY2025 10-K doesn't disclose a concentration figure at all,
  and is reported as "Not disclosed" rather than estimated.
- **Peer average** (used for the moat-metric spread columns) is the
  simple mean across the current four-company coverage set — it will
  shift if the coverage set is extended or if a company's fiscal year
  changes.
- Every figure reflects the most recent 10-K available at the time
  the script is run. Re-running after a company files a new 10-K will
  move that company's numbers to the newer fiscal year.
