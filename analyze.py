"""
analyze.py — single entry point: one ticker in, one coherent report out.

Purely a consolidation layer. No metric, trajectory, verdict, or
resolution logic is reimplemented here — everything is computed by
calling straight into the already-validated modules:
  - full_metrics_for_ticker.py -> ticker resolution (resolve_ticker.py)
    + the current-year metric set (fetch_margins.py, fetch_roic_others.py,
    fetch_credit_metrics.py) with the confidence framework applied
  - fetch_trajectory.py        -> 5-year trajectory, per-year confidence
  - fetch_verdict.py           -> the plain-language verdict (moat
    direction, margin durability, credit direction), benchmarked against
    the four-company peer set (VRT/ETN/PWR/NVT) — the only peer group
    this tool has. For an arbitrary ticker outside that set (e.g. ORCL),
    the moat-direction spread is still computed against that same
    benchmark; it's the only comparison basis available, and the report
    labels it explicitly so the number isn't mistaken for an
    apples-to-apples industry comparison.

analyze() assembles those outputs into one dict; main() renders it as a
console report and a single HTML file. Two small, additive changes were
made to support this reuse (neither changes any existing printed output
or computed value):
  - full_metrics_for_ticker.full_metrics_for_ticker() now also returns
    "us_gaap" (the already-fetched companyfacts payload), so analyze.py
    can hand it straight to fetch_trajectory.gather_ticker_trajectory()
    instead of fetching it a second time.
  - fetch_verdict.print_company_verdict() now also returns the values it
    already computes (moat direction, durability label, etc.), instead
    of only printing them, so analyze.py's HTML report doesn't have to
    re-derive or scrape them.

Network usage: the peer group (VRT/ETN/PWR/NVT) is fetched here via a
small local helper — the same User-Agent/rate-limit discipline every
other script in this codebase uses — rather than through
fetch_margins.sec_get(), whose MAX_CALLS budget is sized for exactly
that four-company set and would be exceeded by adding a fifth,
arbitrary target ticker in the same process run. The target ticker's
own companyfacts fetch (via full_metrics_for_ticker(), which does use
fetch_margins.sec_get() internally) stays a single, separate call,
comfortably inside that existing budget.
"""

import html
import io
import os
import sys
import time
from contextlib import redirect_stdout

import requests

import fetch_customer_concentration
import fetch_trajectory
import fetch_verdict
import full_metrics_for_ticker

HEADERS = {"User-Agent": "Aidan Breer Student Research aidanmbreer@gmail.com"}
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

PEER_TICKERS = fetch_trajectory.TICKERS  # ["VRT", "ETN", "PWR", "NVT"] — this tool's only peer group


def _get_json(url):
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    time.sleep(0.2)  # stay well under SEC's 10 req/sec courtesy limit
    return resp.json()


def gather_peer_series():
    """Peer-group 5-year trajectories — the exact same call
    (fetch_trajectory.gather_ticker_trajectory) and TICKERS list
    fetch_verdict.py's own main() uses, just fetched through this
    module's own uncapped helper instead of fetch_margins.sec_get()."""
    tickers_json = _get_json("https://www.sec.gov/files/company_tickers.json")
    ciks = {}
    for ticker in PEER_TICKERS:
        match = next(v for v in tickers_json.values() if v["ticker"] == ticker)
        ciks[ticker] = str(match["cik_str"]).zfill(10)

    all_series = {}
    for ticker in PEER_TICKERS:
        cik = ciks[ticker]
        facts = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        us_gaap = facts["facts"]["us-gaap"]
        all_series[ticker] = fetch_trajectory.gather_ticker_trajectory(ticker, us_gaap)
    return all_series


def analyze(ticker):
    """Runs the full pipeline for one ticker. Returns a dict with
    status "error" (ticker didn't resolve — same failure shape
    resolve_ticker/full_metrics_for_ticker already use) or "ok" with
    everything the console and HTML reports need.

    The compute functions this calls into (compute_margins, compute_roic,
    compute_credit_metrics, and their per-year trajectory calls, for both
    the target ticker and the four peer companies) each print their own
    per-tag diagnostic trace as an unavoidable side effect of being called
    — useful when run standalone, but overwhelming here (~5 companies x
    5 years of raw tag-resolution detail) buried under the one coherent
    report this function exists to produce. That data-gathering trace is
    captured, not discarded, so it's still available for hand-checking —
    see the returned "raw_trace" key — just not printed by default. This
    only affects what analyze.py chooses to print; none of the reused
    functions themselves are changed, and no computed value is altered by
    capturing their stdout instead of letting it reach the terminal.
    """
    ticker = ticker.strip().upper()

    trace_buf = io.StringIO()
    with redirect_stdout(trace_buf):
        current = full_metrics_for_ticker.full_metrics_for_ticker(ticker)
        if current["status"] == "ok":
            trajectory_series = fetch_trajectory.gather_ticker_trajectory(ticker, current["us_gaap"])
            peer_series = gather_peer_series()

    if current["status"] == "error":
        return {"status": "error", "ticker": ticker, "reason": current["reason"]}

    all_series = dict(peer_series)
    all_series[ticker] = trajectory_series  # self-consistent if ticker is already one of the peers

    gross_peer_avg = fetch_verdict.peer_average_series(all_series, "gross_margin")
    op_peer_avg = fetch_verdict.peer_average_series(all_series, "operating_margin")
    gross_spread = fetch_verdict.spread_series(all_series, ticker, "gross_margin", gross_peer_avg)
    op_spread = fetch_verdict.spread_series(all_series, ticker, "operating_margin", op_peer_avg)

    filing = fetch_customer_concentration.most_recent_10k(current["cik"])
    text, filing_source_url = fetch_customer_concentration.fetch_10k_text(
        current["cik"], filing["accession"], filing["document"]
    )
    sentences = fetch_customer_concentration.customer_concentration_sentences(text)

    # print_company_verdict() both prints its console block and (as of the
    # additive change made for this task) returns the same computed values —
    # called exactly once here so its printed side effect isn't duplicated
    # between the console report and the HTML report.
    verdict_buf = io.StringIO()
    with redirect_stdout(verdict_buf):
        verdict = fetch_verdict.print_company_verdict(ticker, all_series, gross_spread, op_spread, sentences)

    return {
        "status": "ok",
        "ticker": ticker,
        "current": current,
        "trajectory": trajectory_series,
        "gross_spread": gross_spread,
        "op_spread": op_spread,
        "sentences": sentences,
        "filing": filing,
        "filing_source_url": filing_source_url,
        "raw_trace": trace_buf.getvalue(),
        "all_series": all_series,
        "is_peer": ticker in PEER_TICKERS,
        "verdict": verdict,
        "verdict_console_text": verdict_buf.getvalue(),
    }


def print_report(report):
    ticker = report["ticker"]
    if report["status"] == "error":
        print(f"########## {ticker} ##########")
        print(f"CANNOT PROCESS: {report['reason']}")
        print()
        return

    print("=" * 78)
    print(f"ANALYZE: {ticker} — unified single-ticker report")
    print("=" * 78)
    print()

    print("--- CURRENT-YEAR METRICS (with confidence flags) ---")
    full_metrics_for_ticker.print_result(report["current"])

    print("--- 5-YEAR TRAJECTORY ---")
    fetch_trajectory.print_trajectory_summary(ticker, report["trajectory"])

    print("--- VERDICT ---")
    print(
        "Benchmarked against this tool's peer group (VRT, ETN, PWR, NVT)"
        + ("." if report["is_peer"] else f" — {ticker} is outside that set; treat the spread as directional, not an industry comparison.")
    )
    print(report["verdict_console_text"], end="")


# --- HTML ---------------------------------------------------------------

CONFIDENCE_COLORS = {
    "clean": "#0a6b0a",
    "derived": "#8a6d00",
    "fallback": "#b3261e",
    "stale": "#b3261e",
    "unresolved": "#999999",
}

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{ticker} — Analysis</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.1rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 2rem; margin-bottom: 0.6rem; border-bottom: 2px solid #1f4e78; padding-bottom: 0.2rem; }}
  .subtitle {{ color: #555; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.88rem; margin-bottom: 1rem; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
  th {{ background: #1f4e78; color: #fff; text-align: center; }}
  td:first-child, th:first-child {{ text-align: left; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #f7f9fb; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.78rem; font-weight: 600; color: #fff; }}
  .flags {{ text-align: left; font-size: 0.78rem; color: #555; margin-top: 2px; }}
  .verdict-box {{ background: #f2f6fa; border: 1px solid #ccd9e6; border-radius: 6px; padding: 1rem 1.2rem; }}
  .verdict-line {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 0.6rem; }}
  .signal {{ margin-bottom: 0.5rem; }}
  .signal .label {{ font-weight: 600; }}
  .caveats {{ margin-top: 0.8rem; font-size: 0.85rem; color: #7a4a00; }}
  .caveats li {{ margin-bottom: 0.3rem; }}
  .peer-note {{ font-size: 0.85rem; color: #666; margin-bottom: 0.8rem; }}
  .footnote {{ margin-top: 2rem; font-size: 0.8rem; color: #666; max-width: 800px; }}
</style>
</head>
<body>
<h1>{company_name} ({ticker})</h1>
<div class="subtitle">CIK {cik} — most recent fiscal year end {fy_end} — {source_link}</div>

<h2>Current-Year Metrics</h2>
<table>
<thead><tr><th>Metric</th><th>Value</th><th>Confidence</th><th>Tag / Method</th></tr></thead>
<tbody>
{metrics_rows}
</tbody>
</table>

<h2>5-Year Trajectory</h2>
<table>
<thead><tr><th>Metric</th>{trajectory_year_headers}<th>Direction</th></tr></thead>
<tbody>
{trajectory_rows}
</tbody>
</table>

<h2>Verdict</h2>
<div class="peer-note">{peer_note}</div>
<div class="verdict-box">
<div class="verdict-line">{verdict_line}</div>
{signals_html}
{caveats_html}
</div>

<div class="footnote">
Every number above is computed by the existing, already-validated modules (fetch_margins.py, fetch_roic_others.py,
fetch_credit_metrics.py, fetch_trajectory.py, fetch_verdict.py) — analyze.py only resolves the ticker, calls them,
and assembles this one report; no metric is recomputed here.
</div>
</body>
</html>
"""


def confidence_badge(status):
    color = CONFIDENCE_COLORS.get(status, "#999999")
    return f'<span class="badge" style="background:{color}">{status.upper()}</span>'


def esc(text):
    return html.escape(str(text))


def metrics_table_rows(metrics):
    rows = []
    for key in full_metrics_for_ticker.METRIC_ORDER:
        m = metrics[key]
        label = esc(full_metrics_for_ticker.METRIC_LABELS[key])
        if m["status"] == "unresolved":
            rows.append(
                f"<tr><td>{label}</td><td>—</td><td>{confidence_badge('unresolved')}</td>"
                f'<td class="flags">{esc(m["reason"])}</td></tr>'
            )
            continue
        value_str = esc(full_metrics_for_ticker.fmt_value(key, m["value"]))
        flags_html = ""
        if m.get("flags"):
            flags_html = "<div class='flags'>" + "<br>".join(esc(f) for f in m["flags"]) + "</div>"
        rows.append(
            f"<tr><td>{label}</td><td>{value_str}</td><td>{confidence_badge(m['status'])}</td>"
            f"<td>{esc(m['tag'])}{flags_html}</td></tr>"
        )
    return "\n".join(rows)


def trajectory_table(series):
    # Years are the same across all six metric series for this coverage model.
    any_metric = next(iter(fetch_trajectory.METRIC_LABELS))
    years = [yr for yr, _, _ in series[any_metric]]
    year_headers = "".join(f"<th>FY{esc(yr)}</th>" for yr in years)

    rows = []
    for metric, label in fetch_trajectory.METRIC_LABELS.items():
        s = series[metric]
        cells = "".join(
            f'<td>{esc(fetch_trajectory.fmt_value(metric, val))}<div class="flags">[{esc(conf)}]</div></td>'
            for _, val, conf in s
        )
        direction, *_ = fetch_trajectory.direction_and_change(metric, s)
        rows.append(f"<tr><td>{esc(label)}</td>{cells}<td>{esc(direction)}</td></tr>")

    return year_headers, "\n".join(rows)


def signal_html(label, value, note=None):
    block = f'<div class="signal"><span class="label">{esc(label)}:</span> {esc(value)}</div>'
    if note:
        block = block[:-6] + f' <span style="color:#7a4a00;font-size:0.85rem;">({esc(note)})</span></div>'
    return block


def write_html(report, path):
    ticker = report["ticker"]
    current = report["current"]

    metrics_rows = metrics_table_rows(current["metrics"])
    year_headers, trajectory_rows = trajectory_table(report["trajectory"])

    verdict = report["verdict"]

    peer_note = (
        "Benchmarked against this tool's peer group (VRT, ETN, PWR, NVT)."
        if report["is_peer"]
        else (
            f"Benchmarked against this tool's peer group (VRT, ETN, PWR, NVT) — {esc(ticker)} is outside that "
            "four-company coverage set, so treat the margin-spread signal below as directional only, not "
            "an apples-to-apples industry comparison."
        )
    )

    signals = []
    signals.append(
        signal_html(
            "Moat direction",
            f"{verdict['moat_direction']} (ROIC {verdict['roic_direction']}, gross-margin spread "
            f"{verdict['gross_margin_spread_direction']}, operating-margin spread {verdict['operating_margin_spread_direction']})",
            verdict["roic_confidence_note"],
        )
    )
    signals.append(
        signal_html(
            "Margin quality / durability",
            f"{verdict['durability_label']} — {verdict['durability_explanation']}",
        )
    )
    signals.append(
        signal_html(
            "Credit direction",
            f"leverage {verdict['leverage_label']}, coverage {verdict['coverage_label']}",
            verdict["leverage_confidence_note"] or verdict["coverage_confidence_note"],
        )
    )
    signals_html = "\n".join(signals)

    caveats_html = ""
    if verdict["caveats"]:
        items = "\n".join(f"<li>{esc(c)}</li>" for c in verdict["caveats"])
        caveats_html = f'<ul class="caveats">{items}</ul>'

    page_html = HTML_TEMPLATE.format(
        ticker=esc(ticker),
        company_name=esc(current["company_name"]),
        cik=esc(current["cik"]),
        fy_end=esc(current["authoritative_fiscal_year_end"]),
        source_link=f'<a href="{esc(report["filing_source_url"])}">source 10-K</a>',
        metrics_rows=metrics_rows,
        trajectory_year_headers=year_headers,
        trajectory_rows=trajectory_rows,
        peer_note=peer_note,
        verdict_line=esc(verdict["verdict_line"]),
        signals_html=signals_html,
        caveats_html=caveats_html,
    )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(page_html)


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 analyze.py TICKER")
        sys.exit(1)

    ticker = sys.argv[1]
    report = analyze(ticker)
    print_report(report)

    if report["status"] == "ok":
        html_path = os.path.join(OUTPUT_DIR, f"analyze_{report['ticker']}.html")
        write_html(report, html_path)
        print(f"Wrote {html_path}")


if __name__ == "__main__":
    main()
