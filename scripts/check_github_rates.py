"""Periodic check of the hardcoded GitHub list rates in app.py.

GitHub has no pricing API (unlike Oracle's cetools), so the direct-GitHub mapping's
rates are constants that can silently drift. This script fetches GitHub's published
pricing/docs pages and reports whether each hardcoded figure still appears. It is a
DRIFT DETECTOR, not an oracle: GitHub's pages are partly JS-rendered, so a value can
be current yet absent from the raw HTML - every NOT FOUND needs a human look before
concluding the rate changed.

Run: python3 scripts/check_github_rates.py
Exit 0 = every fetchable page confirmed its figure (bot-blocked marketing pages are
listed as MANUAL - open them and confirm by eye).
Exit 1 = a page was fetched but the hardcoded figure was NOT on it - likely real
drift: fix the GH_* constants in app.py, then bump GH_RATES_VERIFIED.

Checked constants (app.py):
  GH_BASE_TEAM_RATE / GH_BASE_ENT_RATE            <- github.com/pricing
  GH_SEC_CODE_RATE / GH_SEC_SECRET_RATE           <- GHAS / security product pages
  GH_COPILOT_BUS_RATE / GH_COPILOT_ENT_RATE       <- Copilot plans page
  GH_COPILOT_BUS_INCLUDED_CREDITS / _ENT_         <- Copilot usage-based billing doc
  GH_AI_CREDIT_RATE ($0.01/credit)                <- same doc
"""
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402  (the constants under test)

UA = {"User-Agent": "Mozilla/5.0 (oci-intake-app rate check)"}

# (label, url, [patterns that should appear if the hardcoded rate is still current]).
# github.com marketing pages 403 non-browser agents; those entries fall back to a
# MANUAL status (open the URL and eyeball the figure) instead of failing the run.
CHECKS = [
    ("Team base ${:.0f}/user".format(app.GH_BASE_TEAM_RATE),
     "https://github.com/pricing",
     [r"\$4\b"]),
    ("Enterprise Cloud base ${:.0f}/user".format(app.GH_BASE_ENT_RATE),
     "https://github.com/pricing",
     [r"\$21\b"]),
    ("Copilot Business ${:.0f}/user".format(app.GH_COPILOT_BUS_RATE),
     "https://docs.github.com/en/copilot/get-started/plans",
     [r"\$19\b"]),
    ("Copilot Enterprise ${:.0f}/user".format(app.GH_COPILOT_ENT_RATE),
     "https://docs.github.com/en/copilot/get-started/plans",
     [r"\$39\b"]),
    ("Code Security ${:.0f}/committer".format(app.GH_SEC_CODE_RATE),
     "https://github.com/security/advanced-security",
     [r"\$30\b"]),
    ("Secret Protection ${:.0f}/committer".format(app.GH_SEC_SECRET_RATE),
     "https://github.com/security/advanced-security",
     [r"\$19\b"]),
    ("Business included credits {:,.0f}/seat".format(app.GH_COPILOT_BUS_INCLUDED_CREDITS),
     "https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises",
     [r"1,?900"]),
    ("Enterprise included credits {:,.0f}/seat".format(app.GH_COPILOT_ENT_INCLUDED_CREDITS),
     "https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises",
     [r"3,?900"]),
    ("AI credit ${:.2f}".format(app.GH_AI_CREDIT_RATE),
     "https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises",
     [r"\$0\.01\b|1 AI credit"]),
]


def fetch(url, cache={}):
    if url in cache:
        return cache[url]
    try:
        req = urllib.request.Request(url, headers=UA)
        body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as exc:  # unreachable page: report, don't crash
        body = None
        print(f"  [unreachable] {url}: {exc}")
    cache[url] = body
    return body


def main():
    failures = 0
    manual = 0
    print(f"Hardcoded GitHub rates last verified: {app.GH_RATES_VERIFIED} "
          f"({app.github_rates_staleness_days()} days ago)\n")
    for label, url, patterns in CHECKS:
        body = fetch(url)
        if body is None:
            # Bot-blocked / unreachable is not evidence of drift - flag for a manual look.
            print(f"MANUAL      {label}   <- open {url} and confirm by eye")
            manual += 1
            continue
        ok = all(re.search(p, body) for p in patterns)
        print(f"{'FOUND     ' if ok else 'NOT FOUND '}  {label}   <- {url}")
        if not ok:
            failures += 1
    print()
    if failures:
        print(f"{failures} check(s) fetched their page but did NOT find the hardcoded "
              f"figure - likely real drift. Fix the GH_* constants in app.py, then bump "
              f"GH_RATES_VERIFIED.")
        return 1
    if manual:
        print(f"{manual} page(s) need a manual eyeball (bot-blocked). Everything fetchable "
              f"confirmed. After the manual checks, update GH_RATES_VERIFIED in app.py.")
    else:
        print("All hardcoded rates confirmed on their source pages. "
              "Update GH_RATES_VERIFIED in app.py to today's date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
