"""
webapp.py — local web UI for analyze.py.

A thin Flask presentation layer over the existing analyze.py pipeline.
No metric, trajectory, verdict, resolution, or scope logic lives here —
every number comes from analyze.analyze() and analyze.render_html(),
imported and called directly, unchanged. This file only handles HTTP
routing, a simple in-memory cache, and error presentation.

Run:
    python3 webapp.py
Then open http://127.0.0.1:5001 in a browser.

Caching: successful analyses are cached in memory, keyed by normalized
ticker, for the life of the process — re-requesting the same ticker
serves the already-rendered report instantly instead of re-hitting SEC
EDGAR (respects SEC's rate-limit courtesy expectations, which every
script in this codebase already builds in at the request-fetching
level). Errors are not cached, since a fetch failure may be transient;
a genuinely bad ticker fails fast on retry anyway (resolve_ticker's own
ticker-directory lookup is a local dict scan after the first fetch).
"""

from flask import Flask, render_template, request

import analyze

app = Flask(__name__)

_CACHE = {}  # ticker -> rendered HTML string (analyze.render_html output)

NAV_BAR = """
<div style="background:#1f4e78;color:#fff;padding:0.9rem 1.2rem;border-radius:8px;
            margin-bottom:1.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
  <a href="/" style="color:#fff;text-decoration:none;font-weight:700;font-size:1.05rem;">moat-engine</a>
  <form method="POST" action="/analyze" style="display:flex;gap:0.5rem;align-items:center;margin-left:auto;">
    <input type="text" name="ticker" placeholder="Ticker" maxlength="10" required
           style="padding:0.45rem 0.7rem;border-radius:4px;border:none;text-transform:uppercase;width:9rem;">
    <button type="submit" style="padding:0.45rem 1rem;border-radius:4px;border:none;
            background:#fff;color:#1f4e78;font-weight:600;cursor:pointer;">Analyze</button>
  </form>
  {cached_note}
</div>
"""
CACHED_NOTE = '<span style="font-size:0.8rem;opacity:0.85;">served from cache</span>'


def _with_nav_bar(page_html, cached):
    nav = NAV_BAR.format(cached_note=CACHED_NOTE if cached else "")
    return page_html.replace("<body>", "<body>\n" + nav, 1)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["GET", "POST"])
def analyze_ticker():
    raw = request.values.get("ticker", "")
    ticker = raw.strip().upper()

    if not ticker:
        return render_template("index.html", error="Enter a ticker symbol.")

    cached_html = _CACHE.get(ticker)
    if cached_html is not None:
        return _with_nav_bar(cached_html, cached=True)

    try:
        report = analyze.analyze(ticker)
    except Exception as e:
        # Network hiccups, SEC rate-limit/availability issues, or anything
        # else unanticipated — never a stack trace in the browser.
        return render_template(
            "index.html",
            ticker=raw,
            error=f"Something went wrong fetching data for '{ticker}': {e}",
        )

    if report["status"] == "error":
        # Bad/unknown ticker — resolve_ticker.py's own clean failure path,
        # reused as-is (see analyze.analyze()'s "status": "error" branch).
        return render_template("index.html", ticker=raw, error=report["reason"])

    page_html = analyze.render_html(report)
    _CACHE[ticker] = page_html
    return _with_nav_bar(page_html, cached=False)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
